"""The station referees engine claims: one claim per ticket, idle claims
expire (loom#2277).

Several engines work the same backlog, each with one claim label
(ENGINE_CLAIMS, or LOOM_ENGINE_CLAIMS). GitHub has no conditional write, so
two engines could claim one ticket, and a claim held by a stopped engine
blocked its ticket for good. The station's referee_claims step reads each
repo's open issues carrying an engine claim at most once every EVERY seconds
and, per issue:

- keeps the engine claim put on first when there are two or more, judged by
  each label's newest labeled event on the timeline, and removes the others
  with a short status note;
- removes the engine claim it keeps when nothing on the ticket is newer than
  IDLE seconds: no event by anyone but the station, no update of an open pull
  request that names it, and not the claim's own putting-on. A comment line
  `waiting: <what> until <time>` by anyone but the station holds the claim
  until that time plus GRACE.

Any claim label that is not an engine claim is a person's and is never read
or touched. A timeline is read only for an issue with two engine claims or
one not updated within IDLE, so a pass costs a few list reads per repo.

The first run over the claims that already exist is the first pass: at most
FIRST_PER_RUN issues a run and no note on any of them, until a run finds
nothing more to remove. After it, at most PER_RUN issues a run. Expiry never
posts a note: a label removal sends no email and the timeline records it.

plan() is pure: the station reads, plan() decides, the station writes.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from .stale_claims import CLAIM_PREFIX, _login, _stamp

ENGINE_CLAIMS = ("claim:claude-code", "claim:muse", "claim:dot", "claim:ccqw")
IDLE = 2 * 3600
GRACE = 30 * 60
EVERY = 1800
PER_RUN = 10
FIRST_PER_RUN = 5
# Timelines read a run, across repos.
READS_PER_RUN = 40
STATE_FILE = "claim-referee.json"

_WAIT_RE = re.compile(r"^\s*waiting:\s*\S.*?\s+until\s+(.+?)\s*$", re.I | re.M)
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Removal(NamedTuple):
    label: str
    why: str
    duplicate: bool    # a second engine claim, not an expiry


def mode(raw: str) -> str:
    """LOOM_CLAIM_REFEREE as "on" (the default), "dry" or "off"."""
    value = (raw or "").strip().lower()
    if value in ("0", "off", "false", "no"):
        return "off"
    return "dry" if value == "dry" else "on"


def engines(raw: str) -> Tuple[str, ...]:
    """LOOM_ENGINE_CLAIMS: the claim:* words, lowercased; else ENGINE_CLAIMS."""
    found = tuple(w.lower() for w in (raw or "").split() if w.lower().startswith(CLAIM_PREFIX))
    return found or ENGINE_CLAIMS


def engine_claims(issue: Dict[str, Any], engine_labels: Iterable[str]) -> List[str]:
    """The engine claim labels on the issue, as the issue spells them."""
    wanted = {x.lower() for x in engine_labels}
    return [str(x.get("name") or "") for x in issue.get("labels") or []
            if isinstance(x, dict) and str(x.get("name") or "").lower() in wanted]


def needs_timeline(issue: Dict[str, Any], engine_labels: Iterable[str], now: float) -> bool:
    """The issue may be due: two engine claims, or one on an issue not
    updated within IDLE (or with no readable update time)."""
    claims = engine_claims(issue, engine_labels)
    if len(claims) > 1:
        return True
    updated = _stamp(issue.get("updated_at"))
    return bool(claims) and (updated is None or now - updated >= IDLE)


def parse_until(text: str) -> Optional[float]:
    """The time in a wait line: an ISO date and time, with Z, UTC or an
    offset (none is UTC), or a bare date (its last second, UTC)."""
    value = text.strip().rstrip(".").strip()
    value = re.sub(r"\s*(?:UTC|GMT|Z)$", "+00:00", value, flags=re.I)
    if _DAY_RE.match(value):
        value += "T23:59:59+00:00"
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    if found.tzinfo is None:
        found = found.replace(tzinfo=timezone.utc)
    return found.timestamp()


def _put_on(timeline: Sequence[Dict[str, Any]], label: str) -> Optional[Tuple[float, int]]:
    """(time, position) of the label's newest labeled event, None if none."""
    found = None
    for index, event in enumerate(timeline):
        if (event.get("event") == "labeled"
                and str((event.get("label") or {}).get("name") or "").lower() == label.lower()):
            stamp = _stamp(event.get("created_at"))
            if stamp is not None:
                found = (stamp, index)
    return found


def _by_station(event: Dict[str, Any], station: set) -> bool:
    return _login(event.get("actor")) in station or _login(event.get("user")) in station


def wait_until(timeline: Sequence[Dict[str, Any]], station: Iterable[str]) -> Optional[float]:
    """The time the newest stated wait holds to, None when no comment by
    anyone but the station states one."""
    logins = {x.lower() for x in station}
    found = None
    for event in timeline:
        if event.get("event") != "commented" or _by_station(event, logins):
            continue
        for match in _WAIT_RE.finditer(str(event.get("body") or "")):
            until = parse_until(match.group(1))
            if until is not None:
                found = until
    return found


def last_activity(issue: Dict[str, Any], timeline: Sequence[Dict[str, Any]], label: str,
                  station: Iterable[str]) -> Optional[float]:
    """The newest real activity on the ticket: an event by anyone but the
    station, an update of an open pull request naming it, the claim label's
    putting-on, or the issue's creation."""
    logins = {x.lower() for x in station}
    seen = [_stamp(e.get("created_at")) for e in timeline if not _by_station(e, logins)]
    for event in timeline:
        source = event.get("source") if isinstance(event.get("source"), dict) else {}
        pull = source.get("issue") if isinstance(source.get("issue"), dict) else {}
        if (event.get("event") == "cross-referenced" and "pull_request" in pull
                and pull.get("state") == "open"):
            seen.append(_stamp(pull.get("updated_at")))
    put = _put_on(timeline, label)
    seen += [put[0] if put else None, _stamp(issue.get("created_at"))]
    known = [t for t in seen if t is not None]
    return max(known) if known else None


def plan(issue: Dict[str, Any], timeline: Iterable[Any], now: float,
         engine_labels: Iterable[str], station: Iterable[str]) -> List[Removal]:
    """The engine claims to remove from the issue. Two or more: all but the
    one put on first. The one kept: when idle IDLE seconds and no stated wait
    holds. A claim whose putting-on the timeline does not show leaves the
    issue for a later pass."""
    events = [e for e in timeline if isinstance(e, dict)]
    claims = engine_claims(issue, engine_labels)
    if not claims:
        return []
    put = {label: _put_on(events, label) for label in claims}
    if len(claims) > 1 and any(v is None for v in put.values()):
        return []
    keep = min(claims, key=lambda label: put[label] or (0.0, 0))
    removed = [Removal(label, "a second engine claim: %s was put on first" % keep, True)
               for label in claims if label != keep]
    until = wait_until(events, station)
    if until is not None and now < until + GRACE:
        return removed
    last = last_activity(issue, events, keep, station)
    if last is not None and now - last < IDLE:
        return removed
    return removed + [Removal(keep, "no activity in %d hours and no stated wait"
                              % (IDLE // 3600), False)]
