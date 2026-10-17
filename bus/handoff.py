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
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import text as _text

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
_KEY = r"[a-f0-9]{8}"
_NOTE = r'(?:"([^"]*)"|([^\s>]+))'
_MARK = re.compile(
    r'<!-- loom:(defer|done) v1 stage=([a-z]+) item=(%s)(?: rev=(%s))?(?: key=(%s))?(?: note=%s)? holder=(%s)(?: note=%s)? -->'
    % (_ITEM, _REV, _KEY, _NOTE, _HOLDER, _NOTE))
_TICK = re.compile(r"^Tick \S+, (%s)\s*$" % _HOLDER, re.M)


class Mark(NamedTuple):
    kind: str
    stage: str
    item: Tuple[str, int]
    rev: str
    holder: str
    at: str
    note: str = ""
    key: str = ""


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
                 note: Optional[str] = None, key: Optional[str] = None) -> str:
    """The line a brain puts in its journal for stage work it leaves undone.
    The revision is the item as the brain left it (bus.brain defer)."""
    _check(stage, holder, rev, note)
    if key is not None and not re.fullmatch(_KEY, key):
        raise ValueError("key must be eight lowercase hexadecimal characters")
    extra = (' note="%s"' % note) if note else ""
    keyed = (" key=%s" % key) if key else ""
    return "<!-- loom:defer v1 stage=%s item=%s rev=%s%s holder=%s%s -->" % (
        stage, item_ref(item), rev, keyed, holder, extra)


def done_marker(stage: str, item: Tuple[str, int], holder: str,
                key: Optional[str] = None) -> str:
    """The line a brain puts in its journal for stage work it finished."""
    _check(stage, holder, None)
    if key is not None and not re.fullmatch(_KEY, key):
        raise ValueError("key must be eight lowercase hexadecimal characters")
    keyed = (" key=%s" % key) if key else ""
    return "<!-- loom:done v1 stage=%s item=%s%s holder=%s -->" % (
        stage, item_ref(item), keyed, holder)


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
            key = m.group(5) or ""
            note = m.group(6) or m.group(7) or m.group(9) or m.group(10) or ""
            holder = m.group(8)
            if stage not in STAGES or (kind == "defer" and not rev):
                continue
            repo, _, number = ref.partition("#")
            out.append(Mark(kind, stage, (repo, int(number)), rev, holder,
                            str(comment["at"]), note, key))
    return out


def resume_items(found: Sequence[Mark]) -> List[Mark]:
    """The defers no later marker replaced: for each stage and item, the newest
    marker wins, so a done marker, or a newer defer, retires an older defer."""
    latest: Dict[Tuple[str, Tuple[str, int], str], Mark] = {}
    for mark in found:
        identity = mark.key if mark.stage == "brief" else ""
        latest[(mark.stage, mark.item, identity)] = mark
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


# The handoff packet (docs/BRAIN.md, Handoff packet): one comment on the Loom
# goal home that the station keeps current, which every brain reads first.
HANDOFF_MARKER = "<!-- loom:handoff v1 -->"
PACKET_TICKS = 5
# Open defers listed in full; the rest are counted, so the comment stays far
# below GitHub's comment size limit.
PACKET_DEFERS = 30
LINE_MAX = 160
_MODEL_LINE = re.compile(r"^(?:- )?Model: *([A-Za-z0-9._:/-]+)\s*$", re.M)
_DECIDED_LINE = re.compile(r"^(?:- )?Decided: *(\S.*?)\s*$", re.M)


def quote(value: str, limit: int = LINE_MAX) -> str:
    """Public text from GitHub as one plain line for the packet: ASCII only,
    at-signs dropped so no quote mentions anyone again, clipped to `limit`.
    Text that fails the posting checks (bus/text.py) is not quoted at all."""
    line = " ".join(str(value).replace("@", "").split())
    line = line.encode("ascii", "replace").decode("ascii")
    if len(line) > limit:
        line = line[:limit - 3].rstrip() + "..."
    try:
        _text.check(line)
    except ValueError as err:
        return "(not quoted: %s)" % err
    return line


def journal_model(body: str) -> str:
    """The model a journal names on a `- Model: NAME` line, or ""."""
    m = _MODEL_LINE.search(body)
    return m.group(1) if m else ""


def decided_line(body: str) -> str:
    """What a journal says was decided: its `- Decided:` line, else its first
    line of prose after the Tick line (markers and headings skipped)."""
    m = _DECIDED_LINE.search(body)
    if m:
        return quote(m.group(1))
    for raw in body.splitlines():
        line = raw.strip()
        if not line or "<!--" in line or _TICK.match(line):
            continue
        line = line.lstrip("#").strip()
        if line.startswith("- "):
            line = line[2:]
        if line:
            return quote(line)
    return ""


def rules_section(doc: str) -> List[str]:
    """The lines of the `## Rules` section of docs/MUSE.md, without the
    heading and trailing blank lines; [] when there is none."""
    lines = doc.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "## Rules")
    except StopIteration:
        return []
    out: List[str] = []
    for line in lines[start + 1:]:
        if line.startswith("# ") or line.startswith("## "):
            break
        out.append(line.rstrip())
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return out


class Goal(NamedTuple):
    """One goal home's journals as the packet reads them. `shown` says whether
    the repo is public (or the packet's own), so its name and text may be
    quoted in the packet."""
    repo: str
    number: int
    shown: bool
    journals: List[Dict[str, Any]]


