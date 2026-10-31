"""Whether an ask for a person still needs that person (loom#2261, loom#2278).

An ask carries needs:human until the answer is in the ticket. Nothing took
the label off when an answer landed, so the digest asked again for what was
already done. state() reads one ticket and says which of three it is:

- ANSWERED: after the ask, an operator or trusted person left a comment that
  is not a question, a dashboard answer was posted, a trusted brain wrote a
  line starting "Finished:" or "Done:", or the body records a Decision.
- WAITING: a wait:* label, or a Blocked on section naming an open issue.
  A person cannot act on it today. It stays on the dashboard.
- OPEN: anything else. A ticket whose ask time cannot be read stays OPEN: a
  missed ask costs more than one asked twice.

The pure part is here; the station reads the comments and events and writes
the labels (bus/label_pass.py).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import answer, decision_claim
from .labels import HUMAN_LABELS, WAIT_PREFIX

ANSWERED, WAITING, OPEN = "answered", "waiting", "open"
# How a trusted brain records the step the ask named as done.
_FINISHED = re.compile(r"^\s*(?:finished|done)\s*:", re.I | re.M)
# A decision that names a command: someone must still run it.
_COMMAND = re.compile(r"`[^`\n]+`")


def _names(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") if isinstance(x, dict) else x) for x in issue.get("labels") or []]


def asked_at(comments: Sequence[Any], events: Sequence[Any]) -> Optional[datetime]:
    """When the ask now open was made: the newest ask comment, or the moment
    an ask label went on a ticket holding none. A label swapped for another
    ask label (the migration to needs:human) does not restart the ask.
    `events` are oldest first."""
    held: set = set()
    at: Optional[datetime] = None
    for event in events:
        if not isinstance(event, dict) or event.get("event") not in ("labeled", "unlabeled"):
            continue
        name = str((event.get("label") or {}).get("name") or "")
        if name not in HUMAN_LABELS:
            continue
        if event["event"] == "unlabeled":
            held.discard(name)
            continue
        if not held:
            at = decision_claim.when(event.get("created_at"))
        held.add(name)
    asked = decision_claim.latest_ask(comments)
    return max(x for x in (at, asked) if x is not None) if at or asked else None


def _answers(comment: Dict[str, Any], people: Iterable[str], trusted: Sequence[str],
             ops: Sequence[str]) -> bool:
    body = str(comment.get("body") or "").strip()
    if answer.trusted_answer(comment, trusted, ops):
        return True
    login = str((comment.get("user") or {}).get("login") or "").lower()
    if decision_claim.person_comment(comment) and login in people:
        return not body.endswith("?")
    return login in {str(t).lower() for t in trusted} and bool(_FINISHED.search(body))


def answer_of(issue: Dict[str, Any], comments: Sequence[Any], events: Sequence[Any],
              trusted: Iterable[str], ops: Iterable[str]) -> Optional[str]:
    """The text that answers the open ask ("" for a Decision in the body), or
    None when nothing does."""
    trusted, ops = list(trusted), list(ops)
    since = asked_at(comments, events)
    if since is None:
        return None
    people = set(decision_claim.person_logins(trusted, ops))
    if decision_claim.body_decision(issue.get("body"), since, people):
        return ""
    for comment in reversed([c for c in comments if isinstance(c, dict)]):
        stamp = decision_claim.when(comment.get("created_at"))
        if stamp is not None and stamp > since and _answers(comment, people, trusted, ops):
            return str(comment.get("body") or "")
    return None


def state(issue: Dict[str, Any], comments: Sequence[Any], events: Sequence[Any],
          trusted: Iterable[str], ops: Iterable[str], blockers: Iterable[int] = ()) -> str:
    """ANSWERED, WAITING or OPEN. `blockers` are the open issues its Blocked
    on section names; the caller reads them."""
    if answer_of(issue, comments, events, trusted, ops) is not None:
        return ANSWERED
    if list(blockers) or any(n.startswith(WAIT_PREFIX) for n in _names(issue)):
        return WAITING
    return OPEN


def names_command(text: str) -> bool:
    """The answer names a command, so the ticket goes to who can run it."""
    return bool(_COMMAND.search(text))
