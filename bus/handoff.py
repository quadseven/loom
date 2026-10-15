"""Stage handoff between brains (docs/BRAIN.md, Handoff).

A brain that leaves stage work undone writes a defer marker for each item in
its journal; a brain that finishes stage work writes a done marker. From the
journals on a goal home, this module works out, per stage, which brain last
held it and what is left to resume, so a peer can finish deferred work even
while the deferring brain keeps journaling.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

JOURNAL_MARKER = "<!-- loom:journal v1 -->"
# GitHub work any brain can do. Ships and rolls are not here: they stay with
# whoever a goal's rules name.
STAGES = ("review", "merge", "failed", "ask", "regression", "brief")
# The old rule: a brain outside its own scope acts only when no other brain
# journaled in this window.
QUIET = timedelta(hours=2)
_HOLDER = r"[A-Za-z0-9._-]+"
_ITEM = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+"
_REV = r"[A-Za-z0-9._:-]+"
_NOTE = r'(?:"([^"]*)"|([^\s>]+))'
_MARK = re.compile(
    r'<!-- loom:(defer|done) v1 stage=([a-z]+) item=(%s)(?: rev=(%s))?(?: note=%s)? holder=(%s)(?: note=%s)? -->'
    % (_ITEM, _REV, _NOTE, _HOLDER, _NOTE))
_TICK = re.compile(r"^Tick \S+, (%s)\s*$" % _HOLDER, re.M)


class Mark(NamedTuple):
    kind: str
    stage: str
    item: Tuple[str, int]
    rev: str
    holder: str
    at: str
    note: str = ""


def item_ref(item: Tuple[str, int]) -> str:
    return "%s#%d" % item


def _check(stage: str, holder: str, rev: Optional[str], note: Optional[str] = None) -> None:
    if stage not in STAGES:
        raise ValueError("stage is one of %s" % ", ".join(STAGES))
    if not re.fullmatch(_HOLDER, holder or ""):
        raise ValueError("holder may only use letters, digits, '.', '_' and '-'")
    if rev is not None and not re.fullmatch(_REV, rev):
        raise ValueError("revision is not a revision")
    if note is not None:
        if "\n" in note or "\r" in note:
            raise ValueError("note must be one line")
        if '"' in note:
            raise ValueError("note must not contain double quotes")


def defer_marker(stage: str, item: Tuple[str, int], rev: str, holder: str,
                 note: Optional[str] = None) -> str:
    """The line a brain puts in its journal for stage work it leaves undone.
    The revision is the item as the brain left it (bus.brain defer)."""
    _check(stage, holder, rev, note)
    extra = (' note="%s"' % note) if note else ""
    return "<!-- loom:defer v1 stage=%s item=%s rev=%s holder=%s%s -->" % (
        stage, item_ref(item), rev, holder, extra)


def done_marker(stage: str, item: Tuple[str, int], holder: str) -> str:
    """The line a brain puts in its journal for stage work it finished."""
    _check(stage, holder, None)
    return "<!-- loom:done v1 stage=%s item=%s holder=%s -->" % (stage, item_ref(item), holder)


def journals(comments: Iterable[Dict[str, Any]], authors: Sequence[str]) -> List[Dict[str, Any]]:
    """Journal comments from allowed logins, oldest first. Each comment is
    {"body", "at", "login"}; anything else is data, never a handoff."""
    found = [c for c in comments if c.get("login") in authors
             and str(c.get("body") or "").startswith(JOURNAL_MARKER) and c.get("at")]
    return sorted(found, key=lambda c: str(c["at"]))


def marks(found: Iterable[Dict[str, Any]]) -> List[Mark]:
    """Every handoff marker in the journals, in the order written."""
    out: List[Mark] = []
    for comment in found:
        for m in _MARK.finditer(str(comment.get("body") or "")):
            kind, stage, ref = m.group(1), m.group(2), m.group(3)
            rev = m.group(4) or ""
            note = m.group(5) or m.group(6) or m.group(8) or m.group(9) or ""
            holder = m.group(7)
            if stage not in STAGES or (kind == "defer" and not rev):
                continue
            repo, _, number = ref.partition("#")
            out.append(Mark(kind, stage, (repo, int(number)), rev, holder, str(comment["at"]), note))
    return out


def resume_items(found: Sequence[Mark]) -> List[Mark]:
    """The defers no later marker replaced: for each stage and item, the newest
    marker wins, so a done marker, or a newer defer, retires an older defer."""
    latest: Dict[Tuple[str, Tuple[str, int]], Mark] = {}
    for mark in found:
        latest[(mark.stage, mark.item)] = mark
    return [m for m in latest.values() if m.kind == "defer"]


def live(items: Sequence[Mark], current: Dict[Tuple[str, int], str]) -> List[Mark]:
    """The resume items whose item is still as the deferring brain left it:
    `current` maps each open item to its revision now, so an item that was
    finished (closed, merged) or changed (new commits, new comments, another
    state) no longer counts."""
    return [m for m in items if current.get(m.item) == m.rev]


def stages(found: Sequence[Mark]) -> Dict[str, Dict[str, Optional[Mark]]]:
    """Per stage: the newest marker (its holder is the brain responsible, and a
    defer means the stage is yielded) and the newest done marker."""
    out: Dict[str, Dict[str, Optional[Mark]]] = {}
    for mark in found:
        entry = out.setdefault(mark.stage, {"latest": None, "done": None})
        entry["latest"] = mark
        if mark.kind == "done":
            entry["done"] = mark
    return out


def stage_lines(found: Sequence[Mark]) -> List[str]:
    lines = []
    owners = stages(found)
    for stage in STAGES:
        entry = owners.get(stage)
        if not entry:
            continue
        latest, done = entry["latest"], entry["done"]
        assert latest is not None
        if latest.kind == "defer":
            text = "yielded by %s at %s" % (latest.holder, latest.at)
        else:
            text = "held by %s" % latest.holder
        if done is not None:
            text += "; last done %s by %s at %s" % (item_ref(done.item), done.holder, done.at)
        lines.append("- stage %s: %s" % (stage, text))
    return lines


def _parse(value: str) -> Optional[datetime]:
    # None is not a dropped journal: peer_journals reads an unreadable time as
    # fresh, so a malformed timestamp keeps broad work closed, never open.
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def journal_holder(comment: Dict[str, Any]) -> str:
    """The holder a journal names on its Tick line, or "" when it names none."""
    m = _TICK.search(str(comment.get("body") or ""))
    return m.group(1) if m else ""


def peer_journals(found: Iterable[Dict[str, Any]], holder: str,
                  now: datetime) -> List[Dict[str, Any]]:
    """Journals from any other brain within QUIET of now. A journal naming no
    holder counts as another brain's, and so does one whose time cannot be
    read: GitHub always stamps a comment, so an unreadable time is a fault,
    and the safe reading is "fresh", which keeps broad work closed."""
    out = []
    for comment in found:
        at = _parse(str(comment.get("at") or ""))
        if journal_holder(comment) != holder and (at is None or now - at <= QUIET):
            out.append(comment)
    return out


def quiet(found: Iterable[Dict[str, Any]], holder: str, now: datetime) -> bool:
    """The freshness-only rule: no other brain journaled on any goal lately."""
    return not peer_journals(found, holder, now)


def may_act(stage: str, item: Tuple[str, int], holder: str,
            found: Sequence[Dict[str, Any]], now: datetime,
            current: Dict[Tuple[str, int], str]) -> bool:
    """Whether a brain may do stage work outside its own scope: when every
    other brain is quiet, as before, or when the item is a live resume item a
    brain yielded, however fresh its journals are."""
    if quiet(found, holder, now):
        return True
    return any(m.stage == stage and m.item == item
               for m in live(resume_items(marks(found)), current))
