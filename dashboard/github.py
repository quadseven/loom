"""Read-only ticket snapshots backed by the bus GitHub client."""
from __future__ import annotations

import os
import json
import re
import tempfile
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from bus import labels, tickets
from bus.github import DEFAULT_API, GitHub, GitHubError, token_from_env


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
    goal_match = re.search(r"^(?:Part of|Refs)[ \t]+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#?(\d+)", body, re.M)
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
        "attempts": _body_field(body, "Attempts"), "rung": _body_field(body, "Rung"),
        "updated_at": issue.get("updated_at"),
    }


class TicketStore:
    def __init__(self, repos: Optional[List[str]] = None, gh: Optional[GitHub] = None,
                 budget_floor: Optional[int] = None) -> None:
        self.repos = list(repos if repos is not None else repos_from_env())
        token = os.environ.get("LOOM_DASHBOARD_GITHUB_TOKEN", "").strip()
        if gh is None:
            if not token:
                try:
                    token = token_from_env()
                except GitHubError:
                    token = ""
            gh = GitHub(token, api=os.environ.get("LOOM_DASHBOARD_GITHUB_API", DEFAULT_API),
                        cache_dir=tempfile.gettempdir() + "/loom-dashboard-github-cache")
        self.gh = gh
        self.budget_floor = (int(os.environ.get("LOOM_DASHBOARD_BUDGET_FLOOR", "500"))
                             if budget_floor is None else budget_floor)
        self.data: Dict[str, Optional[List[Dict[str, Any]]]] = {repo: None for repo in self.repos}
        self.errors: Dict[str, str] = {}
        self.stale = False
        self.ready = False
        self._last_remaining: Optional[int] = None

    def refresh(self) -> None:
        if self._last_remaining is not None and self._last_remaining < self.budget_floor:
            self.stale = True
            self.ready = True
            return
        self.stale = False
        for repo in self.repos:
            if self._last_remaining is not None and self._last_remaining < self.budget_floor:
                self.stale = True
                break
            try:
                issues = self.gh.paginate("/repos/%s/issues?labels=loom%%3Aticket&state=all" % quote(repo, safe="/"))
                self.data[repo] = [ticket_record(repo, item) for item in issues
                                   if isinstance(item, dict) and "pull_request" not in item
                                   and "loom:ticket" in _label_names(item)]
                self.errors.pop(repo, None)
            except (GitHubError, OSError, ValueError) as exc:
                reason = str(exc)
                if isinstance(exc, GitHubError) and exc.body:
                    try:
                        detail = json.loads(exc.body).get("message")
                    except (ValueError, AttributeError):
                        detail = None
                    if detail:
                        reason += ": " + str(detail)
                self.errors[repo] = reason
                if self.data[repo] is None:
                    self.data[repo] = None
            self._last_remaining = self.gh.rate.get("core", (None, 0, 0))[0]
        self.ready = True

    def response(self) -> Dict[str, Any]:
        return {"tickets": self.data, "errors": self.errors, "stale": self.stale}
