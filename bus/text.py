"""Checks on text the brain writes to GitHub."""
from __future__ import annotations

import os
import re
from typing import Callable, Iterable, Optional, Tuple

# The review bot's login is grug-tribe[bot]. Its short name with an at-sign in
# front is a real, unrelated GitHub user: each mention notifies and subscribes
# that person, and it cannot be undone. Slash commands use the short name
# without an at-sign.
REVIEW_BOT_SHORT_NAME = "grug"
_HANDLE = re.compile("@" + REVIEW_BOT_SHORT_NAME, re.IGNORECASE)
# GitHub closes the referenced issue on merge for these keywords, even inside
# a negation ("this does not fix #3").
_CLOSING = re.compile(
    r"\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved):?\s+"
    r"(?:#[0-9]+|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+"
    r"|https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[0-9]+)",
    re.IGNORECASE)
# An absolute path: a run of non-space characters starting with a slash (not
# the slash inside a word such as owner/name).
PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/\S*")
# Refuse common absolute filesystem paths while allowing slash commands such
# as /grug and URLs that happen to contain //.
LOCAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:Users|home|private|tmp|var|etc|opt|System|Volumes)/\S*",
    re.IGNORECASE)
# Host suffixes used by private networks and local service discovery.
PRIVATE_HOST_RE = re.compile(
    r"(?i)\b(?:[a-z0-9-]+\.)+(?:local|lan|home|internal|corp|intranet|ts\.net)\b")
