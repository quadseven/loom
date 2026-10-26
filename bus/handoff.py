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
REPORT_MARKER = "<!-- loom:report v1 -->"
# A goal report edited in place (bus/journal.py, loom#1560) carries the time of
# its edit on this line: GitHub keeps the comment's first creation time, so
# every reader that dates reports reads this stamp instead.
REPORTED_STAMP = "<!-- loom:reported %s -->"
_REPORTED_RE = re.compile(r"^<!-- loom:reported (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) -->[ \t]*$", re.M)


def reported_at(body: str) -> Optional[str]:
    """The edit time a report edited in place carries, or None."""
    found = _REPORTED_RE.findall(str(body or ""))
    return found[-1] if found else None


def _trusted_report_time(comment: Any, body: str, at_key: str) -> Optional[str]:
    """A report's REPORTED_STAMP time, only when GitHub's own times bear it
    out: not before the comment was created and not after its last edit. The
    stamp is text anyone could write; a forged or future one would make a
    report look fresh and hold the next one back. None otherwise."""
    if not body.startswith(REPORT_MARKER) or not isinstance(comment, dict):
        return None
    stamp = reported_at(body)
    created = str(comment.get(at_key) or comment.get("created_at")
                  or comment.get("createdAt") or "")
    updated = str(comment.get("updated_at") or comment.get("updatedAt")
                  or comment.get("updated") or "")
    if not stamp or not created or not updated:
        return None
    return stamp if created <= stamp <= updated else None
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
_TICK_AT = re.compile(r"^Tick (\S+), (%s)\s*$" % _HOLDER, re.M)
# A journal edited in place (bus/journal.py post) holds one holder's ticks of
# one UTC day: the newest at the top, the earlier ones under this block,
# newest first. Each new comment emailed the operator; an edit does not.
EARLIER_OPEN = "<details><summary>Earlier ticks today</summary>"
EARLIER_CLOSE = "</details>"


def split_journal(body: str) -> List[Tuple[str, str]]:
    """(tick time, journal body) for each tick of a journal comment, oldest
    first. A journal with one tick comes back whole, its time from its Tick
    line ("" when it has none)."""
    if not body.startswith(JOURNAL_MARKER) or EARLIER_OPEN not in body:
        tick = _TICK_AT.search(body)
        return [(tick.group(1) if tick else "", body)]
    top, _, earlier = body[len(JOURNAL_MARKER):].partition(EARLIER_OPEN)
    earlier = earlier.rsplit(EARLIER_CLOSE, 1)[0]
    sections = [top.strip("\n")]
    starts = [m.start() for m in _TICK_AT.finditer(earlier)]
    ends = starts[1:] + [len(earlier)]
    sections += [earlier[a:b].strip("\n") for a, b in zip(starts, ends, strict=True)]
    found = []
    for section in sections:
        if section.strip():
            tick = _TICK_AT.search(section)
            found.append((tick.group(1) if tick else "", JOURNAL_MARKER + "\n" + section))
    return sorted(found, key=lambda part: part[0])


def _tick_time_from_comment(comment: Any, tick_time: str, at_key: str = "at") -> Optional[str]:
    """Try to parse tick_time as %Y-%m-%dT%H:%M:%SZ; if it has no date, use the
    comment's created_at or updated_at (at_key is the timestamp field to use)."""
    parsed = _parse(tick_time)
    if parsed is not None:
        return tick_time
    timestamp = str(comment.get(at_key) or comment.get("created_at") or comment.get("createdAt") or "")
    if timestamp:
        day = timestamp[:10]
        return f"{day}T{tick_time}"
    return None


