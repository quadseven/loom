"""One read of each issue per status pass (loom#2057).

Status reads a lane pull request's ticket in several helpers (the merge
refusals, the docs override, the goal-home rules) and the ticket's epic and
goal home once per pull request. The answers do not change within one pass,
so this client answers a repeated `GET /repos/{owner}/{name}/issues/{n}` from
the first read and sends every other call on unchanged. A failed read is not
kept: the next helper reads again and fails or succeeds as it did before.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict

from .github import GitHub, Response

# A single issue, not its comments, events, sub-issues or a listing.
ISSUE_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+$")


class IssueReads:
    """A GitHub client for one read-only pass that reads each issue once.

    Status helpers may run on several threads; a second reader of the same
    issue waits for the first one's answer instead of sending its own."""

    def __init__(self, gh: GitHub) -> None:
        self._gh = gh
        self._lock = threading.Lock()
        self._path_locks: Dict[str, threading.Lock] = {}
        self._answers: Dict[str, Response] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gh, name)

    def rest(self, method: str, path: str, body: Any = None,
             etag: Any = None, ok: Any = ()) -> Response:
        if method != "GET" or body is not None or etag is not None or ok \
                or not ISSUE_PATH.match(path):
            return self._gh.rest(method, path, body, etag, ok)
        with self._lock:
            path_lock = self._path_locks.setdefault(path, threading.Lock())
        with path_lock:
            if path not in self._answers:
                self._answers[path] = self._gh.rest(method, path)
            return self._answers[path]


def once(gh: Any) -> Any:
    """gh wrapped so each issue is read once; an already wrapped gh as is."""
    return gh if isinstance(gh, IssueReads) else IssueReads(gh)
