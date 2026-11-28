"""The parts of bus/github.py a gate needs without the client: the error a
failed call raises and the REST form of a GraphQL login.

bus/github.py imports http.client, the shadow guard and the fix-rounds
ledger, which a lane's `python3 -m bus.<module>` call pays for on every
start. A module that only catches GitHubError or reads a login imports this
one instead; bus/github.py re-exports both names, so either import gives the
same class and function."""
from __future__ import annotations

from typing import Any, Dict, Optional


class GitHubError(Exception):
    """A failed GitHub call: the HTTP status and the response body text."""

    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def rest_login(author: Optional[Dict[str, Any]]) -> str:
    """A GraphQL author's login in the form REST, and so LOOM_BRIEF_AUTHORS,
    uses: GraphQL names a GitHub App "<slug>" and REST "<slug>[bot]". Only an
    author whose __typename is Bot gains the suffix, so a person who takes the
    bare slug as a user login is never read as the App."""
    author = author or {}
    login = str(author.get("login") or "")
    if login and author.get("__typename") == "Bot" and not login.lower().endswith("[bot]"):
        login += "[bot]"
    return login
