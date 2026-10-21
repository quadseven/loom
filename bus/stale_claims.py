"""Release another operator's claim once its claimer has left the issue alone.

An issue labelled claim:<login>, or assigned to that login, belongs to that
operator's Loom (tickets.owned_elsewhere), and this Loom never touches it. A
claim nobody works held its issue forever: the operator asked for claims left
idle to come back to this Loom (2026-10-21). The station's
release_stale_claims step reads each such open issue at most once an hour.
When neither the claim nor anything its claimer did on the issue (a comment, a
label, an assignment, a reference from their pull request) is newer than
LOOM_CLAIM_IDLE_HOURS (default 24; 0 turns the step off), and no open pull
request of theirs names the issue, the station removes the claim label and
unassigns the claimer. A label removal and an unassignment send no email; the
issue's timeline records who released it. The issue is then this Loom's
backlog like any other.

The same step lapses this Loom's own claim the same way (loom#1217). A ticket
carrying this Loom's claim:<login> label and a Loom label (loom:*, wait:*,
needs-brain) that has had no event on its timeline, no run (loom:running), and
no open pull request updated within LOOM_CLAIM_IDLE_HOURS loses the claim
label, and this operator's assignment, with a status note naming why. A claim
no lane works locked every other operator's Loom out of the ticket while its
claimer waited on a model limit or a brain.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

CLAIM_PREFIX = "claim:"
IDLE_HOURS = 24
EVERY = 3600
PER_RUN = 20
STATE_FILE = "stale-claims.json"


def idle_hours(raw: str) -> int:
    """LOOM_CLAIM_IDLE_HOURS: whole hours; 0 is off; anything else is the
    default."""
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else IDLE_HOURS


def _login(value: Any) -> str:
    return str(value.get("login") or "").lower() if isinstance(value, dict) else ""


def _stamp(value: Any) -> Optional[float]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _touched_by(event: Dict[str, Any], login: str) -> bool:
    """The claimer acted in this timeline event: wrote it, or referenced the
    issue from an issue or pull request they wrote."""
    if login in (_login(event.get("actor")), _login(event.get("user"))):
        return True
    source = (event.get("source") or {}).get("issue") if isinstance(event.get("source"), dict) else None
    return isinstance(source, dict) and _login(source.get("user")) == login


def _open_pull_of(event: Dict[str, Any], login: str) -> bool:
    """An open pull request by the claimer references the issue."""
    source = (event.get("source") or {}).get("issue") if isinstance(event.get("source"), dict) else None
    return (event.get("event") == "cross-referenced" and isinstance(source, dict)
            and "pull_request" in source and source.get("state") == "open"
            and _login(source.get("user")) == login)


def last_touch(timeline: Iterable[Any], login: str) -> Optional[float]:
    """When the claimer last acted on the issue, None when never."""
    login = login.lower()
    times = [_stamp(e.get("created_at") or e.get("updated_at")) for e in timeline
             if isinstance(e, dict) and _touched_by(e, login)]
    known = [t for t in times if t is not None]
    return max(known) if known else None


def _claimed_at(timeline: Iterable[Dict[str, Any]], label: str) -> Optional[float]:
    """When the claim label was last put on, by anyone."""
    times = [_stamp(e.get("created_at")) for e in timeline
             if e.get("event") == "labeled" and label
             and str((e.get("label") or {}).get("name") or "").lower() == label.lower()]
    known = [t for t in times if t is not None]
    return max(known) if known else None


def stale(issue: Dict[str, Any], timeline: Iterable[Any], login: str, label: str,
          now: float, idle_seconds: float) -> bool:
    """The claim is idle: no act of the claimer's on the issue, the claim
    label's last putting-on, nor the issue's creation is within idle_seconds,
    and no open pull request of theirs names it."""
    events = [e for e in timeline if isinstance(e, dict)]
    if any(_open_pull_of(e, login.lower()) for e in events):
        return False
    newest = [t for t in (last_touch(events, login), _claimed_at(events, label),
                          _stamp(issue.get("created_at"))) if t is not None]
    return bool(newest) and now - max(newest) >= idle_seconds


def claimers(issue: Dict[str, Any], ours: Iterable[str],
             operators: Iterable[str] = ()) -> Dict[str, str]:
    """{login: the claim label naming it, or "" when only assigned} for each
    other operator who owns the issue: a `claim:<login>` label not ours, or an
    assignment to one of `operators` (LOOM_TRUSTED_BRIEFERS) or to a login a
    claim label names. Empty when the issue is also assigned to anyone else:
    a person this Loom knows nothing of owns it, and it is never released."""
    mine = {x.lower() for x in ours}
    known = {x.lower() for x in operators}
    found: Dict[str, str] = {}
    for label in issue.get("labels") or []:
        name = str(label.get("name") or "") if isinstance(label, dict) else ""
        if name.lower().startswith(CLAIM_PREFIX):
            login = name[len(CLAIM_PREFIX):].strip().lower()
            if login and login not in mine:
                found[login] = name
    for person in issue.get("assignees") or []:
        login = _login(person)
        if not login or login in mine:
            continue
        if login not in known and login not in found:
            return {}
        found.setdefault(login, "")
    return found


def own_claims(issue: Dict[str, Any], ours: Iterable[str]) -> Dict[str, str]:
    """{login: the claim label naming it} for each claim:<login> label on the
    issue naming one of `ours` (this Loom's own logins)."""
    mine = {x.lower() for x in ours}
    found: Dict[str, str] = {}
    for label in issue.get("labels") or []:
        name = str(label.get("name") or "") if isinstance(label, dict) else ""
        if name.lower().startswith(CLAIM_PREFIX):
            login = name[len(CLAIM_PREFIX):].strip().lower()
            if login in mine:
                found[login] = name
    return found


def loom_managed(issue: Dict[str, Any]) -> bool:
    """The issue carries a label the Loom puts on its tickets (loom:*, wait:*,
    needs-brain). A claim with none is an operator's own session's, which the
    claimed-issue settle (bus/claimed.py) handles; it never lapses here."""
    names = [str(x.get("name") or "").lower() for x in issue.get("labels") or []
             if isinstance(x, dict)]
    return any(n.startswith(("loom:", "wait:")) or n == "needs-brain" for n in names)


def running(issue: Dict[str, Any]) -> bool:
    """A lane run for the issue is in progress."""
    return any(isinstance(x, dict) and str(x.get("name") or "").lower() == "loom:running"
               for x in issue.get("labels") or [])


def lapsed(issue: Dict[str, Any], timeline: Iterable[Any], pull_updates: Iterable[Any],
           label: str, now: float, idle_seconds: float) -> bool:
    """This Loom's own claim is not being worked: the issue is Loom-managed
    and no run is in progress, and nothing on its timeline (any event, the
    claim label's last putting-on among them), no open pull request naming
    it (`pull_updates`: their updated_at stamps) and not its creation is
    within idle_seconds."""
    if not loom_managed(issue) or running(issue):
        return False
    events = [e for e in timeline if isinstance(e, dict)]
    seen = [_stamp(e.get("created_at")) for e in events]
    seen += [_stamp(x) for x in pull_updates]
    seen += [_claimed_at(events, label), _stamp(issue.get("created_at"))]
    known = [t for t in seen if t is not None]
    return bool(known) and now - max(known) >= idle_seconds