def expand_journals(comments: Iterable[Any], at_key: str = "at",
                    id_key: str = "id") -> List[Any]:
    """Each journal comment edited in place as one comment per tick, oldest
    first, with the tick's own time under at_key: the comment keeps the
    createdAt of its first tick, so a tick's time is its Tick line. A report
    edited in place gets its REPORTED_STAMP time under at_key. Every other
    comment passes through unchanged. Ticks before the newest get
    "<id>@<time>" as their id, so readers that skip a seen id keep them."""
    out: List[Any] = []
    for comment in comments:
        body = str(comment.get("body") or "").lstrip() if isinstance(comment, dict) else ""
        stamp = _trusted_report_time(comment, body, at_key)
        if stamp is not None:
            # A report edited in place is dated by its edit (loom#1560).
            out.append(dict(comment, **{at_key: stamp}))
            continue
        if not body.startswith(JOURNAL_MARKER) or EARLIER_OPEN not in body:
            out.append(comment)
            continue
        parts = split_journal(body)
        for index, (at, part) in enumerate(parts):
            item = dict(comment, body=part)
            if at:
                # Try to infer the full date when tick has no date
                at = _tick_time_from_comment(comment, at, at_key)
            item[at_key] = at or comment.get(at_key)
            if index < len(parts) - 1 and comment.get(id_key) is not None:
                item[id_key] = "%s@%s" % (comment[id_key], at or comment.get(id_key))
            out.append(item)
    return out


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
    {"body", "at", "login"}; anything else is data, never a handoff. GitHub
    logins ignore case, so the match does too."""
    allowed = {a.lower() for a in authors}
    found = [c for c in expand_journals(comments)
             if str(c.get("login") or "").lower() in allowed
             and str(c.get("body") or "").startswith(JOURNAL_MARKER) and c.get("at")]
    return sorted(found, key=lambda c: str(c["at"]))


def untrusted_journal_logins(comments: Iterable[Dict[str, Any]],
                             authors: Sequence[str]) -> List[str]:
    """The logins that wrote a journal-shaped comment but are not in
    `authors`, sorted: what a brain skipped, so a trust list missing the
    operator shows in its output instead of as "no stage handoffs"."""
    allowed = {a.lower() for a in authors}
    return sorted({str(c.get("login") or "") for c in comments
                   if str(c.get("body") or "").startswith(JOURNAL_MARKER)
                   and str(c.get("login") or "").lower() not in allowed})


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
    and the safe reading is "fresh", which keeps broad work closed.
    When the time cannot be read, the comment must have been created or
    updated within QUIET."""
    out = []
    for comment in found:
        at = _parse(str(comment.get("at") or ""))
        if journal_holder(comment) != holder:
            if at is None:
                # Unreadable time: check if the comment itself is fresh
                ts = (str(comment.get("updated_at") or comment.get("updatedAt") or "")
                      or str(comment.get("created_at") or comment.get("createdAt") or ""))
                if ts:
                    ts_parsed = _parse(ts)
                    if ts_parsed is not None and now - ts_parsed <= QUIET:
                        out.append(comment)
            elif now - at <= QUIET:
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
_ITEM_REF = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)")


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


def compact(comment: Dict[str, Any]) -> Dict[str, Any]:
    """A journal cut down to what the packet reads from it (the Tick line,
    the decided line, the model line and every handoff marker), so the
    station can keep it between passes. The packet reads the same from it
    as from the whole journal."""
    body = str(comment.get("body") or "")
    tick = _TICK.search(body)
    decided, model = decided_line(body), journal_model(body)
    lines = [JOURNAL_MARKER] + ([tick.group(0).strip()] if tick else [])
    lines += ["- Decided: %s" % decided] if decided else []
    lines += ["- Model: %s" % model] if model else []
    lines += [m.group(0) for m in _MARK.finditer(body)]
    return {"body": "\n".join(lines), "at": comment.get("at"), "login": comment.get("login")}


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


def _tick_line(goal: Goal, comment: Dict[str, Any]) -> Tuple[str, str, str]:
    """(holder, time, packet line) for one journal."""
    body, at = str(comment.get("body") or ""), str(comment.get("at") or "")
    holder = journal_holder(comment) or "unnamed"
    found = marks([comment])
    left = ["%s %s" % (m.stage, _ref(m.item[0], m.item[1], goal.shown))
            for m in found if m.kind == "defer"]
    decided = decided_line(body) if goal.shown else ""
    line = "- %s %s, model %s, on %s: %d done, %d deferred. Decided: %s. Left open: %s." % (
        at, holder, journal_model(body) or "unknown", _ref(goal.repo, goal.number, goal.shown),
        sum(1 for m in found if m.kind == "done"), len(left),
        decided.rstrip(".") or "(private goal home, not quoted)", ", ".join(left) or "none")
    return holder, at, line


