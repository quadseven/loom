"""What each goal home says, for the Goals cards and the Floor standup.

Read from data the snapshot already holds: the goal issues' titles, the
issue comments of the last two weeks (reports, journals and proof records),
and the dated metrics comments. A value no source carries is None with a
reason, never a stand-in.

- report: the newest report's headline (its first line) and time.
- measure: the newest `Measure:` line of a report or journal, when any.
- verified: proof records on the goal's tickets per UTC day, last 7 days,
  and the 7 days before (the "was").
- factory: for the metrics goal, the dated "N/9 pass" lines, last 7 days.
- parent: the bigger goal this one rolls up into, from a `Parent goal:
  OWNER/NAME#N` line in the goal home's body or its GitHub sub-issue parent.
  The Goals page shows one card per top-level goal (rollup) with the smaller
  goals as rows inside it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bus import brain

# A goal's unproven work (loom#483): its tickets at merged or shipped. A
# verified ticket carries loom:verified instead, so it never counts; a
# failed audit moves a ticket back to shipped, so it does. The snapshot rows
# already carry each ticket's state, so the Floor needs no comment reads.
UNPROVEN_STATES = ("shipped",)  # same as bus.brain.UNPROVEN: verification debt only

REPORT_MARKER = "<!-- loom:report v1 -->"
JOURNAL_MARKER = "<!-- loom:journal v1 -->"
PROOF_MARKER = "<!-- loom:proof v1 -->"
_ISSUE_URL = re.compile(r"/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)$")
_PARENT_LINE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?Parent goal[ \t]*:[ \t]*"
                          r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(\d+)\b", re.I | re.M)
_MEASURE = re.compile(r"^-?[ \t]*Measure:[ \t]*(.+)$", re.I | re.M)
_FACTORY = re.compile(r"^<!-- loom:metrics v1 -->\n(\d{4}-\d{2}-\d{2}): (\d)/9 pass")
_SECTION_LINE = re.compile(r"^\s*(?:[-*]\s*)?(Going well|Not going well|Still stuck(?: since last report)?"
                           r"|Next)\s*:\s*(.*)$", re.I)
_SECTION_HEAD = re.compile(r"^#{2,4}\s*(Going well|Not going well|Still stuck(?: since last report)?"
                           r"|Next|Needs you)\s*$", re.I)
_KEYS = {"going well": "well", "not going well": "not well", "still stuck": "stuck",
         "still stuck since last report": "stuck", "next": "next", "needs you": "needs you"}
DAYS = 7
# "No report on this goal in the last two weeks" is said only when a report
# source for the goal home was read and holds no report this recent.
QUIET = timedelta(days=14)
IN_FLIGHT_STATES = ("queued", "briefed", "running", "pr")  # the station standup's "in flight"


def issue_ref(comment: Dict[str, Any]) -> Optional[str]:
    """owner/name#N of the issue a comment belongs to, from its issue_url."""
    match = _ISSUE_URL.search(str(comment.get("issue_url") or ""))
    return "%s#%s" % match.groups() if match else None


def parent_of(repo: str, issue: Dict[str, Any],
              repos: Optional[Iterable[str]] = None) -> Optional[str]:
    """The goal a goal home rolls up into: its `Parent goal: OWNER/NAME#N`
    body line (a bare `#N` is in the same repo), else its GitHub sub-issue
    parent (`parent_issue_url`). None for a top-level goal, and, with
    `repos`, for a parent in any repo not listed there."""
    parent = None
    match = _PARENT_LINE.search(str(issue.get("body") or ""))
    if match:
        parent = "%s#%s" % (match.group(1) or repo, match.group(2))
    else:
        match = _ISSUE_URL.search(str(issue.get("parent_issue_url") or ""))
        parent = "%s#%s" % match.groups() if match else None
    if parent and repos is not None and parent.partition("#")[0] not in set(repos):
        return None
    return parent


_PART_OF_LINE = re.compile(r"^[ \t]*Part of[ \t]+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#?(\d+)",
                           re.I | re.M)
