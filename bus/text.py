"""Checks on text the brain writes to GitHub."""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional, Tuple

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
# `503` is a status only when it is not an issue or pull request reference:
# `#503` is a ticket, a reference with a repo name in front of it is too, and
# `PR 503` names a pull request.
_DOWN = (r"(?:unreachable|down|unavailable|offline|failing|erroring"
         r"|(?<![#/\w])(?<![Pp][Rr] )503)")
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


def has_reason(text: object) -> bool:
    """Whether text has anything a reader sees: an HTML comment is not a reason."""
    return bool(re.sub(r"<!--.*?-->", "", str(text or ""), flags=re.DOTALL).strip())


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