# Credential shapes that must not reach a public comment: provider key
# prefixes, bearer tokens and key=value secrets.
CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:sk|pk|ghp|gho|ghs|ghu|github_pat|xox[a-z]|glpat|AKIA|ASIA)[-_A-Za-z0-9]{12,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b[A-Za-z0-9_]*(?:token|secret|password|passwd|api[_-]?key)[A-Za-z0-9_]*\s*[=:]\s*\S+")
# The same shapes plus long unbroken token runs.
SECRET_RE = re.compile(CREDENTIAL_RE.pattern + r"|\b[A-Za-z0-9+/_-]{32,}={0,2}")
# An at-sign followed by a login-shaped word is a GitHub mention wherever it
# sits outside code: a Python decorator, a CSS at-rule and an npm scope all
# look like one, and a real user or organization may own the name
# (loom#1394). A word, a slash or a backtick in front means an
# email address, a URL or a path, which GitHub does not turn into a mention.
_MENTION = re.compile(
    r"(?<![A-Za-z0-9_/`])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"(?:/[A-Za-z0-9_.-]*[A-Za-z0-9_])?")
# A fence line: the opening or closing run of three or more backticks or
# tildes, optionally indented or inside a quote.
_FENCE = re.compile(r"^[ >]*(`{3,}|~{3,})")
# An inline code span: a run of backticks closed by a run of the same length,
# inside one paragraph.
_SPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.DOTALL)
# An outage dated by one time ("unreachable since 04:28Z") reads as its
# start, but a brain only knows its own reads: the last good one and the
# first failing one, and the start lies between them. On 2026-10-22 journals
# and an ask said a service was "down since ~04:28Z" when 04:28Z was the last
# good read; the service went down at 08:02Z, and the next read, at 09:02Z,
# was the first to fail. Text that dates an outage that way must name both.
_TIME = r"~?\s*(?:\d{4}-\d{2}-\d{2}[ T])?\d{1,2}:\d{2}(?::\d{2})?Z?"
_DOWN = r"(?:unreachable|down|unavailable|offline|failing|erroring|503)"
# Within one sentence: a full stop ends it, a decimal point does not.
_SAME_SENTENCE = r"(?:(?![.!?](?:\s|$))[^\n])"
_OUTAGE_SINCE = re.compile(
    r"\b%s\b%s{0,40}?\b(?:since|from|starting|around)\s+%s" % (_DOWN, _SAME_SENTENCE, _TIME)
    + r"|\bsince\s+%s%s{0,120}?\b%s\b" % (_TIME, _SAME_SENTENCE, _DOWN)
    + r"|\b(?:outage|downtime)\b%s{0,30}?\b(?:since|began|started|from)\s+(?:at\s+)?%s"
    % (_SAME_SENTENCE, _TIME), re.I)
_LAST_GOOD = re.compile(r"\blast (?:good|ok|healthy|successful|working)\b", re.I)
_FIRST_BAD = re.compile(r"\bfirst (?:fail\w*|bad|error\w*|unhealthy|503)\b", re.I)


def undated_outage(text: str) -> str:
    """The phrase that dates an outage by one time when the text does not
    also name the last good observation and the first failing one; "" when
    the text is fine."""
    found = _OUTAGE_SINCE.search(text)
    if not found or (_LAST_GOOD.search(text) and _FIRST_BAD.search(text)):
        return ""
    return found.group(0)


# The logins Loom is meant to mention, comma separated. Nobody by default.
MENTION_ALLOW_ENV = "LOOM_MENTION_ALLOW"


def mention_allowlist() -> Tuple[str, ...]:
    """The logins Loom may mention, lower case, from LOOM_MENTION_ALLOW."""
    raw = os.environ.get(MENTION_ALLOW_ENV, "")
    return tuple(w.lstrip("@").lower() for w in re.split(r"[\s,]+", raw) if w.lstrip("@"))


def _wrap_mentions(prose: str, allow: Iterable[str]) -> str:
    def wrap(m: "re.Match[str]") -> str:
        return m.group(0) if m.group(1).lower() in allow else "`" + m.group(0) + "`"
    return _MENTION.sub(wrap, prose)


def _wrap_outside_spans(prose: str, allow: Iterable[str]) -> str:
    out, at = [], 0
    for span in _SPAN.finditer(prose):
        out.append(_wrap_mentions(prose[at:span.start()], allow))
        out.append(span.group(0))
        at = span.end()
    out.append(_wrap_mentions(prose[at:], allow))
    return "".join(out)


def scrub_mentions(text: str, allow: Optional[Iterable[str]] = None) -> str:
    """The text with every would-be GitHub mention wrapped in backticks, so it
    renders as code and notifies nobody. Code spans, fenced blocks and the
    logins in `allow` (default: LOOM_MENTION_ALLOW) are left as they are."""
    allowed = frozenset(w.lstrip("@").lower() for w in (
        mention_allowlist() if allow is None else allow))
    out: list = []
    prose: list = []
    fence = ""
    for line in text.splitlines(keepends=True):
        found = _FENCE.match(line)
        if fence:
            out.append(line)
            if (found and found.group(1)[0] == fence[0] and len(found.group(1)) >= len(fence)
                    and not line[found.end():].strip()):
                fence = ""
        elif found:
            out.append(_wrap_outside_spans("".join(prose), allowed))
            prose = []
            out.append(line)
            fence = found.group(1)
        else:
            prose.append(line)
    out.append(_wrap_outside_spans("".join(prose), allowed))
    return "".join(out)


def scrub_paths(text: str) -> str:
    """The text with every local path replaced by <path>."""
    return PATH_RE.sub("<path>", text)


# What public_safe rewrites. A github.com link to a repo, or to one of its
# issues or pull requests, with any trailing path or fragment but not the
# punctuation after it.
_GITHUB_LINK = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9-]+/[A-Za-z0-9_.-]*[A-Za-z0-9_])"
    r"(?:/(?:issues|pull)/([0-9]+))?(?:[/?#][^\s)\]>`'\"]*[^\s)\]>`'\".,;:!?])?")
# owner/name#N.
_ISSUE_REF = re.compile(r"(?<![\w./-])([A-Za-z0-9-]+/[A-Za-z0-9_.-]*[A-Za-z0-9_])#([0-9]+)\b")
# A bare owner/name, or one with a path in it after: a path or a branch has
# the same shape, so only one under the target repo's own owner is looked up.
_REPO_NAME = re.compile(r"(?<![\w./:#-])([A-Za-z0-9-]+/[A-Za-z0-9_.-]*[A-Za-z0-9_])(?![\w#-])")
# A dotted hostname.
_HOST = re.compile(
    r"(?i)(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9](?![\w-])")
# An HTML comment: a marker Loom reads back, which no rewrite may change.
_MARKER = re.compile(r"<!--.*?-->", re.DOTALL)
PRIVATE_HOST_PLACEHOLDER = "<private-host>"
REDACTED = "<redacted>"


def parse_deny_list(raw: str) -> Tuple[str, ...]:
    """The leak scanner's deny-list terms (infra-public check_private_leaks):
    one per line, blank lines and # comments dropped, case-insensitive
    duplicates dropped."""
    seen: set = set()
    terms = []
    for line in raw.splitlines():
        term = line.strip()
        if term and not term.startswith("#") and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return tuple(terms)


