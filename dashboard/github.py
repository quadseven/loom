"""Read-only ticket snapshots backed by the bus GitHub client."""
from __future__ import annotations

import os
import json
import ipaddress
import re
import tempfile
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from bus import labels, tickets
from bus.github import DEFAULT_API, GitHub, GitHubError


def repos_from_env() -> List[str]:
    return [repo for repo in os.environ.get("LOOM_REPOS", "").split()
            if tickets.REPO_RE.fullmatch(repo)]


def _label_names(issue: Dict[str, Any]) -> List[str]:
    result = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            result.append(name)
    return result


def _body_field(body: str, key: str) -> Optional[str]:
    match = re.search(r"^%s[ \t]*:[ \t]*(.+?)[ \t]*$" % re.escape(key), body, re.M | re.I)
    return match.group(1) if match else None


def ticket_record(repo: str, issue: Dict[str, Any]) -> Dict[str, Any]:
    names = _label_names(issue)
    body = str(issue.get("body") or "")
    goal_match = re.search(r"^[ \t]*Part of[ \t]+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#?(\d+)", body, re.M | re.I)
    goal = None
    if goal_match:
        goal = (goal_match.group(1) or repo) + "#" + goal_match.group(2)
    return {
        "repo": repo, "number": issue.get("number"), "title": issue.get("title"),
        "state": next((name[5:] for name in names if name in labels.LABELS and name.startswith("loom:")
                        and name not in ("loom:ticket", "loom:goal")), None),
        "goal": goal,
        "lane": next((name[5:] for name in names if name.startswith("lane:")), None),
        "model": _body_field(body, "Model"),
        "difficulty": _body_field(body, "Difficulty"),
        # Attempts and rung are computed from transcripts by the Ticket page.
        "attempts": None, "rung": None,
        "updated_at": issue.get("updated_at"),
    }


class TicketStore:
    def __init__(self, repos: Optional[List[str]] = None, gh: Optional[GitHub] = None,
                 budget_floor: Optional[int] = None) -> None:
        self.repos = list(repos if repos is not None else repos_from_env())
        token = os.environ.get("LOOM_DASHBOARD_GITHUB_TOKEN", "").strip()
        if gh is None:
            api = os.environ.get("LOOM_DASHBOARD_GITHUB_API", DEFAULT_API)
            parsed_api = urlsplit(api)
            loopback_test_api = False
            if parsed_api.hostname:
                try:
                    loopback_test_api = ipaddress.ip_address(parsed_api.hostname).packed == bytes((127, 0, 0, 1))
                except ValueError:
                    pass
            if parsed_api.scheme != "https" and not (
                    parsed_api.scheme == "http" and loopback_test_api):
                raise ValueError("GitHub API base must use https, or a loopback HTTP URL for tests")
            gh = GitHub(token, api=api,
                        cache_dir=tempfile.gettempdir() + "/loom-dashboard-github-cache")
        self.gh = gh
        self.budget_floor = (int(os.environ.get("LOOM_DASHBOARD_BUDGET_FLOOR", "500"))
                             if budget_floor is None else budget_floor)
        self._snapshot = {"tickets": {repo: None for repo in self.repos}, "errors": {},
                          "stale": False, "ready": False}
        self.auth = "token" if token else "none"
        self._snapshot_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None
        self._last_remaining: Optional[int] = None

    @property
    def data(self):
        return self._snapshot["tickets"]

    @property
    def errors(self):
        return self._snapshot["errors"]

    @property
    def stale(self):
        return self._snapshot["stale"]

    @property
    def ready(self):
        return self._snapshot["ready"]

    def start_background(self) -> None:
        if self._refresh_thread is not None:
            return
        interval = max(1, int(os.environ.get("LOOM_DASHBOARD_REFRESH_SECONDS", "60")))

        def run() -> None:
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(interval)

        self._refresh_thread = threading.Thread(target=run, daemon=True)
        self._refresh_thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=2)

    def refresh(self) -> None:
        with self._refresh_lock:
            current = self._snapshot
            data = dict(current["tickets"])
            errors = dict(current["errors"])
            stale = self._last_remaining is not None and self._last_remaining < self.budget_floor
            if not stale:
                for repo in self.repos:
                    if self._last_remaining is not None and self._last_remaining < self.budget_floor:
                        stale = True
                        break
                    try:
                        issues = self.gh.paginate("/repos/%s/issues?labels=loom%%3Aticket&state=all" % quote(repo, safe="/"))
                        data[repo] = [ticket_record(repo, item) for item in issues
                                      if isinstance(item, dict) and "pull_request" not in item
                                      and "loom:ticket" in _label_names(item)]
                        errors.pop(repo, None)
                    except (GitHubError, OSError, ValueError) as exc:
                        reason = str(exc)
                        if isinstance(exc, GitHubError) and exc.body:
                            try:
                                detail = json.loads(exc.body).get("message")
                            except (ValueError, AttributeError):
                                detail = None
                            if detail:
                                reason += ": " + str(detail)
                        errors[repo] = reason
                    remaining = self.gh.rate.get("core", (None, 0, 0))[0]
                    if isinstance(remaining, int):
                        self._last_remaining = remaining
            snapshot = {"tickets": data, "errors": errors, "stale": stale, "ready": True}
            with self._snapshot_lock:
                self._snapshot = snapshot

    def response(self) -> Dict[str, Any]:
        with self._snapshot_lock:
            snapshot = self._snapshot
        return {"tickets": snapshot["tickets"], "errors": snapshot["errors"],
                "stale": snapshot["stale"], "auth": self.auth}
