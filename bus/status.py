"""One status comment per ticket, edited in place for routine events.

Routine ticket events (a lane given, a claim, a stall or limit move, a rung
climb, an equivalent-model move, an automatic retry, a lane failure or factory
hold the factory acts on itself, a fix round's push, a ready mark) were each a
new comment, and every new
comment emails the operator: zippie#264 carried 77 of them on 2026-10-18. They
now go into one comment per ticket, newest first, which the station edits; an
edit sends no email (loom#866). Outcomes (a pull request, merged, shipped,
verified, an ask, a runaway hold for the brain) stay new comments.

Each event keeps its original text, so readers see the same bodies they read
before: the GitHub client expands a status comment in any issue-comment list
into one pseudo-comment per event (expand), timed by the event's own line.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, Any, Iterable, List, Optional, Tuple

MARKER = "<!-- loom:status v1 -->"
HEAD = ("Loom status for this ticket, newest first. Routine events are edited in "
        "here instead of posted as new comments.")
# GitHub's limit is 65536 characters; the oldest events are dropped first.
MAX_CHARS = 60000
_EVENT_RE = re.compile(r"^- ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z) (.*)$")
_CONT = "  "
# An issue's comment list, as a path or a full API URL.
COMMENTS_RE = re.compile(r"/repos/[^/]+/[^/]+/issues/[0-9]+/comments(?:\?|$)")
# Pseudo-comments made by expand carry this key, so a list is re-sorted once.
EXPANDED = "_loom_status_event"


def is_status(body: Any) -> bool:
    return str(body or "").lstrip().startswith(MARKER)


def parse(body: str) -> List[Tuple[str, str]]:
    """(UTC time, event text) for each event of a status comment, newest
    first as written. A continuation line belongs to the event above it."""
    events: List[Tuple[str, List[str]]] = []
    for line in str(body or "").splitlines():
        match = _EVENT_RE.match(line)
        if match:
            events.append((match.group(1), [match.group(2)]))
        elif events and line.startswith(_CONT):
            events[-1][1].append(line[len(_CONT):])
    return [(at, "\n".join(lines)) for at, lines in events]


def render(events: List[Tuple[str, str]]) -> str:
    """The comment body for events, newest first, dropping the oldest past
    MAX_CHARS."""
    lines = [MARKER, HEAD, ""]
    size = sum(len(x) + 1 for x in lines)
    for at, text in events:
        first, *rest = str(text).splitlines() or [""]
        block = ["- %s %s" % (at, first)] + [_CONT + x for x in rest]
        cost = sum(len(x) + 1 for x in block)
        if size + cost > MAX_CHARS:
            break
        lines.extend(block)
        size += cost
    return "\n".join(lines) + "\n"


def _author(comment: Dict[str, Any]) -> str:
    user = comment.get("user")
    return str(user.get("login") or "").lower() if isinstance(user, dict) else ""


def expand(items: Any, authors: Iterable[str] = ()) -> Any:
    """An issue-comment list with each status comment written by one of
    `authors` replaced by one pseudo-comment per event (its body the event's
    text, its created_at and updated_at the event's time, everything else the
    comment's), sorted by time when any was expanded. A status-looking comment
    by anyone else stays one opaque comment: its event times are its own
    words, and expanding them would let a stranger forge when a claim or a
    retry happened. Any other value passes through unchanged."""
    allowed = {a.lower() for a in authors if a}
    if not allowed or not isinstance(items, list) or not any(
            isinstance(c, dict) and is_status(c.get("body")) and _author(c) in allowed
            for c in items):
        return items
    out: List[Any] = []
    for comment in items:
        if not (isinstance(comment, dict) and is_status(comment.get("body"))
                and _author(comment) in allowed):
            out.append(comment)
            continue
        for at, text in reversed(parse(str(comment.get("body") or ""))):
            item: Dict[str, Any] = dict(comment)
            item.update(body=text, created_at=at, updated_at=at)
            item[EXPANDED] = True
            out.append(item)
    return resort(out)


def resort(items: List[Any]) -> List[Any]:
    """The list sorted by created_at when it holds expanded events (a
    stable sort, so comments at the same second keep their order)."""
    if not any(isinstance(c, dict) and c.get(EXPANDED) for c in items):
        return items
    return sorted(items, key=lambda c: str(c.get("created_at") or "") if isinstance(c, dict) else "")


def _utc(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _login(comment: Any) -> str:
    user = comment.get("user") if isinstance(comment, dict) else None
    return str((user or {}).get("login") or "").lower() if isinstance(user, dict) else ""


def find(gh: Any, repo: str, number: int,
         trusted: Iterable[str] = ()) -> Optional[Tuple[int, List[Tuple[str, str]]]]:
    """(id, events newest first) of the ticket's status comment, the first
    one by a trusted login or this client's own (its App bot login); None
    when it has none yet, and None when no login is allowed: a writer with
    the right to edit any comment must never take a stranger's look-alike as
    its own (security review, 2026-10-18). The list may come back expanded
    (the GitHub client expands status comments) or raw; both name the
    comment's id and author."""
    allowed = {str(t).lower() for t in trusted if t}
    own = getattr(gh, "_status_logins", None)
    if callable(own):
        allowed |= {str(t).lower() for t in own()}
    if not allowed:
        return None
    items = gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
    events: dict = {}
    order: List[int] = []
    for comment in items if isinstance(items, list) else []:
        if not isinstance(comment, dict) or _login(comment) not in allowed:
            continue
        ident = comment.get("id")
        if not isinstance(ident, int) or isinstance(ident, bool):
            continue
        if is_status(comment.get("body")):
            events.setdefault(ident, []).extend(parse(str(comment.get("body") or "")))
        elif comment.get(EXPANDED):
            events.setdefault(ident, []).append(
                (str(comment.get("created_at") or ""), str(comment.get("body") or "")))
        else:
            continue
        if ident not in order:
            order.append(ident)
    if not order:
        return None
    first = order[0]
    return first, sorted(events[first], key=lambda e: e[0], reverse=True)


def add(gh: Any, repo: str, number: int, text: str, now: float,
        trusted: Iterable[str] = ()) -> None:
    """Put one routine event at the top of the ticket's status comment:
    edit it in place, or post it the first time."""
    at = _utc(now)
    found = find(gh, repo, number, trusted)
    if found is None:
        gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                {"body": render([(at, text)])})
        return
    ident, events = found
    gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, ident),
            {"body": render([(at, text)] + events)})