# Decision 0003: a goal holds at most 6 open epics, an epic at most 15 tickets.
EPIC_CAP = 6
EPIC_TICKET_CAP = 15


def issue_parent(issue: Dict[str, Any]) -> Optional[str]:
    """owner/name#N of an issue's GitHub sub-issue parent, or None."""
    match = _ISSUE_URL.search(str(issue.get("parent_issue_url") or ""))
    return "%s#%s" % match.groups() if match else None


def epic_goal_of(repo: str, issue: Dict[str, Any],
                 repos: Optional[Iterable[str]] = None) -> Optional[str]:
    """The goal an epic belongs to: its `Part of` line (a bare `#N` is in
    the same repo), else its sub-issue parent. With `repos`, None for a goal
    in any repo not listed there."""
    match = _PART_OF_LINE.search(str(issue.get("body") or ""))
    goal = "%s#%s" % (match.group(1) or repo, match.group(2)) if match else issue_parent(issue)
    if goal and repos is not None and goal.partition("#")[0] not in set(repos):
        return None
    return goal


def rollup(goals: Iterable[str], parents: Dict[str, Optional[str]]) -> Dict[str, List[str]]:
    """Each top-level goal and the goals under it, at any depth, in the order
    first seen. Goals whose parents loop (a names b, b names a) share one
    top: the lowest-sorting goal of the loop."""
    out: Dict[str, List[str]] = {}
    for goal in goals:
        chain, top = [goal], goal
        while parents.get(top):
            nxt = parents[top]
            if nxt in chain:
                top = min(chain[chain.index(nxt):])
                break
            top = nxt
            chain.append(top)
        out.setdefault(top, [])
        if goal != top and goal not in out[top]:
            out[top].append(goal)
    return out