def _record_line(record: Dict[str, Any]) -> str:
    """The packet line for a tick that holds the lock with no journal since
    it started: in progress, or it ended without one. The step record is the
    lock commit in the packet's own repo, so its last note is quoted."""
    notes = record.get("notes") or []
    newest = notes[-1] if notes else None
    note = newest.get("text") if isinstance(newest, dict) else ""
    text = "- %s %s, no journal yet: at step %s (%s/%d)" % (
        record.get("started_at"), record.get("holder"), record.get("step") or "none",
        record.get("step_index") or 0, len(record.get("steps") or ()))
    if note:
        text += ". Last note: %s" % quote(str(note))
    return text + "."


def _last_ticks(goals: Sequence[Goal], record: Optional[Dict[str, Any]]
                ) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """(each holder's newest tick time, the tick lines newest first)."""
    last: Dict[str, str] = {}
    lines: List[Tuple[str, str]] = []
    for goal in goals:
        for comment in goal.journals:
            holder, at, line = _tick_line(goal, comment)
            last[holder] = max(last.get(holder, ""), at)
            lines.append((at, line))
    record = record or {}
    holder, started = str(record.get("holder") or ""), str(record.get("started_at") or "")
    if holder and started and last.get(holder, "") < started:
        last[holder] = started
        lines.append((started, _record_line(record)))
    lines.sort(key=lambda pair: pair[0], reverse=True)
    return last, lines


def _quiet_since(holder: str, last: Dict[str, str], now: datetime) -> bool:
    """Whether a brain has not ticked within QUIET. A brain with no tick at
    all counts as quiet; an unreadable time counts as fresh, as in
    peer_journals."""
    at = last.get(holder)
    if not at:
        return True
    when = _parse(at)
    return when is not None and when < now - QUIET


def _open_defers(goals: Sequence[Goal]) -> List[Tuple[Mark, bool]]:
    """The standing defers across every goal home, oldest first, each with
    whether its journal may be quoted. For each stage and item (and brief
    key) the newest marker on any goal home wins, as in `resume_items`, so a
    done marker, or a newer defer, retires an older defer."""
    found = sorted(((m, g.shown) for g in goals for m in marks(g.journals)),
                   key=lambda pair: pair[0].at)
    latest: Dict[Tuple[str, Tuple[str, int], str], Tuple[Mark, bool]] = {}
    for mark, shown in found:
        identity = mark.key if mark.stage == "brief" else ""
        latest[(mark.stage, mark.item, identity)] = (mark, shown)
    return sorted((pair for pair in latest.values() if pair[0].kind == "defer"),
                  key=lambda pair: pair[0].at)


def _defer_lines(standing: Sequence[Tuple[Mark, bool]], last: Dict[str, str],
                 now: datetime, public: Callable[[str], bool]) -> List[str]:
    """The open defers section."""
    stale = [pair for pair in standing if _quiet_since(pair[0].holder, last, now)]
    out = ["## Open defers (%d, %d from brains no longer ticking)" % (len(standing), len(stale))]
    for mark, shown in standing[:PACKET_DEFERS]:
        named = bool(public(mark.item[0]))
        line = "- %s %s, deferred by %s at %s" % (
            mark.stage, _ref(mark.item[0], mark.item[1], named), mark.holder, mark.at)
        if mark.note and shown and named:
            line += ": %s" % quote(mark.note)
        if (mark, shown) in stale:
            seen = last.get(mark.holder)
            line += ". Open work for any brain: %s last ticked %s, over %g hours ago" % (
                mark.holder, ("at " + seen) if seen else "never", QUIET.total_seconds() / 3600)
        out.append(line + ".")
    if len(standing) > PACKET_DEFERS:
        out.append("- and %d more; `resume` lists them all." % (len(standing) - PACKET_DEFERS))
    if not standing:
        out.append("- none")
    out.append("`resume` still drops a defer whose item changed or closed since; "
               "act on what it lists.")
    return out


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
    out += [""] + _defer_lines(_open_defers(goals), last, now, public)
    out += _rules_lines(rules)
    if unread:
        out += ["", "Not read this pass: %d repo(s); their ticks and defers are missing "
                "above." % unread]
    return "\n".join(out)


