"""One trusted reading of a ticket's comments: its history (loom#2457).

Ticket state is stored as text in issue comments, then re-derived by many
small parsers. This module is the single home for that reading: it takes a
ticket's raw comments, applies trust filtering once on the way in
(`read_history`, `trusted_comments`), and returns the structured history:
attempts, rung, tried rungs, fix rounds and the dead lane. It also owns the
writing of the markers it parses (`rung_marker`, `equivalent_marker`), so
format and parser stay in one file and cannot drift apart.

The small helpers here (`rung_of`, `tried_rungs`, `attempts`, `fixes_of`,
`dead_lane` and their supporting patterns) moved from bus/factory.py, which
now delegates to this module; their behavior is unchanged, pinned by the
characterization tests in tests/test_bus_history.py at this interface.
Callers that already hold trusted comments may use the helpers directly;
callers holding raw comments use `read_history`, the one path that applies
the filter, instead of repeating the filtering rules at each call site.

This module imports nothing from bus.brain, bus.factory or bus.station,
so moving the reading here cuts one edge of the circular imports between
those components: they may import this module at the top level.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .github import rest_login

# The marker formats this module owns: it writes them and parses them.
RUNG_MARKER = "<!-- loom:rung %d -->"
RUNG_RE = re.compile(r"<!-- loom:rung (\d+) -->")
EQUIVALENT_MARKER = "<!-- loom:equivalent %s -->"
EQUIVALENT_RE = re.compile(r"<!-- loom:equivalent (\S+) -->")
FIX_MARKER = "<!-- loom:fix v1 -->"
CLAIM_RE = re.compile(r"^Claimed for lane ")
CLAIMED_RE = re.compile(r"Claimed for lane ([a-z0-9-]+) \(body sha256 [0-9a-f]{12}\)")
FAILED_RE = re.compile(r"^Lane \S+ failed: ")
RETRY_ENDED_RE = re.compile(r"^Automatic retry [0-9]+ of [0-9]+ \([a-z]+\): moved from lane "
                            r"\S+ to lane \S+\. Lane \S+ failed: ")
HELD_RE = re.compile(r"^Lane \S+ held: ")
STOPPED_RE = re.compile(r"^Lane \S+ stopped: (?=\[(?:limit|auth|transient)\] )")
# The comments that assign the ticket to a lane: a brief. Claims after the
# newest of these ran on the lane the ticket was last briefed to.
BRIEF_RES = (
    re.compile(r"^The station gave this queued ticket lane ([a-z0-9-]+) \(tier [a-z]+\)\.$"),
    re.compile(r"the factory moved this ticket to lane ([a-z0-9-]+),"),
    re.compile(r"moved from lane [a-z0-9-]+ to lane ([a-z0-9-]+)\."),
    re.compile(r"the factory moved this ticket up the model ladder to rung \d+, \S+ on lane ([a-z0-9-]+)\."),
)


def rung_marker(rung: int) -> str:
    """The comment text that records a ticket's rung."""
    return RUNG_MARKER % rung


def equivalent_marker(model: str) -> str:
    """The comment text that records an equivalent-model move."""
    return EQUIVALENT_MARKER % model


def _ts(value: Any) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


ts_of = _ts


def trusted_authors(env: Optional[Dict[str, str]] = None) -> List[str]:
    """LOOM_BRIEF_AUTHORS: the logins whose comments are trusted markers."""
    raw = (os.environ if env is None else env).get("LOOM_BRIEF_AUTHORS", "")
    return raw.replace(",", " ").split()


def comment_login(comment: Any) -> str:
    """The login that wrote a comment, in the form LOOM_BRIEF_AUTHORS uses:
    REST's user.login, else GraphQL's author.login (rest_login)."""
    if not isinstance(comment, dict):
        return ""
    user = comment.get("user")
    if isinstance(user, dict) and user.get("login"):
        return str(user.get("login"))
    author = comment.get("author")
    return rest_login(author if isinstance(author, dict) else None)


def trusted_comments(comments: Any, authors: Optional[Sequence[str]] = None
                     ) -> List[Dict[str, Any]]:
    """The comments a trusted login wrote (case-insensitive), in order;
    `authors` defaults to trusted_authors(). Anyone can comment on a public
    repo, so every other comment is data, never a marker."""
    trusted = {a.lower() for a in (trusted_authors() if authors is None else authors) if a}
    return [c for c in comments or ()
            if isinstance(c, dict) and comment_login(c).lower() in trusted]


def rung_of(comments: Sequence[Dict[str, Any]]) -> int:
    """The rung of the ticket's latest rung marker, 0 when none. A marker of
    0 (posted by the brain or the operator) starts the ladder again."""
    found = [int(m.group(1)) for c in comments
             for m in RUNG_RE.finditer(str(c.get("body") or ""))]
    return found[-1] if found else 0


def last_climb(comments: Sequence[Dict[str, Any]]) -> float:
    """When the ticket last moved on the ladder (its newest rung marker), 0
    when never."""
    return max((_ts(c.get("created_at")) for c in comments
                if RUNG_RE.search(str(c.get("body") or ""))), default=0.0)


