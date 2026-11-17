"""A person's own session claims an issue by assignment or by a comment.

A second operator's session works the shared backlog under its own GitHub
login. It claims an issue by assigning that login and commenting a fixed
phrase. Lanes of this Loom must never take such an issue, and no reaper may
unassign the login or remove a claim from it.

LOOM_HUMAN_CLAIMS names the claims: entries split by ";" or new lines, each
`<login>:<phrase>`. Unset or blank is DEFAULT, so the rule holds with no
change to the station's environment; "off" (or "none") turns it off. The
phrase is matched case-insensitively anywhere in a comment body, and only in
a comment the login wrote: anyone can comment on a public repo.

An issue is held (holder) when it is assigned to a claim login, or when the
claim login wrote a comment carrying its phrase. The assignment is free to
check; the comment needs a read, so Reader reads an issue's comments only
when asked about one that is otherwise about to be taken and only when the
issue's comment count (GitHub sends it with every issue) is above zero, and
keeps each answer for the issue's updated_at (a new comment moves it).
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

ENV = "LOOM_HUMAN_CLAIMS"
DEFAULT = "ccqw:Claimed by Cait's Claude Code"
OFF = ("off", "none", "0")
# Cached answers kept before the cache is emptied.
CACHE_MAX = 2000


def parse(raw: Optional[str]) -> Dict[str, str]:
    """{login: phrase}, both lowercased, from a LOOM_HUMAN_CLAIMS value. An
    entry with no login or no phrase is skipped."""
    raw = (raw or "").strip()
    if not raw:
        raw = DEFAULT
    if raw.lower() in OFF:
        return {}
    found: Dict[str, str] = {}
    for entry in raw.replace("\n", ";").split(";"):
        login, sep, phrase = entry.partition(":")
        login, phrase = login.strip().lower(), " ".join(phrase.split()).lower()
        if sep and login and phrase:
            found[login] = phrase
    return found


def configured(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The claims the environment names (os.environ when env is None)."""
    return parse((os.environ if env is None else env).get(ENV))


def _login(value: Any) -> str:
    return str(value.get("login") or "").lower() if isinstance(value, dict) else ""


def _claims(claims: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return configured() if claims is None else claims


def assigned(issue: Dict[str, Any], claims: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The claim login the issue is assigned to, else None."""
    held = _claims(claims)
    if not held or not isinstance(issue, dict):
        return None
    people = list(issue.get("assignees") or [])
    if not people and isinstance(issue.get("assignee"), dict):
        people = [issue["assignee"]]
    return next((login for login in (_login(p) for p in people) if login in held), None)


def commented(comments: Iterable[Any],
              claims: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The claim login that wrote a comment carrying its phrase, else None.
    Takes issue comments (user) and timeline commented events (actor)."""
    held = _claims(claims)
    if not held:
        return None
    for comment in comments or []:
        if not isinstance(comment, dict):
            continue
        if comment.get("event") not in (None, "commented"):
            continue
        body = " ".join(str(comment.get("body") or "").split()).lower()
        for login in {_login(comment.get("user")), _login(comment.get("actor"))}:
            if login in held and held[login] in body:
                return login
    return None


def holder(issue: Dict[str, Any], comments: Iterable[Any] = (),
           claims: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The claim login holding the issue, by assignment or comment."""
    held = _claims(claims)
    return assigned(issue, held) or commented(comments, held)


def has_comments(issue: Dict[str, Any]) -> bool:
    """The issue's comment count says it has a comment to read."""
    count = issue.get("comments") if isinstance(issue, dict) else None
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


class Reader:
    """holder() with the comment read done at most once per issue state.
    `read` returns an issue's comments: read(repo, number)."""

    def __init__(self, read: Callable[[str, int], List[Any]],
                 claims: Optional[Mapping[str, str]] = None) -> None:
        self.read = read
        self.claims = claims
        self.cache: Dict[Tuple[str, int, str], Optional[str]] = {}

    def holder(self, repo: str, issue: Dict[str, Any]) -> Optional[str]:
        held = _claims(self.claims)
        if not held:
            return None
        found = assigned(issue, held)
        if found or not has_comments(issue):
            return found
        key = (repo.lower(), int(issue["number"]), str(issue.get("updated_at") or ""))
        if key not in self.cache:
            if len(self.cache) >= CACHE_MAX:
                self.cache.clear()
            self.cache[key] = commented(self.read(repo, int(issue["number"])), held)
        return self.cache[key]
