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

from datetime import datetime, timedelta, timezone

from bus import brain, factory, labels, tickets
from bus.github import DEFAULT_API, GitHub, GitHubError

from . import hot_spots


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
        "refs": re.findall(r"(?:https?://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)", body),
        "html_url": issue.get("html_url"),
        "state": next((name[5:] for name in names if name in labels.LABELS and name.startswith("loom:")
                        and name not in ("loom:ticket", "loom:goal")), None),
        "goal": goal,
        "lane": next((name[5:] for name in names if name.startswith("lane:")), None),
        "model": _body_field(body, "Model"),
        "difficulty": _body_field(body, "Difficulty"),
        # Attempts and rung are computed from transcripts by the Ticket page.
        "attempts": None, "rung": None,
        "updated_at": issue.get("updated_at"),
        "body": body,
        "labels": names,
        "html_url": issue.get("html_url"),
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
                          "stale": False, "ready": False,
                          "improving": {"journals": [], "metrics_comments": []},
                          "lanes": [], "pull_requests": [], "dashboard_errors": [],
                          "reports": [], "brain": {"record": None, "error": "not read yet"},
                          "hot_spots": {}}
        self.operator_items = []
        self.human_counts = {}
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

    def _below_floor(self) -> bool:
        return self._last_remaining is not None and self._last_remaining < self.budget_floor

    @staticmethod
    def _error_reason(exc: Exception) -> str:
        reason = str(exc)
        if isinstance(exc, GitHubError) and exc.body:
            try:
                detail = json.loads(exc.body).get("message")
            except (ValueError, AttributeError):
                detail = None
            if detail:
                reason += ": " + str(detail)
        return reason

    def _read_repo(self, repo: str) -> List[Dict[str, Any]]:
        # Every request goes through bus.github's transport, which applies its TIMEOUT.
        issues = self.gh.paginate("/repos/%s/issues?labels=loom%%3Aticket&state=all" % quote(repo, safe="/"))
        return [ticket_record(repo, item) for item in issues
                if isinstance(item, dict) and "pull_request" not in item
                and "loom:ticket" in _label_names(item)]

    def _read_operator_items(self):
        items, human_counts = [], {}
        for repo in self.repos:
            try:
                asks = self.gh.paginate("/repos/%s/issues?labels=needs-operator&state=open" % quote(repo, safe="/"))
                humans = self.gh.paginate("/repos/%s/issues?labels=loom%%3Ahuman&state=open" % quote(repo, safe="/"))
            except (GitHubError, OSError, ValueError):
                continue
            human_counts[repo] = len([row for row in humans if isinstance(row, dict) and "pull_request" not in row])
            for issue in asks:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                items.append({"repo": repo, "number": issue.get("number"), "title": issue.get("title", ""),
                              "html_url": issue.get("html_url"), "ask": issue.get("body") or issue.get("title", ""),
                              "waiting_since": issue.get("updated_at", "9999"), "waiting_since_source": "updated",
                              "unblocks": 0, "queue": "needs_operator"})
        return items, human_counts

    def _read_dashboard(self, data: Dict[str, Any]) -> None:
        """Read optional view sources; errors stay visible without failing tickets."""
        if self._below_floor():
            data.update({"improving": {"journals": [], "metrics_comments": []},
                         "lanes": [], "pull_requests": [],
                         "dashboard_errors": ["optional view sources were not refreshed because the GitHub request budget is below its floor"]})
            reason = "not refreshed: the GitHub request budget is below its floor"
            data.update({"reports": [], "brain": {"record": None, "error": reason}, "hot_spots": {}})
            return

        journals, metric_comments, prs, lanes, reports = [], [], [], [], []
        errors = []
        homes, status_home = _dashboard_homes()
        for repo, issue_number in homes:
            self._read_dashboard_home(repo, issue_number, journals, metric_comments, lanes, errors, reports)
        if status_home and tickets.REPO_RE.fullmatch(status_home):
            self._read_lane_status(status_home, lanes, errors)
        for repo in self.repos:
            self._read_pull_requests(repo, prs, errors)
        data["improving"] = {"journals": journals, "metrics_comments": metric_comments}
        data["lanes"] = lanes
        data["pull_requests"] = prs
        data["dashboard_errors"] = errors
        data["reports"] = reports
        data["brain"] = self._read_brain_step(status_home)
        data["hot_spots"] = self._read_hot_spot_sources()

    def _read_dashboard_home(self, repo, issue_number, journals, metric_comments, lanes, errors, reports=None):
        try:
            if issue_number:
                issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (quote(repo, safe="/"), issue_number)).data
                issues = [issue] if isinstance(issue, dict) else []
            else:
                issues = self.gh.paginate("/repos/%s/issues?state=all&labels=loom%%3Agoal&per_page=100" % quote(repo, safe="/"))
            for issue in issues:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, int(issue["number"])))
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    body = str(comment.get("body") or "")
                    comment.update({"repo": repo, "issue_number": issue["number"],
                                    "issue_title": issue.get("title")})
                    if body.startswith("<!-- loom:journal v1 -->"):
                        journals.append(comment)
                    if body.startswith("<!-- loom:metrics v1 -->"):
                        metric_comments.append(comment)
                    if body.startswith("<!-- loom:lanes v1 -->"):
                        lanes.append(comment)
                    if reports is not None and body.startswith("<!-- loom:report v1 -->"):
                        reports.append(comment)
        except (GitHubError, OSError, ValueError, KeyError) as exc:
            errors.append("%s: %s" % (repo, self._error_reason(exc)))

    def _read_lane_status(self, status_home, lanes, errors):
        try:
            status_issues = self.gh.paginate(
                "/repos/%s/issues?state=open&labels=loom%%3Alanes&per_page=100"
                % quote(status_home, safe="/"))
            for issue in status_issues:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                comments = self.gh.paginate("/repos/%s/issues/%d/comments"
                                            % (quote(status_home, safe="/"), int(issue["number"])))
                lanes.extend(comment for comment in comments if isinstance(comment, dict)
                             and str(comment.get("body") or "").startswith("<!-- loom:lanes v1 -->"))
        except (GitHubError, OSError, ValueError, KeyError) as exc:
            errors.append("%s lane status: %s" % (status_home, self._error_reason(exc)))

    def _read_brain_step(self, lock_repo):
        """The tick step record (bus.brain step --show), read-only."""
        if not lock_repo or not tickets.REPO_RE.fullmatch(lock_repo):
            return {"record": None, "error": "LOOM_STATUS_REPO is not set, so the lock repo is unknown"}
        try:
            return {"record": brain.tick_step(self.gh, lock_repo), "error": None}
        except (GitHubError, OSError, ValueError, KeyError) as exc:
            return {"record": None, "error": "step record unreadable: %s" % self._error_reason(exc)}

    def _read_hot_spot_sources(self):
        """Station outcome and rung comments, and review bot comments, of the last 14 days."""
        since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bot = (os.environ.get("LOOM_REVIEW_BOT", "").strip() or factory.DEFAULT_REVIEW_BOT).lower()
        issue_comments, review_comments, errors = [], [], []
        for repo in self.repos:
            base = "/repos/%s" % quote(repo, safe="/")
            try:
                issue_comments.extend(
                    {"body": c["body"], "created_at": c.get("created_at"), "issue_url": c.get("issue_url")}
                    for c in self.gh.paginate("%s/issues/comments?since=%s&per_page=100" % (base, since))
                    if isinstance(c, dict) and hot_spots.relevant(str(c.get("body") or "")))
                review_comments.extend(
                    {"body": c["body"], "created_at": c.get("created_at")}
                    for c in self.gh.paginate("%s/pulls/comments?since=%s&per_page=100" % (base, since))
                    if isinstance(c, dict) and c.get("body")
                    and str((c.get("user") or {}).get("login") or "").lower() in (bot, bot + "[bot]"))
            except (GitHubError, OSError, ValueError) as exc:
                errors.append("%s comments: %s" % (repo, self._error_reason(exc)))
                return {"errors": errors}
        return {"issue_comments": issue_comments, "review_comments": review_comments, "errors": errors}

    def _read_pull_requests(self, repo, prs, errors):
        try:
            items = self.gh.paginate("/repos/%s/pulls?state=all&per_page=100" % quote(repo, safe="/"))
            prs.extend(item for item in items if isinstance(item, dict))
        except (GitHubError, OSError, ValueError) as exc:
            errors.append("%s pull requests: %s" % (repo, self._error_reason(exc)))
    def _note_budget(self) -> None:
        remaining = self.gh.rate.get("core", (None, 0, 0))[0]
        if isinstance(remaining, int):
            self._last_remaining = remaining

    def _read_repos(self, data: Dict[str, Any], errors: Dict[str, str]) -> bool:
        """Read each repo into data/errors; return True if the budget floor stopped the pass."""
        for repo in self.repos:
            if self._below_floor():
                return True
            try:
                data[repo] = self._read_repo(repo)
                errors.pop(repo, None)
            except (GitHubError, OSError, ValueError) as exc:
                errors[repo] = self._error_reason(exc)
            self._note_budget()
        return False

    def refresh(self) -> None:
        with self._refresh_lock:
            current = self._snapshot
            data = dict(current["tickets"])
            errors = dict(current["errors"])
            stale = self._read_repos(data, errors) if not self._below_floor() else True
            if self._below_floor():
                operator_items, human_counts = self.operator_items, self.human_counts
            else:
                operator_items, human_counts = self._read_operator_items()
            snapshot = {"tickets": data, "errors": errors, "stale": stale, "ready": True}
            self.operator_items, self.human_counts = operator_items, human_counts
            self._read_dashboard(snapshot)
            with self._snapshot_lock:
                self._snapshot = snapshot

    def response(self) -> Dict[str, Any]:
        with self._snapshot_lock:
            snapshot = self._snapshot
        return {"tickets": snapshot["tickets"], "errors": snapshot["errors"],
                "stale": snapshot["stale"], "auth": self.auth,
                "improving": snapshot.get("improving", {}),
                "lanes": snapshot.get("lanes", []),
                "pull_requests": snapshot.get("pull_requests", []),
                "dashboard_errors": snapshot.get("dashboard_errors", []),
                "reports": snapshot.get("reports", []),
                "brain": snapshot.get("brain", {"record": None, "error": "not read yet"}),
                "hot_spots": snapshot.get("hot_spots", {})}


def _dashboard_homes():
    def goal_ref(value):
        match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#([0-9]+))?", value)
        return (match.group(1), int(match.group(2)) if match.group(2) else None) if match else None

    homes = [goal_ref(value) for value in os.environ.get("LOOM_IMPROVEMENT_HOMES", "").split()]
    homes = [home for home in homes if home]
    status_home = os.environ.get("LOOM_STATUS_REPO", "").strip()
    if status_home and tickets.REPO_RE.fullmatch(status_home):
        homes.append((status_home, None))
    metrics_home = os.environ.get("LOOM_METRICS_GOAL", "").strip()
    metrics_ref = goal_ref(metrics_home) if metrics_home else None
    if metrics_ref:
        homes.append(metrics_ref)
    return list(dict.fromkeys(homes)), status_home
