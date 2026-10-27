"""One-tap answers to a person-wait (loom#1777).

The dashboard's Needs you page lets an operator answer an ask in one tap. The
dashboard posts ONE comment on the issue as the Loom GitHub App:

    <!-- loom:answer v1 --> Answer from <login> (via the dashboard): <choice>. <note>

takes off loom:human and needs:<login> (and needs-operator), and adds
loom:answer-pending until the station has read it. This module is the shared
part: the answer options an ask offers (derived from its words, no model),
the comment's format, and how the station reads one back.

An answer counts as the operator's only when its author is trusted
(LOOM_BRIEF_AUTHORS, where the App's bot login is listed) or is the operator
it names, and the login it names is an operator (`operators`). Anyone can
type the marker; nobody else's copy counts.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

ANSWER_MARKER = "<!-- loom:answer v1 -->"
# Equal to bus.brain.HUMAN_MARKER (a test holds them together): the comment a
# loom:human ask is written in. Not imported, so bus.brain can import this.
HUMAN_MARKER = "<!-- loom:human v1 -->"
# Not loom:answered: that is a ticket state (a runner's answer, docs/BRAIN.md).
ANSWERED_LABEL = "loom:answer-pending"
VIA = "via the dashboard"
CHOICE_MAX = 200
NOTE_MAX = 1000

CHOICE, NUMBER, TEXT = "choice", "number", "text"
MERGE, HOLD = "Merge", "Hold"
NOT_NOW = "Not now"
# Kinds of loom:human ask (bus.brain.HUMAN_KINDS) a person settles by doing
# the thing and saying so.
DONE_KINDS = ("account", "physical", "hardware")


class Options(NamedTuple):
    """What a card offers: `kind` CHOICE (one button per entry of
    `choices`), NUMBER (a number field) or TEXT (a text field)."""
    kind: str
    choices: Tuple[str, ...] = ()


# The rules, tried in this order; the first that matches decides. Each reads
# the ask's own words, so the same ask always offers the same answers.
_NUMBER_RE = re.compile(r"\breply with (?:one|a|a single) number\b", re.I)
# A lane pull request held for the operator (bus.operator_hold.ask): the
# operator merges it or keeps it held.
HELD_PR_RE = re.compile(r"\bPull request #([0-9]+) waits for you: Loom will not merge it\b")
_MERGE_HOLD_RE = re.compile(r"\bmerge\b[^.?!\n]{0,160}?\bor (?:keep (?:it )?)?hold(?:ing)?\b", re.I)
_KEEP_CLOSE_RE = re.compile(r"\bkeep\b[^.?!\n]{0,160}?\bor close\b", re.I)
_YES_NO_RE = re.compile(r"\breply (?:with )?yes or no\b|\(reply yes\b|\breply yes\b", re.I)
_REPLY_LETTERS_RE = re.compile(r"\breply (?:with )?\(?([a-d])\)?((?:\s*,\s*\(?[a-d]\)?)*)\s*,?\s+or\s+\(?([a-d])\)?(?![\w])",
                               re.I)
_LETTER_OPTION_RE = re.compile(r"(?:^|[\s(])\(([a-d])\)\s", re.I | re.M)
_DONE_RE = re.compile(r"\breply (?:with )?done\b", re.I)
_KIND_RE = re.compile(r"^\s*Kind:\s*(\S+)\s*$", re.I | re.M)
_DEFAULT_RE = re.compile(r"^\s*Default:\s*(.*?)\s*$", re.I | re.M)
_REVERSIBLE_RE = re.compile(r"^\s*Reversible:\s*(\S+)\s*$", re.I | re.M)


def decision_default(text: str) -> str:
    """Return a validated default for a reversible decision ask, else empty."""
    source = str(text or "")
    found = _DEFAULT_RE.findall(source)
    if not found:
        return ""
    if len(found) != 1 or ask_kind(source) != "decision":
        raise ValueError("a Default line is only allowed once on a decision ask")
    if not reversible_decision(source):
        raise ValueError("a decision default requires Reversible: yes")
    cleaned = _DEFAULT_RE.sub("", _REVERSIBLE_RE.sub("", source))
    offered = options(cleaned)
    if offered.kind != CHOICE:
        raise ValueError("a decision default requires explicit choices in the ask")
    choice, _ = accept(offered, found[0])
    return choice


def reversible_decision(text: str) -> bool:
    """Whether an ask explicitly names itself as a reversible decision."""
    source = str(text or "")
    return ask_kind(source) == "decision" and _REVERSIBLE_RE.findall(source) == ["yes"]


def ask_kind(text: str) -> str:
    """The lower-case kind a "Kind: K" line of the ask names, or ""."""
    found = _KIND_RE.search(str(text or ""))
    return found.group(1).lower().rstrip(".") if found else ""


def _letters(text: str) -> Tuple[str, ...]:
    """a, b (, c, d) when the ask offers lettered options: "Reply a or b",
    "reply a, b or c", or "(a) ... (b) ..." in order from a."""
    found = _REPLY_LETTERS_RE.search(text)
    if found:
        named = [found.group(1)] + re.findall(r"[a-d]", found.group(2) or "", re.I) + [found.group(3)]
        letters = tuple(dict.fromkeys(letter.lower() for letter in named))
        if len(letters) >= 2:
            return letters
    seen = [letter.lower() for letter in _LETTER_OPTION_RE.findall(text)]
    out: List[str] = []
    for letter in "abcd":
        if letter not in seen:
            break
        out.append(letter)
    return tuple(out) if len(out) >= 2 else ()


def options(ask: str, kind: str = "") -> Options:
    """The answers the ask offers, read from its words:

    - "Reply with one number": a number field;
    - a lane pull request held for the operator, or "merge ... or hold":
      Merge or Hold;
    - "keep ... or close": Keep or Close;
    - "reply yes or no", "(reply yes)": Yes or No;
    - "Reply a or b", "(a) ... (b)": a, b (and c, d when offered);
    - "reply done", or Kind account, physical or hardware: Done or Not now;
    - anything else: free text.
    """
    text = " ".join(_DEFAULT_RE.sub("", _REVERSIBLE_RE.sub("", str(ask or "")))
                    .replace("**", "").split())
    kind = (kind or ask_kind(str(ask or ""))).lower()
    if _NUMBER_RE.search(text):
        return Options(NUMBER)
    if HELD_PR_RE.search(text) or _MERGE_HOLD_RE.search(text):
        return Options(CHOICE, (MERGE, HOLD))
    if _KEEP_CLOSE_RE.search(text):
        return Options(CHOICE, ("Keep", "Close"))
    if _YES_NO_RE.search(text):
        return Options(CHOICE, ("Yes", "No"))
    letters = _letters(text)
    if letters:
        return Options(CHOICE, letters)
    if _DONE_RE.search(text) or kind in DONE_KINDS:
        return Options(CHOICE, ("Done", NOT_NOW))
    return Options(TEXT)


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def accept(opts: Options, choice: Any, note: Any = "") -> Tuple[str, str]:
    """(choice, note) as they will be written, or ValueError: a button's
    choice must be one of the card's, a number field takes an integer, and a
    text answer must say something. The note is optional."""
    text = _one_line(choice, CHOICE_MAX)
    note_text = _one_line(note, NOTE_MAX)
    if opts.kind == CHOICE:
        found = next((c for c in opts.choices if c.lower() == text.lower()), None)
        if found is None:
            raise ValueError("the answer must be one of: %s" % ", ".join(opts.choices))
        return found, note_text
    if opts.kind == NUMBER:
        if not re.fullmatch(r"-?[0-9]{1,12}", text):
            raise ValueError("the answer must be one number")
        return text, note_text
    text = text.rstrip(".").strip()
    if not text:
        raise ValueError("write the answer")
    return text, note_text


_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def comment_body(login: str, choice: str, note: str = "") -> str:
    """The one comment an answer is posted as."""
    if not _LOGIN_RE.fullmatch(str(login or "")):
        raise ValueError("not a GitHub login: %r" % login)
    text = "%s Answer from %s (%s): %s." % (ANSWER_MARKER, login, VIA, str(choice).rstrip("."))
    return text + (" " + note if note else "")


class Answer(NamedTuple):
    login: str          # the operator who answered, lower case
    choice: str         # as written ("Merge", "Yes", "3", or the free text)
    note: str
    author: str         # who posted the comment, lower case
    created_at: str
    id: int
    url: str

    def text(self) -> str:
        """The answer in words, as the operator's own comment would say it."""
        return "%s.%s" % (self.choice, (" " + self.note) if self.note else "")


