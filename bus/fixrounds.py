"""One loom:fix comment per pull request, edited in place for each fix round.

Each fix round posted a new loom:fix comment on the ticket, and every new
comment emails the operator: one ticket sent two identical "Check failed"
emails within the hour for the same pull request (loom#1640). The first
round on a pull request is still a new comment, unchanged. Each later round
edits that comment into a fix-rounds comment, which sends no email.

A fix-rounds comment shows one short block per round (its number, time and
reasons) and keeps each round's whole loom:fix body in a hidden line of its
own, compressed and base64-encoded so the body's own HTML comments and quotes
cannot end it:

    <!-- loom:fix v1 -->
    Pull request: <repo>#<pull request>, branch <branch>
    <!-- loom:fix-rounds v1 -->
    ...one visible block per round...
    <!-- loom:fix-round 1 2026-10-25T14:03:00Z eJy... -->

Readers count rounds, read the fix key, the asked threads and the lane's fix
brief from one comment per round, as before: `expand` turns the comment into
one pseudo-comment per round, its body that round's original loom:fix body
and its time the round's own. The GitHub client expands every issue-comment
list (as it does the status comment), and the readers that count rounds call
`expand` too, so a raw list reads the same. A ticket whose rounds are older
per-round comments keeps them and reads both forms.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import notify
from .tickets import FIX_MARKER

MARKER = "<!-- loom:fix-rounds v1 -->"
ROUND = "<!-- loom:fix-round %d %s %s -->"
ROUND_RE = re.compile(r"^<!-- loom:fix-round ([0-9]+) ([0-9]{4}-[0-9]{2}-[0-9]{2}T"
                      r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z) ([A-Za-z0-9+/=]+) -->[ \t]*$", re.M)
PR_RE = re.compile(r"^Pull request: ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)\b", re.M)
HEAD = ("Loom edits this one comment for each fix round on this pull request, so a "
        "new round sends no new email.")
# Pseudo-comments made by expand carry the round number under this key.
EXPANDED = "_loom_fix_round"
# GitHub rejects a comment over 65536 characters.
MAX_CHARS = 64000
# Lines and characters of one round's reasons shown on the page.
SHOWN_LINES = 12
LINE_CHARS = 300
# A comment edited this long after its newest round was written was edited by
# something other than this writer (the clocks of GitHub and the writer agree
# to well within this). GitHub keeps no editor for a comment.
EDIT_SLACK = 300


def is_rounds(body: Any) -> bool:
    text = str(body or "")
    return text.startswith(FIX_MARKER) and MARKER in text.split("\n", 3)[:3]


def _utc(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(stamp: Any) -> Optional[float]:
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _encode(body: str) -> str:
    return base64.b64encode(zlib.compress(body.encode("utf-8"), 9)).decode("ascii")


def _decode(blob: str) -> Optional[str]:
    try:
        return zlib.decompress(base64.b64decode(blob, validate=True)).decode("utf-8")
    except (binascii.Error, zlib.error, UnicodeDecodeError, ValueError):
        return None


def parse(body: Any) -> List[Tuple[int, str, str]]:
    """(round number, UTC time, the round's loom:fix body) for each round the
    comment holds, oldest first. A line that does not decode is skipped."""
    if not is_rounds(body):
        return []
    out = []
    for number, at, blob in ROUND_RE.findall(str(body)):
        text = _decode(blob)
        if text is not None:
            out.append((int(number), at, text))
    return sorted(out, key=lambda r: r[0])


def pull_of(body: Any) -> Optional[Tuple[str, int]]:
    """(repo, number) of the pull request a loom:fix body names."""
    found = PR_RE.search(str(body or ""))
    return (found.group(1), int(found.group(2))) if found else None


def _login(comment: Dict[str, Any]) -> str:
    user = comment.get("user")
    return str(user.get("login") or "").lower() if isinstance(user, dict) else ""


def _edited(comment: Dict[str, Any], rounds: List[Tuple[int, str, str]]) -> bool:
    """Whether the comment changed after its newest round was written."""
    newest = _epoch(rounds[-1][1]) if rounds else None
    updated = _epoch(comment.get("updated_at"))
    return newest is not None and updated is not None and updated > newest + EDIT_SLACK


def expand(items: Any, authors: Optional[Iterable[str]] = None) -> Any:
    """An issue-comment list with each fix-rounds comment replaced by one
    pseudo-comment per round: its body the round's loom:fix body, its
    created_at and updated_at the round's time, everything else (id, user)
    the comment's. With `authors`, only a comment one of them wrote is
    expanded; a look-alike by anyone else stays one opaque comment. A comment
    edited after its newest round keeps its own updated_at on that round, so a
    reader that refuses an edited loom:fix comment refuses it. The list is
    sorted by time when every comment carries one, else each comment's rounds
    take its place. Calling it on an expanded list changes nothing."""
    allowed = None if authors is None else {str(a).lower() for a in authors if a}
    if not isinstance(items, list) or not any(
            isinstance(c, dict) and is_rounds(c.get("body"))
            and (allowed is None or _login(c) in allowed) for c in items):
        return items
    out: List[Any] = []
    for comment in items:
        if not (isinstance(comment, dict) and is_rounds(comment.get("body"))
                and (allowed is None or _login(comment) in allowed)):
            out.append(comment)
            continue
        rounds = parse(comment.get("body"))
        edited = _edited(comment, rounds)
        for i, (number, at, text) in enumerate(rounds):
            item: Dict[str, Any] = dict(comment)
            item.update(body=text, created_at=at, updated_at=at)
            if edited and i == len(rounds) - 1:
                item["updated_at"] = comment.get("updated_at")
            item[EXPANDED] = number
            out.append(item)
    if all(isinstance(c, dict) and c.get("created_at") for c in out):
        out.sort(key=lambda c: str(c.get("created_at")))
    return out


def count(comments: Iterable[Any], authors: Optional[Iterable[str]] = None) -> int:
    """The loom:fix bodies in a comment list, each round of a fix-rounds
    comment counted once (`authors` limits whose comments count)."""
    allowed = None if authors is None else {str(a).lower() for a in authors if a}
    return sum(1 for c in expand(list(comments), authors)
               if isinstance(c, dict) and str(c.get("body") or "").startswith(FIX_MARKER)
               and (allowed is None or _login(c) in allowed))


def _reasons(body: str) -> List[str]:
    """The visible lines of one round's loom:fix body: what it asks, without
    its markers, its pull request line and its hidden detail."""
    shown: List[str] = []
    for line in body.split("\n")[1:]:
        if line.startswith("<!-- loom:fix-detail"):
            break
        if line.startswith("<!--") or PR_RE.match(line):
            continue
        if line.strip() or (shown and shown[-1].strip()):
            shown.append(line[:LINE_CHARS])
    while shown and not shown[-1].strip():
        shown.pop()
    if len(shown) > SHOWN_LINES:
        shown = shown[:SHOWN_LINES] + ["- (%d more lines in this round)" % (len(shown) - SHOWN_LINES)]
    return shown or ["- (no reasons given)"]


def _slim(body: str) -> str:
    """A round's body cut to what the readers count from it: its markers, its
    pull request line, its key and head lines and its ### headings. Used on
    older rounds only when the comment would not fit otherwise."""
    keep = [line for line in body.split("\n")
            if line.startswith(("<!-- loom:fix", "Pull request: ", "### "))
            and not line.startswith("<!-- loom:fix-detail")]
    return "\n".join(keep) + "\n"


def render(pull: Tuple[str, int], branch: str, rounds: List[Tuple[int, str, str]]) -> Optional[str]:
    """The comment body for `rounds` (oldest first), or None when it cannot
    fit even with every round but the newest cut to its headings."""
    def body(items: List[Tuple[int, str, str]]) -> str:
        lines = [FIX_MARKER, "Pull request: %s#%d, branch %s" % (pull[0], pull[1], branch),
                 MARKER, "", HEAD, ""]
        for number, at, text in items:
            lines.append("Round %d, %s UTC:" % (number, at[:16].replace("T", " ")))
            lines.extend(_reasons(text))
            lines.append("")
        lines.extend(ROUND % (number, at, _encode(text)) for number, at, text in items)
        return "\n".join(lines) + "\n"
    items = list(rounds)
    text = body(items)
    for i in range(len(items) - 1):
        if len(text) <= MAX_CHARS:
            break
        number, at, old = items[i]
        items[i] = (number, at, _slim(old))
        text = body(items)
    return text if len(text) <= MAX_CHARS else None


def _branch(body: str) -> str:
    found = re.search(r"^Pull request: \S+#[0-9]+, branch (\S+)", body, re.M)
    return found.group(1) if found else ""


def _writers(gh: Any, authors: Iterable[str]) -> set:
    """Whose fix comment a new round may edit: this client's own App login
    when it has one (an App edits its own comments), else the trusted
    logins (a personal token writes as one of them)."""
    from .github import GitHubError  # the client imports this module
    own = getattr(gh, "app_login", None)
    login = None
    if callable(own):
        try:
            login = own()
        except GitHubError:
            login = None
    if isinstance(login, str) and login:
        return {login.lower()}
    return {str(a).lower() for a in authors if a}


def _rounds_of(comments: Any, writers: set,
               pull: Tuple[str, int]) -> Optional[Tuple[int, List[Tuple[int, str, str]]]]:
    """(comment id, rounds oldest first) of the newest loom:fix comment one of
    `writers` wrote for `pull`: a fix-rounds comment's rounds, or a plain
    loom:fix comment as round 1. None when there is none, or when that comment
    was edited after it was written (it may carry someone else's words)."""
    newest = None
    for c in expand(comments if isinstance(comments, list) else [], writers):
        ident = c.get("id") if isinstance(c, dict) else None
        if (isinstance(ident, int) and not isinstance(ident, bool) and _login(c) in writers
                and str(c.get("body") or "").startswith(FIX_MARKER)
                and pull_of(c.get("body")) == pull):
            newest = c
    if newest is None:
        return None
    ident = newest["id"]
    if newest.get("updated_at") != newest.get("created_at"):
        return None
    if not newest.get(EXPANDED):
        at = str(newest.get("created_at") or "") or _utc(time.time())
        return ident, [(1, at, str(newest.get("body") or ""))]
    rounds = [c for c in expand(comments, writers)
              if isinstance(c, dict) and c.get("id") == ident and c.get(EXPANDED)]
    return ident, sorted(((int(c[EXPANDED]), str(c.get("created_at")), str(c.get("body") or ""))
                          for c in rounds), key=lambda r: r[0])


def post_round(gh: Any, repo: str, number: int, body: str, authors: Iterable[str],
               comments: Optional[List[Any]] = None, now: Optional[float] = None) -> Any:
    """Add one fix round (a whole loom:fix body) to the ticket. The first
    round on a pull request is a new comment, as before. A later one edits the
    newest loom:fix comment this writer wrote for the same pull request into a
    fix-rounds comment holding every round, which sends no email. It posts a
    new comment instead when that comment was edited by someone else, when the
    rounds no longer fit, or when the edit is refused. `comments` is the
    ticket's comment list when the caller has just read it (it is read here
    otherwise). Returns the GitHub client's response."""
    from .github import GitHubError, RateLimited  # the client imports this module
    at = _utc(time.time() if now is None else now)
    pull = pull_of(body)
    found = None
    if pull is not None:
        if comments is None:
            comments = gh.paginate("/repos/%s/issues/%d/comments" % (repo, int(number)))
        found = _rounds_of(comments, _writers(gh, authors), pull)
    if found is not None and pull is not None:
        ident, rounds = found
        rounds.append((rounds[-1][0] + 1, max(at, rounds[-1][1]), body))
        text = render(pull, _branch(body), rounds)
        if text is not None:
            try:
                return gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, ident),
                               {"body": text})
            except RateLimited:
                raise
            except GitHubError:
                pass  # refused (not ours to edit, or gone): post the round instead
    return notify.post(gh, repo, number, body, notify.FIX_BRIEF)
