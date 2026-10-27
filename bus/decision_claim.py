"""Text the brain writes that says a person decided (loom#2089).

On 2026-10-27 the brain closed a lane pull request "per the operator decision
recorded on" a ticket, and closed that ticket "per the operator decision",
when the ticket held only an unanswered loom:human ask: no person had said
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

# Who a claim names as having decided: a role, never the brain.
_ROLE = r"(?:operator|owner|maintainer|human|person|people|user)s?(?:'s|s')?"
_NOUN = (r"(?:decision|ruling|call|approval|answer|instruction|direction|go-ahead"
         r"|sign-off|choice|say-so)")
_ACT = r"(?:decided|ruled|signed off|approved|chose|confirmed|agreed|answered|okayed|OK'd)"
# A named subject (a login or a first name) reads as a person only with these
# verbs: "approved" or "chose" with a name is as often a bot or a lane.
_NAMED_ACT = r"(?:decided|ruled|signed off|okayed|OK'd)"
_NAME = r"([A-Za-z][\w-]*)"

# Every phrasing that claims a person decided. A match whose captured name is
# in NOT_A_PERSON (the brain, a bot, a lane, a negation) is not a claim.
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

# Captured names that are not a person: the brain itself, the factory, its
# bots and lanes, the models lanes run, pronouns and negations.
NOT_A_PERSON = frozenset((
    "brain", "muse", "loom", "factory", "station", "lane", "lanes", "bot", "app", "ci",
    "check", "checks", "review", "reviewer", "elder", "chief", "grug", "ticket", "model",
    "agent", "judge", "tick", "codex", "claude", "gemma", "qwen", "gemini", "agy", "opus",
    "sonnet", "haiku", "kimi", "glm", "deepseek", "fable", "i", "we", "it", "they", "he",
    "she", "you", "who", "which", "that", "this", "and", "or", "but", "also", "not",
    "never", "nobody", "noone", "yet", "since", "so", "was", "is", "be", "been", "has",
    "had", "have", "already", "then", "now", "undecided", "self", "own",
))

# A loom marker comment is Loom's own record, even under a person's login:
# an ask is written as the operator's login on some factories.
_LOOM_MARKER = "<!-- loom:"
# Labels the factory sets and clears itself; a person's decision is never one.
_MACHINE_LABEL = re.compile(r"^(?:loom:|claim:|lane:|wait:|needs)", re.I)
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
           "after the latest loom:human ask on %s records one. An unanswered ask is not a "
           "decision: say the act was the brain's own judgment, or cite the person's "
           "comment by link once one exists")


class DecisionNotRecorded(ValueError):
    """Text claims a person decided and nothing a person wrote records it."""


def claim(text: Any) -> str:
    """The phrase in `text` that claims a person decided, or ""."""
    body = str(text or "")
    for pattern in CLAIM_PATTERNS:
        for found in pattern.finditer(body):
            named = found.group(1) if pattern.groups else None
            if named is not None and named.lower() in NOT_A_PERSON:
                continue
            if not _NEGATED.search(body[max(0, found.start() - 40):found.start()]):
                return found.group(0)
    return ""


def _login(who: Any) -> Tuple[str, str]:
    who = who if isinstance(who, dict) else {}
    return str(who.get("login") or ""), str(who.get("type") or who.get("__typename") or "")


def is_bot(who: Any) -> bool:
    """A GitHub App or bot account: a "[bot]" login or a Bot type."""
    login, kind = _login(who)
    return not login or login.lower().endswith("[bot]") or kind.lower() == "bot"


def _when(value: Any) -> Optional[datetime]:
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
    """A label a person added or took off, other than the factory's own."""
    if not isinstance(event, dict) or event.get("event") not in ("labeled", "unlabeled"):
        return False
    name = str((event.get("label") or {}).get("name") or "")
    return bool(name) and not _MACHINE_LABEL.match(name) and not is_bot(event.get("actor"))


def latest_ask(comments: Sequence[Any]) -> Optional[datetime]:
    """When the newest loom:human ask among `comments` was posted."""
    asks = [_when(c.get("created_at")) for c in comments
            if isinstance(c, dict) and answer.HUMAN_MARKER in str(c.get("body") or "")]
    asks = [a for a in asks if a is not None]
    return max(asks) if asks else None


def recorded(comments: Sequence[Any], events: Sequence[Any],
             trusted: Iterable[str] = (), ops: Iterable[str] = ()) -> bool:
    """A person recorded a decision after the latest ask (or at all, when
    there is no ask): a person's comment, a person's label, or a dashboard
    answer from an operator (bus/answer.py, posted as the App)."""
    since = latest_ask(comments)
    trusted, ops = list(trusted), list(ops)

    def after(item: Dict[str, Any]) -> bool:
        stamp = _when(item.get("created_at"))
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
        comments = gh.paginate(base + "/comments")
        events = gh.paginate(base + "/events")
    except GitHubError as err:
        # A named issue that does not exist records nothing; any other
        # failure is not "no decision" and must not read as one.
        if err.status in (404, 410):
            return False
        raise
    return recorded(comments, events, trusted, ops)


def check(gh: Any, repo: str, number: int, text: Any, trusted: Sequence[str] = (),
          ops: Sequence[str] = ()) -> None:
    """Refuse `text`, bound for `repo`#`number`, when it claims a person's
    decision that no person recorded on that issue or on an issue it names.
    Text with no such claim costs no read."""
    said = claim(text)
    if not said:
        return
    targets = [(repo, int(number))]
    targets += [r for r in refs(text, repo) if r not in targets]
    for target_repo, target in targets:
        if _decided_on(gh, target_repo, target, trusted, ops):
            return
    raise DecisionNotRecorded(REFUSAL % (said, ", ".join("%s#%d" % t for t in targets)))
