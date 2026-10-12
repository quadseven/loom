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


def check(text: str) -> None:
    """Raise ValueError for text the brain must never post."""
    if _HANDLE.search(text):
        raise ValueError("text mentions the review bot's short name after an at-sign")
    if _CLOSING.search(text):
        raise ValueError("text contains a closing keyword with an issue reference")
