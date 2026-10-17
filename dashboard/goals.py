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
_MEASURE = re.compile(r"^-?[ \t]*Measure:[ \t]*(.+)$", re.I | re.M)
_FACTORY = re.compile(r"^<!-- loom:metrics v1 -->\n(\d{4}-\d{2}-\d{2}): (\d)/9 pass")
_SECTION_LINE = re.compile(r"^\s*(?:[-*]\s*)?(Going well|Not going well|Still stuck(?: since last report)?"
                           r"|Next)\s*:\s*(.*)$", re.I)
_SECTION_HEAD = re.compile(r"^#{2,4}\s*(Going well|Not going well|Still stuck(?: since last report)?"
                           r"|Next|Needs you)\s*$", re.I)
_KEYS = {"going well": "well", "not going well": "not well", "still stuck": "stuck",
         "still stuck since last report": "stuck", "next": "next", "needs you": "needs you"}
DAYS = 7


def issue_ref(comment: Dict[str, Any]) -> Optional[str]:
    """owner/name#N of the issue a comment belongs to, from its issue_url."""
    match = _ISSUE_URL.search(str(comment.get("issue_url") or ""))
    return "%s#%s" % match.groups() if match else None


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
    for comment in comments:
        ref, body = issue_ref(comment), str(comment.get("body") or "")
        if not ref:
            continue
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
                 limit: int, now: datetime) -> Dict[str, Any]:
    ordered = sorted(notes, key=lambda c: str(c.get("created_at") or ""))
    reports = [c for c in ordered if str(c.get("body") or "").startswith(REPORT_MARKER)]
    newest = reports[-1] if reports else None
    history, before = _verified(tickets, proofs, now)
    return {
        "title": title,
        "report": short(headline(str(newest.get("body") or "")) or "", 220) if newest else None,
        "report_time": newest.get("created_at") if newest else None,
        "measure": _latest_measure(ordered),
        "metric": str(sum(history)), "unit": "verified with proof, last 7 days",
        "previous": str(before), "history": history, "scale": None,
        "unproven": _unproven(tickets, states), "unproven_limit": limit,
    }


def goal_context(rows: Sequence[Dict[str, Any]], comments: Sequence[Dict[str, Any]],
                 titles: Dict[str, Optional[str]], metric_comments: Sequence[Dict[str, Any]],
                 metrics_goal: Optional[str], now: datetime) -> Dict[str, Dict[str, Any]]:
    """One record per goal the tickets name (their `Part of` line)."""
    by_goal: Dict[str, List[str]] = {}
    states: Dict[str, str] = {}
    for row in rows:
        if row.get("goal"):
            ref = "%s#%s" % (row.get("repo"), row.get("number"))
            by_goal.setdefault(row["goal"], []).append(ref)
            states[ref] = str(row.get("state") or "")
    notes, proofs = _index(comments)
    limit = brain.unverified_limit()
    out = {goal: _goal_record(titles.get(goal), notes.get(goal, []), tickets, states,
                             proofs, limit, now)
           for goal, tickets in by_goal.items()}
    factory = _factory(metric_comments, now) if metrics_goal in out else None
    if factory:
        out[metrics_goal].update(factory)
    return out
