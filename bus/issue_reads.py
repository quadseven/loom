"""One read of each issue per status or resume pass (loom#2057, loom#2078).

Status reads a lane pull request's ticket in several helpers (the merge
refusals, the docs override, the goal-home rules) and the ticket's epic and
goal home once per pull request. The answers do not change within one pass,
so this client answers a repeated `GET /repos/{owner}/{name}/issues/{n}` from
the first read and sends every other call on unchanged. A failed read is not
kept: the next helper reads again and fails or succeeds as it did before.

A pass also lists the repo's issues first, and GitHub's listing carries the
same issue object a single read answers. So an issue a listing already holds
is answered from it, and so is the single read a ticket's first comments page
makes for its body's status section (GitHub._with_body_events). An issue
listing, and an issue's comments, events or sub-issues, are read once per
pass too. On 2026-10-27 status for quadseven/loom made 270 reads, 95 of them
single-issue reads of issues it had already listed (loom#2078).
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional

from .github import GitHub, Response

# A single issue, not its comments, events, sub-issues or a listing.
ISSUE_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+$")
# An issue listing: its items are whole issues (pull requests included).
LISTING = re.compile(r"^/repos/([^/]+/[^/]+)/issues\?")
# The paginated reads kept for the pass: a listing, or one issue's comments,
# events or sub-issues.
KEPT_LIST = re.compile(
    r"^/repos/[^/]+/[^/]+/issues(?:\?|/\d+/(?:comments|events|sub_issues)(?:\?|$))")


def _with_per_page(path: str) -> str:
    """The path GitHub.paginate reads, so two spellings share one answer."""
    if "per_page=" in path:
        return path
    return path + ("&" if "?" in path else "?") + "per_page=100"


class IssueReads:
    """A GitHub client for one read-only pass that reads each issue once.

    Status helpers may run on several threads; a second reader of the same
    issue or list waits for the first one's answer instead of sending its
    own."""

    def __init__(self, gh: GitHub) -> None:
        self._gh = gh
        self._lock = threading.Lock()
        self._path_locks: Dict[str, threading.Lock] = {}
        self._answers: Dict[str, Response] = {}
        self._lists: Dict[str, List[Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gh, name)

    def _path_lock(self, path: str) -> threading.Lock:
        with self._lock:
            return self._path_locks.setdefault(path, threading.Lock())

    def _issue(self, path: str) -> Response:
        """The issue at `path`, read at most once."""
        with self._path_lock(path):
            with self._lock:
                kept = self._answers.get(path)
            if kept is None:
                kept = self._gh.rest("GET", path)
                with self._lock:
                    self._answers[path] = kept
            return kept

    def rest(self, method: str, path: str, body: Any = None,
             etag: Any = None, ok: Any = ()) -> Response:
        plain = method == "GET" and body is None and etag is None and not ok
        if plain and ISSUE_PATH.match(path):
            return self._issue(path)
        if method == "GET" and body is None and etag is None and ISSUE_PATH.match(path):
            # A read that accepts a failure status (ok=(404,)) is answered
            # from a kept answer, which is a success, but is never kept.
            with self._lock:
                kept = self._answers.get(path)
            if kept is not None:
                return kept
        if plain and isinstance(self._gh, GitHub):
            # A comments page reads its issue's body from this pass's answers.
            return self._gh.rest(method, path, issue=self._issue)
        return self._gh.rest(method, path, body, etag, ok)

    def paginate(self, path: str) -> List[Any]:
        key = _with_per_page(path)
        if not KEPT_LIST.match(key):
            return self._gh.paginate(path)
        with self._path_lock(key):
            with self._lock:
                kept = self._lists.get(key)
            if kept is None:
                kept = self._read_list(path)
                self._keep_listed(key, kept)
                with self._lock:
                    self._lists[key] = kept
            return list(kept)

    def _read_list(self, path: str) -> List[Any]:
        if isinstance(self._gh, GitHub):
            # GitHub's own paginate, its first page read through this
            # client's rest, so a comments page reads its issue once.
            return GitHub.paginate(self, path)  # type: ignore[arg-type]
        return self._gh.paginate(path)

    def recall(self, path: str, validator: str) -> Optional[List[Any]]:
        """GitHub.recall, for GitHub itself; a stand-in keeps nothing."""
        if isinstance(self._gh, GitHub):
            return self._gh.recall(_with_per_page(path), validator)
        return None

    def remember(self, path: str, validator: str, items: List[Any]) -> None:
        if isinstance(self._gh, GitHub):
            self._gh.remember(_with_per_page(path), validator, items)

    def keep_listed(self, repo: str, items: List[Any]) -> None:
        """Answer each item's single read from a listing of the repo's
        issues read without this client (bus.needs_you reads the pages on
        its own threads, loom#2394)."""
        self._keep_listed("/repos/%s/issues?per_page=100" % repo, items)

    def _keep_listed(self, key: str, items: List[Any]) -> None:
        """Answer each listed issue's single read from the listing. Only for
        GitHub itself, whose listing and single read return the same issue
        object; a stand-in client promises no such thing."""
        found = LISTING.match(key)
        if not found or not isinstance(self._gh, GitHub):
            return
        for item in items:
            number = item.get("number") if isinstance(item, dict) else None
            if not isinstance(number, int) or isinstance(number, bool):
                continue
            path = "/repos/%s/issues/%d" % (found.group(1), number)
            with self._lock:
                self._answers.setdefault(path, Response(200, item))


def once(gh: Any) -> Any:
    """gh wrapped so each issue is read once; an already wrapped gh as is."""
    return gh if isinstance(gh, IssueReads) else IssueReads(gh)
