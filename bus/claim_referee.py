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
  IDLE seconds: no ACTIVITY event by the claimer or a trusted login and not
  the claim's own putting-on. An open pull request one of them wrote, in the
  same repo, that one of them referenced from the ticket holds the claim for
  MAX_PULL_HOLD after that reference: updated_at moves on anyone's comment, so
  it is no measure of work, and an abandoned pull request must not pin a
  ticket. stale_claims.stale keeps its own looser rule for operators' claims,
  which a pull request of any age or repo holds. A comment line `waiting: <what> until <time>` by the claimer or
  a trusted login holds the claim until that time plus GRACE, and the time
  counts for at most MAX_WAIT after the comment. The repos take comments from
  strangers: nobody else can keep a claim alive, or end a stated wait.

The claimer is the actor of the claim's newest labeled event. The station's
own logins are never the claimer or trusted: its relabels are not work.

An issue with a lane run, an issue assigned to a person, and an issue with no
Loom label (stale_claims.loom_managed: an operator's own session, which
bus/claimed.py settles) never expire; a duplicate is still removed. Any claim
label that is not an engine claim is a person's and is never read or touched.
A timeline is read for every issue with two engine claims or one that may
expire, whatever its updated_at, which a stranger's comment moves. All repos
share one due list and at most READS_PER_RUN a run; the state file keeps the
place in it, so the next run reads on from where this one stopped.

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

from .stale_claims import CLAIM_PREFIX, _login, _made_by, _stamp, loom_managed, running

ENGINE_CLAIMS = ("claim:claude-code", "claim:muse", "claim:dot", "claim:ccqw")
IDLE = 2 * 3600
GRACE = 30 * 60
# A stated wait's time counts for at most this long after its comment.
MAX_WAIT = 24 * 3600
# A writer's open pull request holds the claim this long after its reference.
MAX_PULL_HOLD = 7 * 24 * 3600
EVERY = 1800
PER_RUN = 10
FIRST_PER_RUN = 5
# Timelines read a run, across repos.
READS_PER_RUN = 40
STATE_FILE = "claim-referee.json"

# Comment text is a stranger's input: only so much of it is scanned for waits.
MAX_BODY = 20000
MAX_WAIT_LINE = 300
WAIT_PREFIX = "waiting:"
# What a writer did. Not mentioned or subscribed, which others cause.
ACTIVITY = frozenset({"commented", "labeled", "unlabeled", "assigned", "closed", "reopened",
                      "committed", "cross-referenced"})

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Removal(NamedTuple):
    label: str
    why: str
    duplicate: bool    # a second engine claim, not an expiry


class PutOn(NamedTuple):
    at: float
    position: int
    actor: str


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


def needs_timeline(issue: Dict[str, Any], engine_labels: Iterable[str]) -> bool:
    """The issue may be due: two engine claims, or one that may expire."""
    claims = engine_claims(issue, engine_labels)
    return len(claims) > 1 or (bool(claims) and may_expire(issue))


def in_turn(due: Sequence[Any], turn: Any) -> Tuple[List[Any], int]:
    """`due` from the saved place `turn` on, wrapping, and the place it
    started from, which a run moves on by the reads it spent."""
    if not due:
        return [], 0
    start = turn % len(due) if isinstance(turn, int) and not isinstance(turn, bool) else 0
    return list(due[start:]) + list(due[:start]), start


def may_expire(issue: Dict[str, Any]) -> bool:
    """Not an operator's own session (no Loom label), no lane run in progress,
    and nobody but an App assigned."""
    assigned = [_login(x) for x in issue.get("assignees") or []]
    return (loom_managed(issue) and not running(issue)
            and not any(x and not x.endswith("[bot]") for x in assigned))


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


def stated_times(body: str) -> List[float]:
    """The readable times of the wait lines in a comment. The time is what
    follows the line's last `until`. No line over MAX_WAIT_LINE characters is
    read, and nothing past MAX_BODY."""
    found = []
    for line in body[:MAX_BODY].splitlines():
        text = line.strip()
        if len(line) > MAX_WAIT_LINE or text[:len(WAIT_PREFIX)].lower() != WAIT_PREFIX:
            continue
        words = text[len(WAIT_PREFIX):].split()
        marks = [i for i, word in enumerate(words) if word.lower() == "until"]
        if marks and marks[-1] > 0:
            until = parse_until(" ".join(words[marks[-1] + 1:]))
            if until is not None:
                found.append(until)
    return found


