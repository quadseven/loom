"""Checks on text the brain writes to GitHub."""
from __future__ import annotations

import re

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
# Credential shapes that must not reach a public comment: provider key
# prefixes, bearer tokens, key=value secrets, and long unbroken token runs.
SECRET_RE = re.compile(
    r"(?i)\b(?:sk|pk|ghp|gho|ghs|ghu|github_pat|xox[a-z]|glpat|AKIA|ASIA)[-_A-Za-z0-9]{12,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b[A-Za-z0-9_]*(?:token|secret|password|passwd|api[_-]?key)[A-Za-z0-9_]*\s*[=:]\s*\S+"
    r"|\b[A-Za-z0-9+/_-]{32,}={0,2}")


def scrub_paths(text: str) -> str:
    """The text with every local path replaced by <path>."""
    return PATH_RE.sub("<path>", text)


def check(text: str) -> None:
    """Raise ValueError for text the brain must never post."""
    if not text.isascii():
        raise ValueError("text is not ASCII")
    if LOCAL_PATH_RE.search(text):
        raise ValueError("text contains an absolute path")
    if _HANDLE.search(text):
        raise ValueError("text mentions the review bot's short name after an at-sign")
    if _CLOSING.search(text):
        raise ValueError("text contains a closing keyword with an issue reference")
