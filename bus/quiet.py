"""When a goal is quiet since its last report, and so owed no new one.

Every new comment on a goal home emails the goal's author. A report every 6
hours on a goal where nothing happened said "nothing moved, nothing needs
you" four times a day, by email (loom#1534). A quiet goal is owed no report;
once its last report is HEARTBEAT_EVERY old it gets one heartbeat, which edits
that report in place: GitHub sends no email for an edit, and the station
forwards only new report comments to the operator's phone.

The rule reads only data `bus.brain status` already has: the goal's tickets
(goal_tree), the open invariant breaches that name it, and the goal home's
comments. It never reads a report's prose. A goal is quiet since its newest
report when all of these hold:

- no ticket in its scope was opened or closed after that report, and none
  carrying loom:merged, loom:shipped, loom:verified or loom:failed was
  updated after it;
- no open invariant breach names the goal (or sits in its scope);
- no open ticket in its scope asks the operator (needs:human, needs-operator);
- nothing in its scope is stuck: no open ticket is loom:failed, loom:runaway,
  loom:regression or needs-brain, and none has sat in loom:pr past the stuck
  age;
- the numbers on its newest Measure: line (journal ticks and reports; dates
  and times left out) are the numbers it had when that report went out.

A goal with no report yet is never quiet: its first report is owed.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence

from . import handoff
from .labels import HUMAN_LABELS
from .github import rest_login

JOURNAL_MARKER = "<!-- loom:journal v1 -->"
REPORT_MARKER = "<!-- loom:report v1 -->"
# A quiet goal's one heartbeat per this long, edited into its last report.
HEARTBEAT_EVERY = 7 * 24 * 3600
# The last line of a heartbeat: the report is edited in place, never reposted.
HEARTBEAT_STAMP = "Still quiet as of %s; this report is edited in place until something moves."
MOVED_LABELS = ("loom:merged", "loom:shipped", "loom:verified", "loom:failed")
STUCK_LABELS = ("loom:failed", "loom:runaway", "loom:regression", "needs-brain")
ASK_LABELS = HUMAN_LABELS
INVARIANT_LABEL = "loom:invariant"
_MEASURE = re.compile(r"^-?[ \t]*Measure:[ \t]*(.*)$", re.I | re.M)
# Dates and clock times on a Measure line say when, not how much.
_WHEN = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?Z?)?|\b\d{1,2}:\d{2}(?::\d{2})?Z?")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


class Quiet(NamedTuple):
    """A quiet goal: `since` is its newest report's creation time, `touched`
    the newest edit of any of its reports (a heartbeat moves it)."""
    since: datetime
    touched: datetime

    def heartbeat_due(self, now: datetime) -> bool:
        return (now - self.touched).total_seconds() >= HEARTBEAT_EVERY

    def next_heartbeat(self) -> datetime:
        return self.touched + timedelta(seconds=HEARTBEAT_EVERY)


def when(value: Any) -> str:
    """A UTC time as GitHub writes it."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _at(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _labels(issue: Dict[str, Any]) -> set:
    return {str((x or {}).get("name") or "") if isinstance(x, dict) else str(x)
            for x in issue.get("labels") or []}