def _put_on(timeline: Sequence[Dict[str, Any]], label: str) -> Optional[PutOn]:
    """The label's newest labeled event, None if none."""
    found = None
    for index, event in enumerate(timeline):
        if (event.get("event") == "labeled"
                and str((event.get("label") or {}).get("name") or "").lower() == label.lower()):
            stamp = _stamp(event.get("created_at"))
            if stamp is not None:
                found = PutOn(stamp, index, _login(event.get("actor")))
    return found


def wait_until(timeline: Sequence[Dict[str, Any]], writers: Iterable[str]) -> Optional[float]:
    """The latest time a wait stated by one of `writers` holds to, each cut
    to MAX_WAIT after its comment; None when they state none. The latest, not
    the newest: a later line shortens nothing."""
    found: Optional[float] = None
    for event in timeline:
        said = _stamp(event.get("created_at"))
        if event.get("event") != "commented" or said is None or not _made_by(event, writers):
            continue
        for until in stated_times(str(event.get("body") or "")):
            held = min(until, said + MAX_WAIT)
            found = held if found is None else max(found, held)
    return found


def last_activity(issue: Dict[str, Any], timeline: Sequence[Dict[str, Any]], label: str,
                  writers: Iterable[str]) -> Optional[float]:
    """The newest real activity on the ticket: an ACTIVITY event by one of
    `writers`, the claim label's putting-on, or the issue's creation."""
    logins = set(writers)
    seen = [_stamp(e.get("created_at")) for e in timeline
            if e.get("event") in ACTIVITY and _made_by(e, logins)]
    put = _put_on(timeline, label)
    seen += [put.at if put else None, _stamp(issue.get("created_at"))]
    known = [t for t in seen if t is not None]
    return max(known) if known else None


def has_open_pull(issue: Dict[str, Any], timeline: Sequence[Dict[str, Any]],
                  writers: Iterable[str], now: float) -> bool:
    """A writer referenced an open pull request by a writer, in the issue's
    repo, within MAX_PULL_HOLD."""
    logins = set(writers)
    home = str(issue.get("repository_url") or "").lower()
    for event in timeline:
        source = event.get("source") if isinstance(event.get("source"), dict) else {}
        pull = source.get("issue") if isinstance(source.get("issue"), dict) else {}
        said = _stamp(event.get("created_at"))
        if (event.get("event") == "cross-referenced" and "pull_request" in pull
                and pull.get("state") == "open" and _login(event.get("actor")) in logins
                and _login(pull.get("user")) in logins
                and home and str(pull.get("repository_url") or "").lower() == home
                and said is not None and now - said < MAX_PULL_HOLD):
            return True
    return False


def plan(issue: Dict[str, Any], timeline: Iterable[Any], now: float,
         engine_labels: Iterable[str], station: Iterable[str],
         trusted: Iterable[str]) -> List[Removal]:
    """The engine claims to remove from the issue. Two or more: all but the
    one put on first. The one kept: when the issue may expire, it was idle
    IDLE seconds, and no stated wait or open pull request holds it. Only the claimer and `trusted`
    count, never `station`. A claim whose putting-on the timeline does not
    show leaves the issue for a later pass, however many claims it has."""
    events = [e for e in timeline if isinstance(e, dict)]
    claims = engine_claims(issue, engine_labels)
    put: Dict[str, PutOn] = {}
    for label in claims:
        found = _put_on(events, label)
        if found is None:
            return []
        put[label] = found
    if not claims:
        return []
    keep = min(claims, key=put.__getitem__)
    removed = [Removal(label, "a second engine claim: %s was put on first" % keep, True)
               for label in claims if label != keep]
    if not may_expire(issue):
        return removed
    writers = ({x.lower() for x in trusted} | {put[keep].actor}) - {x.lower() for x in station}
    writers.discard("")
    until = wait_until(events, writers)
    if until is not None and now < until + GRACE:
        return removed
    if has_open_pull(issue, events, writers, now):
        return removed
    last = last_activity(issue, events, keep, writers)
    if last is not None and now - last < IDLE:
        return removed
    return removed + [Removal(keep, "no activity in %d hours and no stated wait"
                              % (IDLE // 3600), False)]
