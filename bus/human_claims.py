"""A person's own session claims an issue by assignment or by a comment.

A second operator's session works the shared backlog under its own GitHub
login. It claims an issue by assigning that login and commenting a fixed
phrase. Lanes of this Loom must never take such an issue, and no reaper may
unassign the login or remove a claim from it.

LOOM_HUMAN_CLAIMS names the claims: entries split by ";" or new lines, each
`<login>:<phrase>|<phrase>...`. A `*` in a phrase stands for any short run
of text on one line. Unset or blank is DEFAULT, so the rule holds with no
change to the station's environment; "off" (or "none") turns it off. DEFAULT
takes both forms the session writes: its hidden claim marker, or "Claimed by
<anyone>'s Claude Code". A phrase is matched case-insensitively anywhere in
a comment body, with runs of white space as one space and a typographic
apostrophe as a plain one, and only in a comment the login wrote: anyone can
comment on a public repo.

An issue is held (holder) when it is assigned to a claim login, or when the
claim login wrote a comment carrying one of its phrases. The assignment is free to
check; the comment needs a read, so Reader reads an issue's comments only
when asked about one that is otherwise about to be taken and only when the
issue's comment count (GitHub sends it with every issue) is above zero, and
keeps each answer for the issue's updated_at (a new comment moves it).
"""
from __future__ import annotations

import functools
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Pattern, Tuple

ENV = "LOOM_HUMAN_CLAIMS"
DEFAULT = "ccqw:<!-- ccqw:claim v1 -->|Claimed by *'s Claude Code"
OFF = ("off", "none", "0")
# The most text a `*` in a phrase stands for.
WILD_MAX = 80
# Cached answers kept before the cache is emptied.
CACHE_MAX = 2000

Claims = Mapping[str, Pattern[str]]


def _normal(text: str) -> str:
    """Lowercased, white space runs as one space, a typographic apostrophe as
    a plain one."""
    return " ".join(text.replace("\u2019", "'").split()).lower()


def _pattern(phrases: List[str]) -> Pattern[str]:
    """One pattern matching any of `phrases` (already normal)."""
    wild = "[^\n]{1,%d}?" % WILD_MAX
    return re.compile("|".join(wild.join(re.escape(part) for part in phrase.split("*"))
                               for phrase in phrases))


def parse(raw: Optional[str]) -> Dict[str, Pattern[str]]:
    """{login: the pattern its phrases make}, from a LOOM_HUMAN_CLAIMS value.
    The login is lowercased. An entry with no login or no phrase is skipped.
    Only the first `:` splits an entry, so a phrase may hold one."""
    raw = (raw or "").strip()
    if not raw:
        raw = DEFAULT
    if raw.lower() in OFF:
        return {}
    found: Dict[str, Pattern[str]] = {}
    for entry in raw.replace("\n", ";").split(";"):
        login, sep, rest = entry.partition(":")
        login = login.strip().lower()
        phrases = [p for p in (_normal(x) for x in rest.split("|")) if p.strip("* ")]
        if sep and login and phrases:
            found[login] = _pattern(phrases)
    return found


@functools.lru_cache(maxsize=8)
def _parsed(raw: str) -> Dict[str, Pattern[str]]:
    """parse, once per setting value: ownership asks it of every issue."""
    return parse(raw)


def configured(env: Optional[Mapping[str, str]] = None) -> Dict[str, Pattern[str]]:
    """The claims the environment names (os.environ when env is None)."""
    return _parsed((os.environ if env is None else env).get(ENV) or "")


def _login(value: Any) -> str:
    return str(value.get("login") or "").lower() if isinstance(value, dict) else ""


def _claims(claims: Optional[Claims]) -> Claims:
    return configured() if claims is None else claims


def assigned(issue: Dict[str, Any], claims: Optional[Claims] = None) -> Optional[str]:
    """The claim login the issue is assigned to, else None."""
    held = _claims(claims)
    if not held or not isinstance(issue, dict):
        return None
    people = list(issue.get("assignees") or [])
    if not people and isinstance(issue.get("assignee"), dict):
        people = [issue["assignee"]]
    return next((login for login in (_login(p) for p in people) if login in held), None)


def commented(comments: Iterable[Any],
              claims: Optional[Claims] = None) -> Optional[str]:
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
        body = _normal(str(comment.get("body") or ""))
        for login in {_login(comment.get("user")), _login(comment.get("actor"))}:
            if login in held and held[login].search(body):
                return login
    return None


def holder(issue: Dict[str, Any], comments: Iterable[Any] = (),
           claims: Optional[Claims] = None) -> Optional[str]:
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
                 claims: Optional[Claims] = None) -> None:
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