_ANSWER_RE = re.compile(r"\A\s*%s\s*Answer from ([A-Za-z0-9-]+) \(%s\): (.*?)(?:\.(?:\s+(.*))?)?\s*\Z"
                        % (re.escape(ANSWER_MARKER), re.escape(VIA)), re.S)


def _author(comment: Mapping[str, Any]) -> str:
    user = comment.get("user")
    return str((user or {}).get("login") or "").lower() if isinstance(user, dict) else ""


def parse(comment: Any) -> Optional[Answer]:
    """The answer a comment carries, whoever wrote it; None for any other
    comment. Trust is `trusted_answer`'s."""
    if not isinstance(comment, dict):
        return None
    found = _ANSWER_RE.match(str(comment.get("body") or ""))
    if not found:
        return None
    choice = " ".join(found.group(2).split())
    note = " ".join(str(found.group(3) or "").split())
    ident = comment.get("id")
    return Answer(found.group(1).lower(), choice, note, _author(comment),
                  str(comment.get("created_at") or ""),
                  ident if isinstance(ident, int) and not isinstance(ident, bool) else 0,
                  str(comment.get("html_url") or ""))


def _split(raw: str) -> List[str]:
    return [part for part in raw.replace(",", " ").split() if part]


def operator_map(env: Optional[Mapping[str, str]] = None,
                 trusted: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """identity -> operator login, both lower case. LOOM_OPERATORS lists the
    operators who may answer, space or comma separated, each a login or
    IDENTITY=LOGIN (an identity a proxy header names, mapped to a login);
    unset, the person logins (no "[bot]") of LOOM_BRIEF_AUTHORS, or of
    `trusted` when given."""
    values = os.environ if env is None else env
    raw = str(values.get("LOOM_OPERATORS") or "").strip()
    out: Dict[str, str] = {}
    if raw:
        for entry in _split(raw):
            ident, _, login = entry.partition("=")
            login = (login or ident).strip().lower()
            if _LOGIN_RE.fullmatch(login):
                out[ident.strip().lower()] = login
                out.setdefault(login, login)
        return out
    names = list(trusted) if trusted is not None else _split(str(values.get("LOOM_BRIEF_AUTHORS") or ""))
    for name in names:
        login = str(name or "").strip().lower()
        if login and not login.endswith("[bot]") and _LOGIN_RE.fullmatch(login):
            out[login] = login
    return out


def operators(env: Optional[Mapping[str, str]] = None,
              trusted: Optional[Iterable[str]] = None) -> List[str]:
    """The operator logins who may answer, lower case, in order."""
    return list(dict.fromkeys(operator_map(env, trusted).values()))


def trusted_answer(comment: Any, trusted: Iterable[str], ops: Iterable[str]) -> Optional[Answer]:
    """The comment's answer when it counts as an operator's: posted by a
    trusted login (the App's bot) or by the operator it names, and naming an
    operator."""
    found = parse(comment)
    if found is None:
        return None
    allowed = {str(t).lower() for t in trusted}
    if found.login not in {str(o).lower() for o in ops}:
        return None
    if found.author not in allowed and found.author != found.login:
        return None
    return found


def answers(comments: Iterable[Any], trusted: Iterable[str], ops: Iterable[str]) -> List[Answer]:
    """Every operator answer among the comments, in their order (oldest
    first, as GitHub lists them)."""
    trusted, ops = list(trusted), list(ops)
    return [a for a in (trusted_answer(c, trusted, ops) for c in comments or []) if a is not None]


def newest(comments: Iterable[Any], trusted: Iterable[str], ops: Iterable[str]) -> Optional[Answer]:
    found = answers(comments, trusted, ops)
    return found[-1] if found else None


def as_operator_comments(comments: Iterable[Any], trusted: Iterable[str], ops: Iterable[str],
                         operator: str = "") -> List[Any]:
    """The comments with each operator answer written as that operator's own
    comment: by `operator` (or the login it names), a person, saying the
    answer in words. A reader of "the operator's newest comment" then reads
    an answer given on the dashboard as one typed on the issue."""
    trusted, ops = list(trusted), list(ops)
    out: List[Any] = []
    for comment in comments or []:
        found = trusted_answer(comment, trusted, ops)
        if found is None:
            out.append(comment)
            continue
        out.append(dict(comment, user={"login": operator or found.login, "type": "User"},
                        body=found.text()))
    return out


def held_pr(text: Any) -> Optional[int]:
    """The pull request a lane hold ask (bus.operator_hold.ask) names, or None."""
    found = HELD_PR_RE.search(" ".join(str(text or "").split()))
    return int(found.group(1)) if found else None


def _hold_asks(comments: Iterable[Any], trusted: Iterable[str]) -> List[Tuple[int, Mapping[str, Any]]]:
    allowed = {str(t).lower() for t in trusted}
    out = []
    for comment in comments or []:
        if not isinstance(comment, dict) or _author(comment) not in allowed:
            continue
        body = str(comment.get("body") or "")
        if not body.startswith(HUMAN_MARKER):
            continue
        number = held_pr(body)
        if number is not None:
            out.append((number, comment))
    return out


def pr_verdict(comments: Sequence[Any], number: int, trusted: Iterable[str], ops: Iterable[str],
               since: str = "") -> Optional[Answer]:
    """The operator's Merge or Hold on lane pull request `number`: the newest
    operator answer saying Merge or Hold, given after the newest hold ask on
    the ticket when that ask names this pull request, and (with `since`, the
    head commit's time) not before the head it would merge. None when there
    is none: the hold stands and asks as before."""
    trusted, ops = list(trusted), list(ops)
    asks = _hold_asks(comments, trusted)
    if not asks or asks[-1][0] != int(number):
        return None
    asked = asks[-1][1]
    asked_at = str(asked.get("created_at") or "")
    verdict = _last_merge_or_hold(comments, asked, trusted, ops)
    if verdict is None or (asked_at and verdict.created_at and verdict.created_at < asked_at):
        return None
    if since and verdict.created_at and verdict.created_at < since:
        return None
    return verdict


def _last_merge_or_hold(comments: Sequence[Any], asked: Any, trusted: List[str],
                       ops: List[str]) -> Optional[Answer]:
    """The newest operator Merge or Hold after the comment `asked`."""
    later = list(comments)
    later = later[next(i for i, c in enumerate(later) if c is asked) + 1:]
    found = [a for a in (trusted_answer(c, trusted, ops) for c in later)
             if a is not None and a.choice.lower() in (MERGE.lower(), HOLD.lower())]
    return found[-1] if found else None


def is_merge(verdict: Optional[Answer]) -> bool:
    return verdict is not None and verdict.choice.lower() == MERGE.lower()


def is_hold(verdict: Optional[Answer]) -> bool:
    return verdict is not None and verdict.choice.lower() == HOLD.lower()


def record_verdict(repo: str, number: int, verdict: Answer, sha: str = "",
                   ticket: Optional[Tuple[str, int]] = None, source: str = "",
                   env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Write the operator's Merge or Hold on a lane pull request to the
    shadow ledger, once per pull request, head and verdict (bus/shadow.py),
    so the ledger pairs Loom's decision with the operator's."""
    from . import shadow
    reason = "the operator %s answered %s%s%s" % (
        verdict.login, verdict.choice.lower(), (" (%s)" % verdict.note) if verdict.note else "",
        (" on ticket %s#%d" % ticket) if ticket else "")
    return shadow.record(shadow.VERDICT, repo, number, "pr", sha=sha, label=verdict.choice.lower(),
                         reason=reason, source=source or "operator answer " + (verdict.url or ""),
                         env=env)