def rows(comments: Iterable[Any], authors: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Goal home comments, REST or GraphQL, as dicts with login, at (created),
    updated, body and id; a journal edited in place counts once per tick at
    the tick's time. With `authors`, only their comments: only theirs reach
    the operator's phone, as in brain.goal_ledger."""
    allowed = {a.lower() for a in authors} if authors else None
    plain = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        login = (str((c.get("user") or {}).get("login") or "") if "user" in c
                 else rest_login(c.get("author")))
        plain.append({"login": login.lower(), "at": c.get("created_at") or c.get("createdAt"),
                      "updated": c.get("updated_at") or c.get("updatedAt"),
                      "body": str(c.get("body") or ""), "id": c.get("id")})
    out = []
    for c in handoff.expand_journals(plain, at_key="at"):
        if _at(c.get("at")) is None or (allowed is not None and c["login"] not in allowed):
            continue
        out.append(c)
    return out


def _numbers(line: str) -> List[str]:
    return _NUMBER.findall(_WHEN.sub(" ", line))


def _measure(body: str) -> Optional[str]:
    if not body.startswith((JOURNAL_MARKER, REPORT_MARKER)):
        return None
    found = _MEASURE.search(body)
    value = found.group(1).strip() if found else ""
    return value if value and value.lower() != "none" else None


def _metric_moved(found: List[Dict[str, Any]], since: datetime) -> Optional[str]:
    """Why the goal's metric line changed after `since`, else None."""
    before: Optional[str] = None
    after: Optional[str] = None
    for c in sorted(found, key=lambda c: str(c["at"])):
        value = _measure(c["body"])
        if value is None:
            continue
        if _at(c["at"]) <= since:
            before = value
        else:
            after = value
    if after is not None and _numbers(after) != _numbers(before or ""):
        return "its metric line changed: %s" % " ".join(after.split())[:120]
    return None


def _news(issue: Dict[str, Any], since: datetime) -> List[str]:
    """What happened to one ticket after `since`: opened, closed, or a
    MOVED_LABELS ticket updated."""
    n, names = issue.get("number"), _labels(issue)
    created, closed = _at(issue.get("created_at")), _at(issue.get("closed_at"))
    updated = _at(issue.get("updated_at"))
    out = []
    if created is not None and created > since:
        out.append("#%s opened" % n)
    if closed is not None and closed > since:
        out.append("#%s closed" % n)
    hit = [x for x in MOVED_LABELS if x in names]
    if hit and updated is not None and updated > since:
        out.append("#%s %s" % (n, hit[0][len("loom:"):]))
    return out


def _standing(issue: Dict[str, Any], now: datetime, stuck_age: float) -> List[str]:
    """What one open ticket still holds: a breach, an ask, a stuck state.
    A closed ticket holds nothing."""
    if issue.get("state", "open") != "open":
        return []
    n, names = issue.get("number"), _labels(issue)
    out = []
    if INVARIANT_LABEL in names:
        out.append("invariant breach #%s is open" % n)
    if names & set(ASK_LABELS):
        out.append("#%s asks the operator" % n)
    stuck = [x for x in STUCK_LABELS if x in names]
    created = _at(issue.get("created_at"))
    if stuck:
        out.append("#%s is %s" % (n, stuck[0]))
    elif "loom:pr" in names and created is not None \
            and (now - created).total_seconds() > stuck_age:
        out.append("#%s is stuck in loom:pr" % n)
    return out


def moved(children: Iterable[Any], breaches: Iterable[Any], found: List[Dict[str, Any]],
          since: datetime, now: datetime) -> List[str]:
    """Each reason the goal is not quiet since `since`, empty when it is."""
    from .factory import STUCK_AGE  # factory imports brain, which imports this module
    out = ["invariant breach #%s is open" % issue.get("number") for issue in breaches
           if isinstance(issue, dict) and issue.get("state", "open") == "open"]
    for issue in children:
        if isinstance(issue, dict):
            out += _news(issue, since) + _standing(issue, now, STUCK_AGE)
    metric = _metric_moved(found, since)
    if metric:
        out.append(metric)
    return out


def last_report(found: List[Dict[str, Any]]) -> Optional[Quiet]:
    """(newest report's creation, newest edit of any report) among `found`
    (rows), or None when the goal has no report."""
    reports = [c for c in found if c["body"].startswith(REPORT_MARKER)]
    if not reports:
        return None
    since = max(_at(c["at"]) for c in reports)
    touched = max(max(_at(c["at"]), _at(c.get("updated")) or _at(c["at"])) for c in reports)
    return Quiet(since, touched)


def quiet(comments: Iterable[Any], children: Iterable[Any], breaches: Iterable[Any],
          now: datetime, authors: Optional[Sequence[str]] = None) -> Optional[Quiet]:
    """The goal's Quiet when nothing moved since its newest report (the rule
    in this module's docstring), else None. `comments` are the goal home's,
    `children` its tickets (brain.goal_children), `breaches` the open
    loom:invariant issues that name it."""
    found = rows(comments, authors)
    last = last_report(found)
    if last is None:
        return None
    return None if moved(list(children), list(breaches), found, last.since, now) else last


def heartbeat(text: str, now: datetime) -> str:
    """A report's text with the heartbeat line, for an edit in place."""
    if not isinstance(text, str) or not isinstance(now, datetime):
        raise TypeError("heartbeat needs the report text and a datetime")
    kept = [line for line in text.rstrip("\n").splitlines()
            if not line.startswith(HEARTBEAT_STAMP.split("%")[0])]
    return "\n".join(kept) + "\n" + HEARTBEAT_STAMP % now.strftime("%Y-%m-%dT%H:%MZ")
