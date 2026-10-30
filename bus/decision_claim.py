"""Text the brain writes that says a person decided (loom#2089).

On 2026-10-27 the brain closed a lane pull request "per the operator decision
recorded on" a ticket, and closed that ticket "per the operator decision",
when the ticket held only an unanswered needs:human ask: no person had said
anything. An unanswered ask is not a decision. The brain may act on its own
judgment and say so, but text that claims a person's decision is refused
unless a person's comment or label, posted after the latest ask, records one
on the target issue or on a ticket the text names.

CLAIM_PATTERNS is the one list of phrasings that claim a decision. `check`
is the guard the brain's write commands call before they post.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import answer
from .github import GitHubError

# Who a claim names as having decided: the operator or the owner. "The user"
# or "a person" is as often a reporter or an upstream author, so it is not.
_ROLE = r"(?:operator|owner)s?(?:'s|s')?"
_NOUN = (r"(?:decision|ruling|call|approval|answer|instruction|direction|go-ahead"
         r"|sign-off|choice|say-so)")
_ACT = r"(?:decided|ruled|signed off|approved|chose|confirmed|agreed|answered|okayed|OK'd)"
# A named subject reads as a claim only with these verbs ("approved" or
# "chose" with a name is as often a bot or a lane), and only when the name is
# an operator's or a trusted person login (claim's `names`): "Upstream
# decided" or "Dependabot decided" claims nothing about this factory.
_NAMED_ACT = r"(?:decided|ruled|signed off|okayed|OK'd)"
_NAME = r"([A-Za-z][\w-]*)"

# Every phrasing that claims a person decided. A match that captures a name
# is a claim only when that name is one of claim's `names`.
CLAIM_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p, re.I) for p in (
    # "per the operator decision", "following the owner's ruling"
    r"\b(?:per|following|according to|pursuant to) (?:the |an? )?%s (?:own )?%s" % (_ROLE, _NOUN),
    # "the operator decided", "operator has approved"
    r"\b%s (?:has |had |have )?(?:already )?%s\b" % (_ROLE, _ACT),
    # "operator decision recorded", "the operator's ruling was recorded"
    r"\b%s %s (?:is |was |has been )?recorded\b" % (_ROLE, _NOUN),
    # "decision recorded by the operator", "the call made by the owner"
    r"\b%s (?:recorded|made|given) by (?:the |an? )?%s" % (_NOUN, _ROLE),
    # "per NAME's decision"
    r"\b(?:per|following|according to|pursuant to) %s's %s" % (_NAME, _NOUN),
    # "NAME decided", "NAME has ruled"
    r"\b%s (?:has |had )?(?:already )?%s\b" % (_NAME, _NAMED_ACT),
))

# A loom marker comment is Loom's own record, even under a person's login:
# an ask is written as the operator's login on some factories.
_LOOM_MARKER = "<!-- loom:"
# Labels the factory sets and clears itself; a person adding one is not a
# decision. A person taking the ask's own label off (needs:human, needs:*)
# is an answer.
_MACHINE_LABEL = re.compile(r"^(?:loom:|claim:|lane:|wait:|needs)", re.I)
_ASK_LABEL = re.compile(r"^(?:loom:human|needs:.+|needs-operator)$", re.I)
# The way an operator records a decision in an issue body:
# "## Decision (<login>, <YYYY-MM-DD>)".
DECISION_HEADING = re.compile(r"^#{2,3}[ \t]*Decision[ \t]*\([ \t]*([A-Za-z0-9][\w-]*)[ \t]*,"
                              r"[ \t]*(\d{4}-\d{2}-\d{2})[ \t]*\)", re.I | re.M)
# A claim that cites a repo document ("the operator's ruling in docs/MUSE.md
# step 6") is checkable in the repo, not on a ticket, so it is not refused.
_DOC_CITATION = re.compile(r"\bin (?:the )?(?:docs\b|[\w./-]*docs/[\w./-]+|[\w./-]+\.md\b)", re.I)
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)|\n")
# A claim right after a negation ("no operator decision was recorded") is
# the opposite of a claim.
_NEGATED = re.compile(r"\b(?:no|not|never|without|nor|neither)\s+(?:\w+\s+){0,2}$", re.I)
_REF = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)"
    r"|(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d+)"
    r"|(?<![\w/.#-])([\w.-]+)#(\d+)"
    r"|(?<![\w/.#-])#(\d+)")
REF_LIMIT = 5

REFUSAL = ("the text claims a person's decision (%r), but no person's comment or label "
           "after the latest needs:human ask on %s records one. An unanswered ask is not a "
           "decision: say the act was the brain's own judgment, or cite the person's "
           "comment by link once one exists")


class DecisionNotRecorded(ValueError):
    """Text claims a person decided and nothing a person wrote records it."""


def person_logins(trusted: Iterable[str] = (), ops: Iterable[str] = ()) -> List[str]:
    """The operator logins and trusted person logins (no "[bot]"), lower case."""
    out = [str(x).strip().lower() for x in list(ops) + list(trusted)]
    return list(dict.fromkeys(x for x in out if x and not x.endswith("[bot]")))


def _cites_doc(body: str, end: int) -> bool:
    rest = body[end:end + 160]
    stop = _SENTENCE_END.search(rest)
    return bool(_DOC_CITATION.search(rest[:stop.start()] if stop else rest))


def _is_claim(body: str, found: "re.Match", named: Optional[str], names: set) -> bool:
    if named is not None and named.lower() not in names:
        return False
    if _NEGATED.search(body[max(0, found.start() - 40):found.start()]):
        return False
    return not _cites_doc(body, found.end())


def claim(text: Any, names: Iterable[str] = ()) -> str:
    """The phrase in `text` that claims a person decided, or "". `names` are
    the logins a named claim ("LOGIN decided") must name to count."""
    body = str(text or "")
    known = {str(n).lower() for n in names}
    for pattern in CLAIM_PATTERNS:
        for found in pattern.finditer(body):
            named = found.group(1) if pattern.groups else None
            if _is_claim(body, found, named, known):
                return found.group(0)
    return ""


def _login(who: Any) -> Tuple[str, str]:
    who = who if isinstance(who, dict) else {}
    return str(who.get("login") or ""), str(who.get("type") or who.get("__typename") or "")


def is_bot(who: Any) -> bool:
    """A GitHub App or bot account: a "[bot]" login or a Bot type."""
    login, kind = _login(who)
    return not login or login.lower().endswith("[bot]") or kind.lower() == "bot"


def when(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def person_comment(comment: Any) -> bool:
    """A comment a person wrote by hand: not a bot's, not a Loom marker
    comment (an ask, a journal, a claim), and not a bare slash command."""
    if not isinstance(comment, dict) or is_bot(comment.get("user") or comment.get("author")):
        return False
    body = str(comment.get("body") or "").strip()
    return bool(body) and _LOOM_MARKER not in body and not body.startswith("/")


def person_label(event: Any) -> bool:
    """A label a person added or took off, other than the factory's own, or
    a person taking the ask's label (needs:human, needs:*) off."""
    if not isinstance(event, dict) or event.get("event") not in ("labeled", "unlabeled"):
        return False
    name = str((event.get("label") or {}).get("name") or "")
    if not name or is_bot(event.get("actor")):
        return False
    if event.get("event") == "unlabeled" and _ASK_LABEL.match(name):
        return True
    return not _MACHINE_LABEL.match(name)