def age(stamp: Any, now: datetime) -> Optional[str]:
    """How long ago a GitHub time was, as the operator reads it: 40m, 3h, 2d."""
    when = _stamp(stamp)
    if when is None:
        return None
    minutes = max(0, int((now - when).total_seconds() // 60))
    if minutes < 120:
        return "%dm" % minutes
    if minutes < 48 * 60:
        return "%dh" % (minutes // 60)
    return "%dd" % (minutes // (24 * 60))


def _stamp(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def headline(body: str) -> Optional[str]:
    """A report's first non-empty line after its marker."""
    return next((line.strip() for line in body.splitlines()[1:] if line.strip()), None)


def short(text: str, limit: int = 180) -> str:
    """The first sentence of `text`, cut at `limit` characters on a word."""
    text = " ".join(text.split())
    sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    if len(sentence) <= limit:
        return sentence
    return sentence[:limit].rsplit(" ", 1)[0] + " ..."


def sections(body: str) -> Dict[str, str]:
    """A report's going well / not going well / still stuck / next, from
    `Going well: ...` lines (brain reports) or `### Going well` headings
    followed by text (the station's standup); each cut to one sentence."""
    found: Dict[str, str] = {}
    current: Optional[str] = None
    collected: List[str] = []

    def close() -> None:
        if current and current not in found and " ".join(collected).strip():
            found[current] = short(" ".join(collected))

    for line in body.splitlines()[1:]:
        heading = _SECTION_HEAD.match(line)
        inline = _SECTION_LINE.match(line)
        if heading or inline or line.startswith("#"):
            close()
            current, collected = None, []
        if heading:
            current = _KEYS[heading.group(1).lower()]
        elif inline:
            key = _KEYS[inline.group(1).lower()]
            if inline.group(2).strip() and key not in found:
                found[key] = short(inline.group(2))
        elif current and line.strip():
            collected.append(line.strip().lstrip("-* "))
    close()
    found.pop("needs you", None)
    return found


def _days(now: datetime) -> List[str]:
    today = now.astimezone(timezone.utc).date()
    return [(today - timedelta(days=back)).isoformat() for back in range(DAYS - 1, -1, -1)]


def _verified(tickets: Iterable[str], proofs: Dict[str, List[str]], now: datetime) -> Tuple[List[int], int]:
    """Proof records per day for the last 7 days, and the count of the 7 before."""
    days = _days(now)
    first = datetime.fromisoformat(days[0]).date()
    counts = {day: 0 for day in days}
    before = 0
    for ticket in tickets:
        for day in proofs.get(ticket, []):
            if day in counts:
                counts[day] += 1
            elif first - timedelta(days=DAYS) <= datetime.fromisoformat(day).date() < first:
                before += 1
    return [counts[day] for day in days], before


def _factory(metric_comments: Sequence[Dict[str, Any]], now: datetime) -> Optional[Dict[str, Any]]:
    by_day: Dict[str, int] = {}
    for comment in metric_comments:
        match = _FACTORY.match(str(comment.get("body") or ""))
        if match:
            by_day[match.group(1)] = int(match.group(2))
    if not by_day:
        return None
    days = _days(now)
    latest = max(by_day)
    earlier = sorted(day for day in by_day if day < latest)
    return {"metric": "%d/9" % by_day[latest], "unit": "factory numbers passing",
            "previous": "%d/9" % by_day[earlier[-1]] if earlier else None,
            "history": [by_day.get(day) for day in days], "scale": 9}


def _index(comments: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[str]]]:
    """Reports and journals by goal issue, and proof record days by ticket."""
    notes: Dict[str, List[Dict[str, Any]]] = {}
    proofs: Dict[str, List[str]] = {}
    seen = set()
    for comment in comments:
        ref, body = issue_ref(comment), str(comment.get("body") or "")
        if not ref or (comment.get("id") is not None and comment["id"] in seen):
            continue
        seen.add(comment.get("id"))
        if body.startswith((REPORT_MARKER, JOURNAL_MARKER)):
            notes.setdefault(ref, []).append(comment)
        elif body.startswith(PROOF_MARKER) and _stamp(comment.get("created_at")):
            proofs.setdefault(ref, []).append(str(comment.get("created_at"))[:10])
    return notes, proofs


def _latest_measure(notes: Sequence[Dict[str, Any]]) -> Optional[str]:
    found = [m.group(1).strip() for c in notes for m in [_MEASURE.search(str(c.get("body") or ""))] if m]
    found = [value for value in found if value.lower() not in ("", "none")]
    return found[-1] if found else None


def _unproven(tickets: Sequence[str], states: Dict[str, str]) -> int:
    """The goal's tickets at merged or shipped: unproven work still waiting on
    a live check, against the limit the station and the brain enforce."""
    return sum(1 for ref in tickets if states.get(ref) in UNPROVEN_STATES)


def _goal_record(title: Optional[str], notes: Sequence[Dict[str, Any]], tickets: Sequence[str],
                 states: Dict[str, str], proofs: Dict[str, List[str]],
                 limit: int, now: datetime, unread_notes: Optional[str] = None,
                 unread_proofs: Optional[str] = None, in_flight: int = 0,
                 parent: Optional[str] = None) -> Dict[str, Any]:
    ordered = sorted(notes, key=lambda c: str(c.get("created_at") or ""))
    reports = [c for c in ordered if str(c.get("body") or "").startswith(REPORT_MARKER)]
    newest = reports[-1] if reports else None
    newest_at = _stamp(newest.get("created_at")) if newest else None
    history, before = _verified(tickets, proofs, now)
    record = {
        "title": title, "parent": parent, "tickets": len(tickets), "in_flight": in_flight,
        "report": short(headline(str(newest.get("body") or "")) or "", 220) if newest else None,
        "report_time": newest.get("created_at") if newest else None,
        # True only when the goal's reports were read and none is two weeks fresh.
        "report_quiet": unread_notes is None and (newest_at is None or now - newest_at > QUIET),
        "report_reason": None if newest or unread_notes is None else unread_notes,
        "measure": _latest_measure(ordered),
        "metric": str(sum(history)), "unit": "verified with proof, last 7 days",
        "previous": str(before), "history": history, "scale": None,
        "unproven": _unproven(tickets, states), "unproven_limit": limit,
    }
    if unread_proofs:
        # A count missing some of the goal's proof records would understate
        # it: no number, and the source that was not read.
        record.update({"metric": None, "previous": None, "history": [],
                       "metric_reason": unread_proofs})
    return record


def goal_context(rows: Sequence[Dict[str, Any]], comments: Sequence[Dict[str, Any]],
                 titles: Dict[str, Optional[str]], metric_comments: Sequence[Dict[str, Any]],
                 metrics_goal: Optional[str], now: datetime,
                 parents: Optional[Dict[str, Optional[str]]] = None,
                 unread: Optional[Dict[str, str]] = None,
                 epics: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """One record per goal the tickets name (their `Part of` line), and one
    per parent goal those goals roll up into.

    `parents` maps a goal to the goal it rolls up into (parent_of). `unread`
    maps a repo to why its issue comments were not read this pass: a goal in
    that repo gets that reason instead of "no report", and a goal with
    tickets there gets it instead of a proof count. `epics` maps each epic
    the tickets name to its title, open flag and goal; each goal record
    lists its epics with their ticket counts."""
    parents, unread = dict(parents or {}), dict(unread or {})
    by_goal: Dict[str, List[str]] = {}
    states: Dict[str, str] = {}
    flying: Dict[str, int] = {}
    for row in rows:
        if row.get("goal"):
            ref = "%s#%s" % (row.get("repo"), row.get("number"))
            by_goal.setdefault(row["goal"], []).append(ref)
            states[ref] = str(row.get("state") or "")
            if row.get("open", True) and row.get("state") in IN_FLIGHT_STATES:
                flying[row["goal"]] = flying.get(row["goal"], 0) + 1
    for goal in list(by_goal):
        seen = {goal}
        while parents.get(goal) and parents[goal] not in seen:
            goal = parents[goal]
            seen.add(goal)
            by_goal.setdefault(goal, [])
    notes, proofs = _index(comments)
    limit = brain.unverified_limit()

    def unread_for(refs: Iterable[str]) -> Optional[str]:
        reasons = sorted({unread[ref.partition("#")[0]] for ref in refs if ref.partition("#")[0] in unread})
        return "; ".join(reasons) or None

    out = {goal: _goal_record(titles.get(goal), notes.get(goal, []), tickets, states,
                             proofs, limit, now, unread_notes=unread_for([goal]),
                             unread_proofs=unread_for(tickets), in_flight=flying.get(goal, 0),
                             parent=parents.get(goal))
           for goal, tickets in by_goal.items()}
    for goal, record in out.items():
        record["epics"] = epic_rows(goal, rows, epics or {})
    factory = _factory(metric_comments, now) if metrics_goal in out else None
    if factory:
        out[metrics_goal].update(factory)
        out[metrics_goal].pop("metric_reason", None)
    return out


def epic_rows(goal: str, rows: Sequence[Dict[str, Any]],
              epics: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The goal's epics the tickets name, open first then by number: each
    with its title, open flag, ticket count and tickets in flight."""
    out: Dict[str, Dict[str, Any]] = {}
    for ref, info in epics.items():
        if info.get("goal") == goal:
            out[ref] = {"ref": ref, "title": info.get("title"), "open": info.get("open", True),
                        "tickets": 0, "in_flight": 0}
    for row in rows:
        found = out.get(row.get("epic") or "")
        if found is None:
            continue
        found["tickets"] += 1
        if row.get("open", True) and row.get("state") in IN_FLIGHT_STATES:
            found["in_flight"] += 1

    def number(ref: str) -> int:
        tail = ref.rpartition("#")[2]
        return int(tail) if tail.isdigit() else 0
    return sorted(out.values(), key=lambda e: (not e["open"], number(e["ref"])))
