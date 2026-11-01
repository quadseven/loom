"""Whether an ask for a person still needs that person (loom#2261, loom#2278).

An ask carries needs:human until the answer is in the ticket. Nothing took
the label off when an answer landed, so the digest asked again for what was
already done. state() reads one ticket and says which of three it is:

- ANSWERED: after the ask, an operator or trusted person left a comment that
  is not a question, a dashboard answer was posted, a trusted brain wrote a
  line starting "Finished:" or "Done:" or a dated "Operator answer
  YYYY-MM-DD:" line (the date on or after the ask's day), or the body
  records a Decision. A LOOM_ASK_ANSWERERS login's visible "Finished:" line
  or dated line counts too, each with words on the line and only that line as
  the answer; nothing else it writes does.
- WAITING: a wait:* label, or a Blocked on section naming an open issue.
  A person cannot act on it today. It stays on the dashboard.
- OPEN: anything else. A ticket whose ask time cannot be read stays OPEN: a
  missed ask costs more than one asked twice.

The pure part is here; the station reads the comments and events and writes
the labels (bus/label_pass.py).

LOOM_ASK_ANSWERERS is an argument of its own, never added to `trusted`: every
other reader of that list grants more than an answer, and a person login in
it would make that login's prose an answer. This module is the setting's only
reader (answerers()), and only the judges of an ask pass the result in. Its
logins stay out of bus/decision_claim.py, so a listed bot's line is no
person's record for the claim guard, nor is the label removal that follows it.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from . import answer, brief_check, decision_claim
from .github import GitHubError
from .labels import HUMAN_LABELS, WAIT_PREFIX

ANSWERED, WAITING, OPEN = "answered", "waiting", "open"
# How a trusted brain records the step the ask named as done.
_FINISHED = re.compile(r"^\s*(?:finished|done)\s*:", re.I | re.M)
# The one finished form a LOOM_ASK_ANSWERERS login has, with words on the line.
_ANSWERER_FINISHED = re.compile(r"finished[ \t]*:[ \t]*\S", re.I)
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
# A decision that names a command: someone must still run it.
_COMMAND = re.compile(r"`[^`\n]+`")


def answerers(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """LOOM_ASK_ANSWERERS: logins whose two line forms answer an ask. It
    grants nothing else (docs/LANES.md, Ask answerers)."""
    raw = (os.environ if env is None else env).get("LOOM_ASK_ANSWERERS", "")
    return raw.replace(",", " ").split()


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


def _records_answer(body: str, since: datetime) -> bool:
    """A dated "Operator answer" line for this ask: one dated before the
    ask's day answered an earlier ask."""
    return any(found.group(1) >= since.date().isoformat()
               for found in decision_claim.ANSWER_LINE.finditer(body))


def _visible_lines(body: str) -> List[str]:
    """The lines a reader sees as prose: none from a Loom marker comment, an
    HTML comment, a fenced block or an indented block."""
    if decision_claim.LOOM_MARKER in body:
        return []
    fence, kept = "", []
    for line in _HTML_COMMENT.sub("", body).splitlines():
        opens = _FENCE.match(line)
        if fence:
            if opens and opens.group(1).startswith(fence):
                fence = ""
        elif opens:
            fence = opens.group(1)[:3]
        elif not line.startswith(("    ", "\t")):
            kept.append(line.strip())
    return kept


def _answerer_line(comment: Dict[str, Any], since: datetime) -> Optional[str]:
    """The line a LOOM_ASK_ANSWERERS login answers with, or None. A dated line
    must name a day from the ask's to the comment's own: a later date records
    nothing yet. Only the line is returned: the rest of the comment is data,
    and must not reach names_command or the brain as an answer."""
    posted = decision_claim.when(comment.get("created_at"))
    for line in _visible_lines(str(comment.get("body") or "")):
        dated = decision_claim.ANSWER_LINE.match(line)
        if _ANSWERER_FINISHED.match(line) or (
                dated and posted is not None
                and since.date().isoformat() <= dated.group(1) <= posted.date().isoformat()):
            return line
    return None


def _answer(comment: Dict[str, Any], people: Iterable[str], trusted: Sequence[str],
            ops: Sequence[str], since: datetime, listed: Iterable[str]) -> Optional[str]:
    """The text of this comment that answers the ask, or None."""
    body = str(comment.get("body") or "").strip()
    whole = str(comment.get("body") or "")
    if answer.trusted_answer(comment, trusted, ops):
        return whole
    login = str((comment.get("user") or {}).get("login") or "").lower()
    if decision_claim.person_comment(comment) and login in people:
        return None if body.endswith("?") else whole
    if login in {str(t).lower() for t in trusted}:
        return whole if _FINISHED.search(body) or _records_answer(body, since) else None
    return _answerer_line(comment, since) if login in listed else None


def answer_of(issue: Dict[str, Any], comments: Sequence[Any], events: Sequence[Any],
              trusted: Iterable[str], ops: Iterable[str],
              ask_answerers: Iterable[str] = ()) -> Optional[str]:
    """The text that answers the open ask ("" for a Decision in the body), or
    None when nothing does. `ask_answerers` are the LOOM_ASK_ANSWERERS logins."""
    trusted, ops = list(trusted), list(ops)
    listed = {str(a).lower() for a in ask_answerers}
    since = asked_at(comments, events)
    if since is None:
        return None
    people = set(decision_claim.person_logins(trusted, ops))
    if decision_claim.body_decision(issue.get("body"), since, people):
        return ""
    for comment in reversed([c for c in comments if isinstance(c, dict)]):
        stamp = decision_claim.when(comment.get("created_at"))
        if stamp is None or stamp <= since:
            continue
        said = _answer(comment, people, trusted, ops, since, listed)
        if said is not None:
            return said
    return None


def open_blockers(rest: Callable[..., Any], repo: str, issue: Dict[str, Any]) -> List[int]:
    """The open issues the ticket's Blocked on section names. `rest` is a
    GitHub client's. A number that names no issue here blocks nothing, and
    any other failed read is the caller's."""
    number = int(issue["number"])
    found: List[int] = []
    for other in brief_check.blocked_on(issue.get("body")):
        if other == number:
            continue
        try:
            read = rest("GET", "/repos/%s/issues/%d" % (repo, other)).data or {}
        except GitHubError as err:
            if err.status not in (404, 410):
                raise
            continue
        if read.get("state") != "closed":
            found.append(other)
    return found


def state(issue: Dict[str, Any], comments: Sequence[Any], events: Sequence[Any],
          trusted: Iterable[str], ops: Iterable[str], blockers: Iterable[int] = (),
          ask_answerers: Iterable[str] = ()) -> str:
    """ANSWERED, WAITING or OPEN. `blockers` are the open issues its Blocked
    on section names; the caller reads them."""
    if answer_of(issue, comments, events, trusted, ops, ask_answerers) is not None:
        return ANSWERED
    if list(blockers) or any(n.startswith(WAIT_PREFIX) for n in _names(issue)):
        return WAITING
    return OPEN


def names_command(text: str) -> bool:
    """The answer names a command, so the ticket goes to who can run it."""
    return bool(_COMMAND.search(text))