def _deny_rule(term: str) -> "re.Pattern[str]":
    """A denied term as the leak scanner matches it: bounded where its own
    edge is a word character, so a domain suffix still matches mid-host."""
    lead = r"(?<![0-9A-Za-z_])" if term[0].isalnum() or term[0] == "_" else ""
    trail = r"(?![0-9A-Za-z_])" if term[-1].isalnum() or term[-1] == "_" else ""
    return re.compile(lead + re.escape(term) + trail, re.I)


class _Guard:
    """public_safe's rewrite of one body for one public target repo."""

    def __init__(self, repo: str, private: Callable[[str], bool],
                 deny: Iterable[str]) -> None:
        self.repo = repo.lower()
        self.owner = self.repo.split("/")[0]
        self.private = private
        self.rules = [_deny_rule(t) for t in deny if t]

    def other(self, name: str) -> bool:
        return name.lower() != self.repo

    def named(self, name: str) -> bool:
        return self.other(name) and name.lower().split("/")[0] == self.owner

    def host_hit(self, host: str) -> bool:
        return bool(PRIVATE_HOST_RE.search(host)) or any(r.search(host) for r in self.rules)

    def candidate(self, piece: str) -> bool:
        """Whether the piece holds anything to rewrite, asking nobody."""
        return (any(self.other(m.group(1)) for m in _GITHUB_LINK.finditer(piece))
                or any(self.other(m.group(1)) for m in _ISSUE_REF.finditer(piece))
                or any(self.named(m.group(1)) for m in _REPO_NAME.finditer(piece))
                or any(self.host_hit(m.group(0)) for m in _HOST.finditer(piece))
                or any(r.search(piece) for r in self.rules))

    def _words(self, name: str, number: Optional[str], whole: str) -> str:
        if not self.other(name) or not self.private(name):
            return whole
        return "issue %s of a private repo" % number if number else "a private repo"

    def rewrite(self, piece: str) -> str:
        piece = _GITHUB_LINK.sub(lambda m: self._words(m.group(1), m.group(2), m.group(0)), piece)
        piece = _ISSUE_REF.sub(lambda m: self._words(m.group(1), m.group(2), m.group(0)), piece)
        piece = _REPO_NAME.sub(lambda m: (self._words(m.group(1), None, m.group(0))
                                          if self.named(m.group(1)) else m.group(0)), piece)
        piece = _HOST.sub(lambda m: (PRIVATE_HOST_PLACEHOLDER if self.host_hit(m.group(0))
                                     else m.group(0)), piece)
        for rule in self.rules:
            piece = rule.sub(REDACTED, piece)
        return piece


def public_safe(text: str, repo: str, private: Callable[[str], bool],
                deny: Iterable[str] = ()) -> str:
    """The text made safe to post on `repo` when that repo is public.

    `private(name)` says whether a repo is private (an unreadable one counts
    as private). On a public `repo`, another repo's issue that is private
    (owner/name#N or its github.com link) becomes "issue N of a private repo",
    its bare name or link "a private repo" (a bare owner/name only under
    repo's own owner, since a path or a branch has the same shape), and a
    hostname with a private suffix (PRIVATE_HOST_RE) or holding a `deny` term
    (the leak scanner's deny-list) becomes <private-host>; any other `deny`
    term becomes <redacted>. Quoted logs and code are rewritten too; HTML
    comments, the markers Loom reads back, are not. On a private `repo` the
    text is returned unchanged, and `private` is asked about `repo` only when
    the text holds something to rewrite."""
    guard = _Guard(repo, private, deny)
    pieces = _MARKER.split(text)
    if not any(guard.candidate(p) for p in pieces) or private(repo):
        return text
    markers = _MARKER.findall(text)
    out = [guard.rewrite(pieces[0])]
    for marker, piece in zip(markers, pieces[1:]):
        out.extend((marker, guard.rewrite(piece)))
    return "".join(out)


def check(text: str) -> None:
    """Raise ValueError for text the brain must never post."""
    if LOCAL_PATH_RE.search(text):
        raise ValueError("text contains an absolute path")
    if PRIVATE_HOST_RE.search(text):
        raise ValueError("text contains a private hostname")
    if _HANDLE.search(text):
        raise ValueError("text mentions the review bot's short name after an at-sign")
    if _CLOSING.search(text):
        raise ValueError("text contains a closing keyword with an issue reference")
    outage = undated_outage(text)
    if outage:
        raise ValueError("text dates an outage by one time (%r): name the last good "
                         "observation and the first failing one, for example \"last good "
                         "read 04:28Z, first failing read 09:02Z\"; the last good read is "
                         "not the start" % outage)