def body_decision(body: Any, since: Optional[datetime], logins: Iterable[str]) -> bool:
    """The issue body records a decision: a "## Decision (<login>, <date>)"
    heading by one of `logins` dated on or after the latest ask's day."""
    allowed = {str(x).lower() for x in logins}
    for found in DECISION_HEADING.finditer(str(body or "")):
        if found.group(1).lower() in allowed and (
                since is None or found.group(2) >= since.date().isoformat()):
            return True
    return False


def latest_ask(comments: Sequence[Any]) -> Optional[datetime]:
    """When the newest needs:human ask among `comments` was posted."""
    asks = [when(c.get("created_at")) for c in comments
            if isinstance(c, dict) and answer.HUMAN_MARKER in str(c.get("body") or "")]
    asks = [a for a in asks if a is not None]
    return max(asks) if asks else None


def recorded(comments: Sequence[Any], events: Sequence[Any],
             trusted: Iterable[str] = (), ops: Iterable[str] = (), body: Any = "") -> bool:
    """A person recorded a decision after the latest ask (or at all, when
    there is no ask): a person's comment, a person's label, a dashboard
    answer from an operator (bus/answer.py, posted as the App), or a
    "## Decision (<login>, <date>)" heading in the issue body."""
    since = latest_ask(comments)
    trusted, ops = list(trusted), list(ops)
    if body_decision(body, since, person_logins(trusted, ops)):
        return True

    def after(item: Dict[str, Any]) -> bool:
        stamp = when(item.get("created_at"))
        return since is None or (stamp is not None and stamp > since)

    return (any(after(c) and (person_comment(c) or answer.trusted_answer(c, trusted, ops))
                for c in comments if isinstance(c, dict))
            or any(after(e) and person_label(e) for e in events if isinstance(e, dict)))


def refs(text: Any, repo: str) -> List[Tuple[str, int]]:
    """The issues `text` names (a link, owner/name#N, name#N in the same
    owner, or #N in `repo`), first REF_LIMIT, in order, without repeats."""
    owner = repo.split("/")[0]
    out: List[Tuple[str, int]] = []
    for m in _REF.finditer(str(text or "")):
        if m.group(1) or m.group(3):
            ref = (m.group(1) or m.group(3), int(m.group(2) or m.group(4)))
        elif m.group(5):
            ref = ("%s/%s" % (owner, m.group(5)), int(m.group(6)))
        else:
            ref = (repo, int(m.group(7)))
        if ref not in out:
            out.append(ref)
    return out[:REF_LIMIT]


def _decided_on(gh: Any, repo: str, number: int, trusted: Sequence[str],
                ops: Sequence[str]) -> bool:
    base = "/repos/%s/issues/%d" % (repo, number)
    try:
        issue = gh.rest("GET", base).data or {}
        comments = gh.paginate(base + "/comments")
        events = gh.paginate(base + "/events")
    except GitHubError as err:
        # A named issue that does not exist records nothing; any other
        # failure is not "no decision" and must not read as one.
        if err.status in (404, 410):
            return False
        raise
    return recorded(comments, events, trusted, ops, issue.get("body"))


def check(gh: Any, repo: str, number: int, text: Any, trusted: Sequence[str] = (),
          ops: Sequence[str] = ()) -> None:
    """Refuse `text`, bound for `repo`#`number`, when it claims a person's
    decision that no person recorded on that issue or on an issue it names.
    Text with no such claim costs no read."""
    said = claim(text, person_logins(trusted, ops))
    if not said:
        return
    targets = [(repo, int(number))]
    targets += [r for r in refs(text, repo) if r not in targets]
    for target_repo, target in targets:
        if _decided_on(gh, target_repo, target, trusted, ops):
            return
    raise DecisionNotRecorded(REFUSAL % (said, ", ".join("%s#%d" % t for t in targets)))