def _ref(repo: str, number: int, shown: bool) -> str:
    return "%s#%d" % (repo, number) if shown else "issue %d of a private repo" % number


def _last_ticks(goals: Sequence[Goal], record: Optional[Dict[str, Any]]
                ) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """(each holder's newest tick time, the tick lines newest first). The
    step record is the lock commit in the packet's own repo, so its last note
    is quoted."""
    last: Dict[str, str] = {}
    lines: List[Tuple[str, str]] = []
    for goal in goals:
        where = _ref(goal.repo, goal.number, goal.shown)
        for comment in goal.journals:
            body, at = str(comment.get("body") or ""), str(comment.get("at") or "")
            holder = journal_holder(comment) or "unnamed"
            last[holder] = max(last.get(holder, ""), at)
            found = marks([comment])
            left = ["%s %s" % (m.stage, _ref(m.item[0], m.item[1], goal.shown))
                    for m in found if m.kind == "defer"]
            decided = decided_line(body) if goal.shown else ""
            lines.append((at, "- %s %s, model %s, on %s: %d done, %d deferred. Decided: %s. "
                          "Left open: %s." % (
                              at, holder, journal_model(body) or "unknown", where,
                              sum(1 for m in found if m.kind == "done"), len(left),
                              decided.rstrip(".") or "(private goal home, not quoted)",
                              ", ".join(left) or "none")))
    holder = str((record or {}).get("holder") or "")
    started = str((record or {}).get("started_at") or "")
    if holder and started and last.get(holder, "") < started:
        # A tick holds the lock with no journal since it started: in progress,
        # or it ended without one.
        last[holder] = started
        notes = (record or {}).get("notes") or []
        note = notes[-1].get("text") if notes and isinstance(notes[-1], dict) else ""
        text = "- %s %s, no journal yet: at step %s (%s/%d)" % (
            started, holder, record.get("step") or "none", record.get("step_index") or 0,
            len(record.get("steps") or ()))
        if note:
            text += ". Last note: %s" % quote(str(note))
        lines.append((started, text + "."))
    lines.sort(key=lambda pair: pair[0], reverse=True)
    return last, lines


def _open_defers(goals: Sequence[Goal]) -> List[Tuple[Mark, bool]]:
    """The standing defers across every goal home, oldest first, each with
    whether its journal may be quoted. For each stage and item the newest
    marker on any goal home wins, as in `resume`, so a done marker, or a newer
    defer, retires an older defer."""
    found = sorted(((m, g.shown) for g in goals for m in marks(g.journals)),
                   key=lambda pair: pair[0].at)
    latest: Dict[Tuple[str, Tuple[str, int]], Tuple[Mark, bool]] = {}
    for mark, shown in found:
        latest[(mark.stage, mark.item)] = (mark, shown)
    return sorted((pair for pair in latest.values() if pair[0].kind == "defer"),
                  key=lambda pair: pair[0].at)


def packet(goals: Sequence[Goal], record: Optional[Dict[str, Any]],
           rules: Tuple[str, List[str]], now: datetime, public: Callable[[str], bool],
           unread: int = 0) -> str:
    """The handoff packet text. `public(repo)` says whether an item's repo may
    be named. Nothing in it moves with the clock alone, so the station
    rewrites the comment only when a tick, a marker or the rules change (and
    when a defer's brain passes QUIET without a tick)."""
    last, ticks = _last_ticks(goals, record)
    out = [HANDOFF_MARKER,
           "Brain handoff packet. Every brain reads it first in its tick (docs/MUSE.md, "
           "Tick). The station rebuilds it each pass from the brain journals, the defer "
           "and done markers on the goal homes, and the tick step record.", "",
           "## Last %d ticks" % PACKET_TICKS]
    out += [line for _, line in ticks[:PACKET_TICKS]] or ["- none"]
    standing = _open_defers(goals)
    stale = [(m, s) for m, s in standing
             if not last.get(m.holder) or (_parse(last[m.holder]) or now) < now - QUIET]
    out += ["", "## Open defers (%d, %d from brains no longer ticking)" % (
        len(standing), len(stale))]
    for mark, shown in standing[:PACKET_DEFERS]:
        named = bool(public(mark.item[0]))
        line = "- %s %s, deferred by %s at %s" % (
            mark.stage, _ref(mark.item[0], mark.item[1], named), mark.holder, mark.at)
        if mark.note and shown and named:
            line += ": %s" % quote(mark.note)
        if (mark, shown) in stale:
            line += (". Open work for any brain: %s last ticked %s, over %g hours ago" % (
                mark.holder, ("at " + last[mark.holder]) if last.get(mark.holder) else "never",
                QUIET.total_seconds() / 3600))
        out.append(line + ".")
    if len(standing) > PACKET_DEFERS:
        out.append("- and %d more; `resume` lists them all." % (len(standing) - PACKET_DEFERS))
    if not standing:
        out.append("- none")
    out.append("`resume` still drops a defer whose item changed or closed since; "
               "act on what it lists.")
    commit, lines = rules
    out += ["", "## Rules (docs/MUSE.md at %s)" % (commit or "an unknown commit")]
    out += lines or ["- not read this pass; read docs/MUSE.md, Rules."]
    if unread:
        out += ["", "Not read this pass: %d repo(s); their ticks and defers are missing "
                "above." % unread]
    return "\n".join(out)