def equivalent_of(comments: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The model of the ticket's latest equivalent move, or None."""
    found = [m.group(1) for c in comments
             for m in EQUIVALENT_RE.finditer(str(c.get("body") or ""))]
    return found[-1] if found else None


def fixes_of(comments: Sequence[Dict[str, Any]]) -> int:
    return sum(1 for c in comments if str(c.get("body") or "").startswith(FIX_MARKER))


def tried_rungs(comments: Sequence[Dict[str, Any]]) -> set:
    """The rungs the ticket has run on since its ladder last started (the
    newest rung 0 marker). A rung counts once a claim follows its marker: a
    rung skipped because its lanes were busy, or one the ticket sat briefed
    on with no lane claiming it, is not tried. An equivalent move
    (EQUIVALENT_MARKER) leaves its rung untried: the claim after it runs
    the equivalent model."""
    tried: set = set()
    pending: set = set()
    just_claimed: set = set()
    for c in comments:
        body = str(c.get("body") or "")
        if CLAIM_RE.match(body):
            just_claimed = set(pending)
            tried |= pending
            pending = set()
        for m in RUNG_RE.finditer(body):
            rung = int(m.group(1))
            if rung == 0:
                tried, pending, just_claimed = set(), set(), set()
            else:
                pending = pending | {rung}
        if EQUIVALENT_RE.search(body):
            pending = set()
        if re.search(r"^Lane \S+ stopped: \[limit\]", body):
            tried -= just_claimed
            just_claimed = set()
    return tried


def _ending(body: str) -> Optional[str]:
    """The classed reason a comment ends the attempt before it with, or None
    when it ends none: a "Lane X failed:" line or one of the other endings
    (RETRY_ENDED_RE, HELD_RE, STOPPED_RE)."""
    for rx in (FAILED_RE, RETRY_ENDED_RE, STOPPED_RE):
        match = rx.match(body)
        if match:
            return body[match.end():]
    held = HELD_RE.match(body)
    return "[factory] " + body[held.end():] if held else None


def _is_brief(body: str) -> bool:
    """Whether the comment assigns the ticket to a lane: a brief."""
    return any(rx.search(body) for rx in BRIEF_RES)


def _attempt_rows(comments: Sequence[Any], reset_at: Any = None) -> List[List[Any]]:
    """Each claim as [epoch, time, lane, reason it failed], oldest first, from
    the claim comments and the endings that followed them (_ending). An
    attempt takes the first ending after it, or "" when none was recorded.
    With `reset_at` given, a comment it matches starts a new segment: only the
    last segment's claims are returned."""
    found: List[List[Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "").strip() if isinstance(comment, dict) else ""
        if reset_at is not None and reset_at(body):
            found = []
            continue
        claim = CLAIMED_RE.fullmatch(body)
        at = _ts(comment.get("created_at")) if claim else 0.0
        if claim and at:
            found.append([int(at), str(comment["created_at"]), claim.group(1), ""])
        ending = _ending(body)
        if ending and found and not found[-1][3]:
            found[-1][3] = ending
    return found


def attempts(comments: Sequence[Any], current: str = "") -> List[Tuple[int, str, str, str]]:
    """Each claim as (epoch, time, lane, reason it failed), oldest first, from
    the claim comments and their endings (_attempt_rows). An attempt takes the
    ending that followed it, or "" when none was recorded; with `current`
    given, the last attempt is the one being reported and takes that reason."""
    found = _attempt_rows(comments)
    if found and current:
        found[-1][3] = current
    return [(a[0], a[1], a[2], a[3]) for a in found]


def attempts_since_brief(comments: Sequence[Any]) -> List[Tuple[int, str, str, str]]:
    """The attempts after the ticket's last brief (its last lane assignment),
    oldest first, each with the failure that followed it. A ticket briefed
    with no assignment comment keeps every claim."""
    return [(a[0], a[1], a[2], a[3]) for a in _attempt_rows(comments, reset_at=_is_brief)]


def dead_lane(comments: Sequence[Any]) -> Optional[str]:
    """The one lane every attempt since the ticket's last brief ran on, or
    None when the attempts spread across lanes or there are none."""
    lanes = {lane for _, _, lane, _ in attempts_since_brief(comments)}
    return next(iter(lanes)) if len(lanes) == 1 else None


class History(NamedTuple):
    """A ticket's history, read once from its trusted comments."""

    rung: int
    tried_rungs: set
    fixes: int
    attempts: List[Tuple[int, str, str, str]]
    attempts_since_brief: List[Tuple[int, str, str, str]]
    dead_lane: Optional[str]
    last_climb: float
    equivalent: Optional[str]


def read_history(comments: Any, authors: Optional[Sequence[str]] = None) -> History:
    """The ticket's history from its raw comments: trust filtering is
    applied once here, on the way in, so no caller can skip it."""
    trusted = trusted_comments(comments, authors)
    return History(
        rung=rung_of(trusted),
        tried_rungs=tried_rungs(trusted),
        fixes=fixes_of(trusted),
        attempts=attempts(trusted),
        attempts_since_brief=attempts_since_brief(trusted),
        dead_lane=dead_lane(trusted),
        last_climb=last_climb(trusted),
        equivalent=equivalent_of(trusted),
    )
