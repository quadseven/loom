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

The first routine event on a ticket had no comment to edit, so it posted a new
one and emailed the operator once per ticket (loom#1341). A client that reads
events back from the issue body (it sets `status_in_body`) now keeps them in a
marked section at the end of the body instead: a body edit sends no email
either. A ticket that already has a status comment keeps using it.
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
# An event read from an issue body section also carries this key: it names no
# comment, so nothing may edit it as one.
FROM_BODY = "_loom_status_body"
# The section of an issue body that holds the routine events, each marker alone
# on its line (a quoted copy of a body, "> <!-- ...", is not a section).
BODY_BEGIN = "<!-- loom:status-body v1 begin -->"
BODY_END = "<!-- loom:status-body v1 end -->"
BODY_HEAD = ("Loom status for this ticket, newest first. Routine events are kept "
             "in this section instead of posted as comments.")
_SECTION_RE = re.compile(r"^%s[ \t]*\n(.*?)^%s[ \t]*$\n?" % (
    re.escape(BODY_BEGIN), re.escape(BODY_END)), re.M | re.S)
# GitHub's limit for an issue body is 65536 characters.
BODY_MAX_CHARS = 65000
# Fresh read: a conditional request that matches nothing, so GitHub answers in full.
FRESH = '"loom-status-fresh"'


class OwnBody(dict):
    """The body of a PATCH that writes the status section itself, so the
    client does not graft the section it just read over it."""


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


def logins(gh: Any, trusted: Iterable[str] = ()) -> set:
    """Who may write status events this client edits: the trusted logins and
    this client's own App login."""
    allowed = {str(t).lower() for t in trusted if t}
    own = getattr(gh, "_status_logins", None)
    if callable(own):
        allowed |= {str(t).lower() for t in own()}
    return allowed


def find(gh: Any, repo: str, number: int,
         trusted: Iterable[str] = ()) -> Optional[Tuple[int, List[Tuple[str, str]]]]:
    """(id, events newest first) of the ticket's status comment, the first
    one by a trusted login or this client's own (its App bot login); None
    when it has none yet, and None when no login is allowed: a writer with
    the right to edit any comment must never take a stranger's look-alike as
    its own (security review, 2026-10-18). The list may come back expanded
    (the GitHub client expands status comments) or raw; both name the
    comment's id and author."""
    allowed = logins(gh, trusted)
    if not allowed:
        return None
    items = gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
    events: dict = {}
    order: List[int] = []
    for comment in items if isinstance(items, list) else []:
        if not isinstance(comment, dict) or _login(comment) not in allowed:
            continue
        ident = comment.get("id")
        if not isinstance(ident, int) or isinstance(ident, bool) or comment.get(FROM_BODY):
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
    """Put one routine event at the top of the ticket's status: edit its
    status comment in place when it has one, else add it to the status section
    of the issue body (a client that reads that back, loom#1341), else post the
    comment the first time."""
    at = _utc(now)
    found = find(gh, repo, number, trusted)
    if found is None:
        if _add_to_body(gh, repo, number, at, text, logins(gh, trusted)):
            return
        gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                {"body": render([(at, text)])})
        return
    ident, events = found
    gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, ident),
            {"body": render([(at, text)] + events)})


BODY_TRIES = 3


def _add_to_body(gh: Any, repo: str, number: int, at: str, text: str, allowed: set) -> bool:
    """Add the event to the issue body's status section; False when it cannot
    go there (the client does not read the section back, a login of ours did
    not write the issue, the section does not fit, or the body kept changing
    under the write) and the caller posts a comment instead.

    A body edit sends no email. GitHub has no conditional write for an issue,
    so the body is read in full, the section merged in, and the body read again
    just before the write: if it moved meanwhile the merge starts over, and
    after BODY_TRIES moves the event goes to a comment rather than risk
    overwriting an edit made at the same time. Only a write within one request
    of the second read can still lose to an edit."""
    if not getattr(gh, "status_in_body", False) or not allowed:
        return False
    path = "/repos/%s/issues/%d" % (repo, number)
    for _ in range(BODY_TRIES):
        issue = gh.rest("GET", path, etag=FRESH).data
        if not _ours(issue, allowed):
            return False
        body = str(issue.get("body") or "")
        try:
            new = with_body_events(body, [(at, text)] + body_events(body))
        except ValueError:
            return False
        again = gh.rest("GET", path, etag=FRESH).data
        if not _ours(again, allowed):
            return False
        if str(again.get("body") or "") != body:
            continue
        gh.rest("PATCH", path, OwnBody(body=new))
        return True
    return False


def _ours(issue: Any, allowed: set) -> bool:
    """Whether issue is an issue (not a pull request) one of ours wrote."""
    return (isinstance(issue, dict) and "pull_request" not in issue
            and isinstance(issue.get("body") or "", str) and _login(issue) in allowed)


# -- the section in an issue body (loom#1341) ---------------------------------

def _section(body: Any) -> Optional["re.Match[str]"]:
    found = None
    for found in _SECTION_RE.finditer(str(body or "").replace("\r\n", "\n")):
        pass
    return found


def body_events(body: Any) -> List[Tuple[str, str]]:
    """(UTC time, event text) for each event in the issue body's status
    section, newest first as written; empty when the body has none."""
    found = _section(body)
    return parse(found.group(1)) if found else []


def strip_body(body: Any) -> str:
    """The issue body without its status section (unchanged when it has none)."""
    text = str(body or "").replace("\r\n", "\n")
    found = _section(text)
    if not found:
        return str(body or "")
    parts = (text[:found.start()].strip("\n"), text[found.end():].strip("\n"))
    return "\n\n".join(x for x in parts if x)


def with_body_events(body: Any, events: List[Tuple[str, str]]) -> str:
    """The issue body with its status section replaced by events (newest
    first) at the end, dropping the oldest events past BODY_MAX_CHARS. Raises
    ValueError when not even the newest fits."""
    base = strip_body(body)
    lines = [BODY_BEGIN, "---", "", BODY_HEAD, ""]
    size = len(base) + 2 + sum(len(x) + 1 for x in lines) + len(BODY_END) + 1
    kept = 0
    for at, text in events:
        first, *rest = str(text).splitlines() or [""]
        block = ["- %s %s" % (at, first)] + [_CONT + x for x in rest]
        cost = sum(len(x) + 1 for x in block)
        if size + cost > BODY_MAX_CHARS:
            break
        lines.extend(block)
        size += cost
        kept += 1
    if events and not kept:
        raise ValueError("the status section does not fit in the issue body")
    lines.append(BODY_END)
    return (base + "\n\n" if base.strip() else "") + "\n".join(lines) + "\n"