def _rules_lines(rules: Tuple[str, List[str]]) -> List[str]:
    """The Rules section: docs/MUSE.md's Rules as read from GitHub."""
    commit, lines = rules
    return (["", "## Rules (docs/MUSE.md at %s)" % (commit or "an unknown commit")]
            + (lines or ["- not read this pass; read docs/MUSE.md, Rules."]))


def _stored_ref(ref: Any, public: Callable[[str], bool]) -> str:
    """A stored OWNER/NAME#N as the packet may show it."""
    m = _ITEM_REF.fullmatch(str(ref or ""))
    if not m:
        return "an item not shown"
    return _ref(m.group(1), int(m.group(2)), bool(public(m.group(1))))


def _count(values: Any) -> int:
    return len(values) if isinstance(values, list) else 0


def _stored_tick_line(tick: Dict[str, Any]) -> str:
    """One stored tick. Its lists are counted, not quoted: a row does not
    say which goal home their text came from, so it may be private."""
    return "- %s %s on %s, model %s, ended %s: %s. %d decided, %d left open, %d handed off." % (
        quote(str(tick.get("started"))), quote(str(tick.get("holder"))),
        quote(str(tick.get("lane"))), quote(str(tick.get("model"))),
        quote(str(tick.get("ended"))), quote(str(tick.get("outcome"))),
        _count(tick.get("decided")), _count(tick.get("left_open")),
        _count(tick.get("handed_off")))


def _commitment_lines(commitments: Sequence[Dict[str, Any]],
                      public: Callable[[str], bool]) -> List[str]:
    """The open commitments section; each one's note is left out, as above."""
    out = ["## Open commitments (%d)" % len(commitments)]
    for c in commitments[:PACKET_DEFERS]:
        stage = c.get("stage") if c.get("stage") in STAGES else "work"
        out.append("- %s %s, from %s's tick %s, due %s." % (
            stage, _stored_ref(c.get("item"), public), quote(str(c.get("holder"))),
            quote(str(c.get("tick_id"))), quote(str(c.get("due") or "none"))))
    if len(commitments) > PACKET_DEFERS:
        out.append("- and %d more; `resume` lists them all." % (len(commitments) - PACKET_DEFERS))
    if not commitments:
        out.append("- none")
    return out


def _digest_line(rule: Dict[str, Any], public: Callable[[str], bool]) -> str:
    """One rules digest source and the commit or comment it was loaded from;
    a private goal home's is neither named nor keyed."""
    source = str(rule.get("source") or "")
    m = _ITEM_REF.fullmatch(source)
    if m and not public(m.group(1)):
        return "- " + _ref(m.group(1), int(m.group(2)), False)
    return "- %s at %s" % (quote(source), quote(str(rule.get("source_ref"))))


def stored_packet(stored: Dict[str, Any], rules: Tuple[str, List[str]],
                  public: Callable[[str], bool]) -> str:
    """The handoff packet text from the dashboard's brain memory
    (bus/brain_memory.fetch_handoff: lists of ticks, open commitments and
    rules): the last ticks, the open commitments, the rules digest's sources,
    and docs/MUSE.md's Rules as read from GitHub. `public(repo)` says
    whether an item's repo may be named; the rows' free text is not quoted,
    so `python3 -m bus.brain_memory handoff` reads it in full."""
    out = [HANDOFF_MARKER,
           "Brain handoff packet. Every brain reads it first in its tick (docs/MUSE.md, "
           "Tick). The station rebuilds it each pass from the dashboard's brain memory; "
           "`python3 -m bus.brain_memory handoff` prints it in full.", "",
           "## Last %d ticks" % PACKET_TICKS]
    out += [_stored_tick_line(t) for t in stored["ticks"][:PACKET_TICKS]] or ["- none"]
    out += [""] + _commitment_lines(stored["commitments"], public)
    out += ["", "## Rules digest"]
    out += [_digest_line(r, public) for r in stored["rules"]] or ["- none"]
    return "\n".join(out + _rules_lines(rules))
