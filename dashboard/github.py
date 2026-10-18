"""Read-only ticket snapshots backed by the bus GitHub client."""
from __future__ import annotations

import os
import json
import ipaddress
import logging
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from bus import brain, labels, tickets
from bus.github import DEFAULT_API, AppToken, GitHub, GitHubError, _next_link
from bus.metrics import waits_for_repos
from .hot_spots import WINDOW as HOT_SPOT_WINDOW
from . import free_view, goals, operators, transcripts, views


READ_WORKERS = 8
# A goal home's title and parent are read again this often (seconds), and at
# most this many goal homes are read in one pass when following parents up.
GOAL_INFO_TTL = 3600
GOAL_INFO_MAX = 200
# Every pull request is read again this often (seconds); between those full
# reads a pass reads only the pull requests updated since the last one (#686).
PR_FULL_READ_SECONDS = 3600
# Every repo's open issues are read for the /free page this often (seconds):
# one paginated list per repo (loom#685).
FREE_ISSUES_SECONDS = 3600
# The dashboard logs the GitHub requests it spent this often (seconds).
REQUEST_LOG_SECONDS = 3600
# The Operators page's claim lists are read again this often (seconds); at
# most this many foreign claim labels are listed per repo, and at most this
# many open pull requests' checks are read in one pass (loom#786).
OPERATOR_CLAIMS_SECONDS = 900
OPERATOR_CLAIM_LABELS = 10
OPERATOR_CHECK_READS = 30
LOG = logging.getLogger(__name__)


def repos_from_env() -> List[str]:
    return [repo for repo in os.environ.get("LOOM_REPOS", "").split()
            if tickets.REPO_RE.fullmatch(repo)]


def app_from_env(env: Optional[Dict[str, str]] = None) -> Optional[AppToken]:
    """The GitHub App installation token when the App settings are present
    and this host can mint one (openssl signs the JWT; the key is a readable
    LOOM_GITHUB_APP_KEY_FILE, or the aws CLI reads it from the parameter
    store); else None, and the dashboard keeps its own token."""
    try:
        app = AppToken.from_env(env)
    except GitHubError as exc:
        LOG.warning("ignoring the GitHub App settings: %s", exc)
        return None
    if app is None:
        return None
    if app.key_source == "file":
        try:
            with open(app.key_file, "rb"):
                pass
        except OSError as exc:
            # The file's name only: never its contents.
            LOG.warning("GitHub App settings ignored: key file %s is unreadable (%s)",
                        app.key_file, type(exc).__name__)
            return None
    tools = ("openssl",) if app.key_source == "file" else ("openssl", "aws")
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        LOG.warning("GitHub App settings ignored: %s not installed", " and ".join(missing))
        return None
    return app


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


def _user_login(value: Any) -> Optional[str]:
    login = value.get("login") if isinstance(value, dict) else None
    return str(login).lower() if login else None


def _logins(values: Any) -> List[str]:
    """The lower-case logins of a list of GitHub user objects."""
    return [login for login in (_user_login(v) for v in values or []) if login]


def ticket_record(repo: str, issue: Dict[str, Any]) -> Dict[str, Any]:
    names = _label_names(issue)
    body = str(issue.get("body") or "")
    goal_match = re.search(r"^[ \t]*Part of[ \t]+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#?(\d+)", body, re.M | re.I)
    goal = None
    if goal_match:
        goal = (goal_match.group(1) or repo) + "#" + goal_match.group(2)
    else:
        # No Part of line: the sub-issue parent names the epic (or goal).
        goal = goals.issue_parent(issue)
    return {
        "repo": repo, "number": issue.get("number"), "title": issue.get("title"),
        "refs": re.findall(r"(?:https?://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)", body),
        "html_url": issue.get("html_url"),
        "state": next((name[5:] for name in names if name in labels.LABELS and name.startswith("loom:")
                        and name not in ("loom:ticket", "loom:goal", "loom:runaway")), None),
        # The issue the ticket names: an epic or, before tickets moved under
        # epics, a goal. TicketStore._assign_epics moves an epic here to
        # "epic" and puts the epic's goal in "goal" (decision 0003).
        "goal": goal,
        "epic": None,
        # Tickets are read with state=all; a closed one is history, never work.
        "open": issue.get("state", "open") != "closed",
        "closed_at": issue.get("closed_at"),
        "state_reason": issue.get("state_reason"),
        "lane": next((name[5:] for name in names if name.startswith("lane:")), None),
        "model": _body_field(body, "Model"),
        "difficulty": _body_field(body, "Difficulty"),
        "priority": tickets.priority_of(issue),
        "created_at": issue.get("created_at"),
        # Attempts and rung are computed from transcripts by the Ticket page.
        "attempts": None, "rung": None,
        "updated_at": issue.get("updated_at"),
        "body": body,
        "labels": names,
        "html_url": issue.get("html_url"),
        # Who owns it (dashboard/operators.py): the assignees and the author,
        # read with the claim:<login> labels by bus.tickets.owned_elsewhere.
        "assignees": _logins(issue.get("assignees")),
        "author": _user_login(issue.get("user")),
    }


# What the views read from a pull request in the list (dashboard/results.py,
# views.py, ticket_data.py and server.py) and from an issue or review comment
# (hot_spots.py, goals.py, results.py, tickets_table.py). The list answers
# embed the base and head repositories and the user in full: about 17 KB of
# JSON per pull request. Kept whole, the 6,700 pull requests of 17 repos held
# about 190 MiB and two weeks of comments about 150 MiB more, twice over while
# a refresh built the next snapshot beside the one being served, and the
# dashboard was killed at its 1 GiB memory limit every few refreshes
# (2026-10-17).
PULL_REQUEST_FIELDS = ("number", "title", "html_url", "state", "draft", "body",
                       "created_at", "updated_at", "merged_at", "closed_at", "repo")
COMMENT_FIELDS = ("id", "body", "created_at", "updated_at", "html_url",
                  "issue_url", "pull_request_url", "path")


def _user(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    return {"login": value.get("login"), "type": value.get("type")}


def pull_request_record(pr: Dict[str, Any]) -> Dict[str, Any]:
    """The fields of a pull request list item that the views read."""
    out = {key: pr[key] for key in PULL_REQUEST_FIELDS if key in pr}
    if isinstance(pr.get("user"), dict):
        out["user"] = _user(pr["user"])
    # Ownership (dashboard/operators.py): assignees and claim:<login> labels.
    if "assignees" in pr:
        out["assignees"] = _logins(pr.get("assignees"))
    if "labels" in pr:
        out["labels"] = _label_names(pr)
    head, base = pr.get("head"), pr.get("base")
    if isinstance(head, dict):
        out["head"] = {"ref": head.get("ref"), "sha": head.get("sha")}
    if isinstance(base, dict):
        base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
        out["base"] = {"ref": base.get("ref"), "repo": {"full_name": base_repo.get("full_name")}}
    return out


def comment_record(comment: Dict[str, Any], repo: str) -> Dict[str, Any]:
    """The fields of an issue or review comment that the views read."""
    out = {key: comment[key] for key in COMMENT_FIELDS if key in comment}
    if isinstance(comment.get("user"), dict):
        out["user"] = _user(comment["user"])
    out["repo"] = repo
    return out


def _refresh_interval() -> int:
    """Seconds the background thread waits between refresh passes."""
    return max(1, int(os.environ.get("LOOM_DASHBOARD_REFRESH_SECONDS", "60")))


def _newest_update(records: List[Dict[str, Any]], newest: Optional[str]) -> Optional[str]:
    """The newest updated_at among the records and newest; None when there is none."""
    stamps = [str(r["updated_at"]) for r in records if r.get("updated_at")]
    if newest:
        stamps.append(newest)
    return max(stamps) if stamps else None


def _unique_by_number(records) -> List[Dict[str, Any]]:
    """The records in order, each number once (a pull request opened during a
    full read shifts the pages, so the next page repeats one)."""
    seen, out = set(), []
    for record in records:
        number = record.get("number")
        if number is not None and number in seen:
            continue
        seen.add(number)
        out.append(record)
    return out


def _int_or(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# The view sources one dashboard pass reads, each with the snapshot keys it
# fills. A pass below the budget floor reads none of them and carries every
# source an earlier pass read; source_read_at says when each was read.
VIEW_SOURCES = {
    "improving": ("improving", "floor_context"),
    "lanes": ("lanes",),
    "pull_requests": ("pull_requests", "footer_models", "footer_runs"),
    "brain_step": ("brain_step", "brain_step_error"),
    "comments": ("issue_comments", "review_comments", "comment_errors", "hot_spots_error"),
}


class TicketStore:
    def __init__(self, repos: Optional[List[str]] = None, gh: Optional[GitHub] = None,
                 budget_floor: Optional[int] = None) -> None:
        self.repos = list(repos if repos is not None else repos_from_env())
        token = os.environ.get("LOOM_DASHBOARD_GITHUB_TOKEN", "").strip()
        app = app_from_env() if gh is None else None
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
            gh = GitHub(app if app is not None else token, api=api,
                        cache_dir=tempfile.gettempdir() + "/loom-dashboard-github-cache")
        self.gh = gh
        self.budget_floor = (int(os.environ.get("LOOM_DASHBOARD_BUDGET_FLOOR", "500"))
                             if budget_floor is None else budget_floor)
        self._snapshot = {"tickets": {repo: None for repo in self.repos}, "errors": {},
                          "stale": False, "ready": False,
                          "ticket_refreshed_at": None, "next_refresh_at": None, "stale_reason": "",
                          "improving": {"journals": [], "metrics_comments": []},
                          "lanes": [], "pull_requests": [], "dashboard_errors": [],
                          "goal_context": {},
                          "goal_context_error": "the dashboard has not read its sources yet"}
        self.operator_items = []
        self.human_counts = {}
        # repo -> open issue records for the /free page, None until read.
        self.open_issues: Dict[str, Optional[List[Dict[str, Any]]]] = {repo: None for repo in self.repos}
        self.open_issues_error = ""
        self.open_issues_read_at = 0.0
        self.operator_error = "LOOM_REPOS is not set" if not self.repos else ""
        self.auth = "app" if getattr(gh, "auth", "") == "app" else "token" if token else "none"
        self._snapshot_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None
        self._last_remaining: Optional[int] = None
        self._reset_at = 0
        self.waits = {}
        self.waits_error = "waits have not been looked up yet"
        self._waits_refreshed_at = 0.0
        # repo -> {"prs": records newest-created first, "newest": the newest
        # updated_at read, "full_at": when every page was last read}.
        self._kept_prs: Dict[str, Dict[str, Any]] = {}
        self._kept_lock = threading.Lock()
        self._request_mark: Optional[Tuple[float, Dict[str, int], int]] = None

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
        interval = _refresh_interval()

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
        """Whether the last reading was under the floor and GitHub has not yet
        restored the window. A pass below the floor reads nothing, so nothing
        updates the reading: without the reset time the dashboard stayed on
        "stale ticket data" for a day after the budget came back (2026-10-16:
        4,265 requests left, still latched)."""
        if self._last_remaining is None or self._last_remaining >= self.budget_floor:
            return False
        return not (self._reset_at and time.time() >= self._reset_at)

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
        ask_errors = []
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            results = list(pool.map(self._read_operator_repo, self.repos))
        for repo, found, count, error in results:
            if error:
                ask_errors.append(error)
            else:
                human_counts[repo] = count
                items.extend(found)
        ask_error = "; ".join(ask_errors) if self.repos else "LOOM_REPOS is not set"
        return items, human_counts, ask_error

    def _read_operator_repo(self, repo):
        try:
            asks = self.gh.paginate("/repos/%s/issues?labels=needs-operator&state=open" % quote(repo, safe="/"))
            humans = self.gh.paginate("/repos/%s/issues?labels=loom%%3Ahuman&state=open" % quote(repo, safe="/"))
        except (GitHubError, OSError, ValueError) as exc:
            return repo, [], 0, "%s operator items: %s" % (repo, self._error_reason(exc))
        count = len([row for row in humans if isinstance(row, dict) and "pull_request" not in row])
        found = [{"repo": repo, "number": issue.get("number"), "title": issue.get("title", ""),
                  "html_url": issue.get("html_url"), "ask": issue.get("body") or issue.get("title", ""),
                  "created_at": issue.get("created_at"),
                  "waiting_since": issue.get("updated_at", "9999"), "waiting_since_source": "updated",
                  "unblocks": 0, "queue": "needs_operator"}
                 for issue in asks if isinstance(issue, dict) and "pull_request" not in issue]
        return repo, found, count, ""

    def _read_open_issues(self, now: float) -> None:
        """Each repo's open issues for the /free page, at most once every
        FREE_ISSUES_SECONDS and never below the budget floor; a repo that
        fails keeps its last read and the error is shown on the page."""
        if now - self.open_issues_read_at < FREE_ISSUES_SECONDS or self._below_floor():
            return
        errors = []
        for repo in self.repos:
            try:
                found = self.gh.paginate("/repos/%s/issues?state=open" % quote(repo, safe="/"))
            except (GitHubError, OSError, ValueError) as exc:
                errors.append("%s: %s" % (repo, self._error_reason(exc)))
                continue
            self.open_issues[repo] = [free_view.open_issue_record(item) for item in found
                                      if isinstance(item, dict) and "pull_request" not in item]
        self.open_issues_error = "; ".join(errors)
        self.open_issues_read_at = now

    def _skip_dashboard_below_floor(self, data: Dict[str, Any]) -> None:
        reset = ("at %s UTC" % datetime.fromtimestamp(self._reset_at, timezone.utc).strftime("%H:%M")
                 if self._reset_at else "when GitHub's request limit resets")
        skipped = "GitHub's request budget ran low; these details refresh %s" % reset
        data.update({"improving": {"journals": [], "metrics_comments": []},
                     "lanes": [], "pull_requests": [],
                     "brain_step": None, "brain_step_error": skipped,
                     "issue_comments": [], "review_comments": [], "hot_spots_error": skipped,
                     "dashboard_errors": [skipped]})
        # Every view source an earlier pass read keeps that read, with its
        # read time. Emptying them showed false zeros for as long as the
        # budget stayed low: Improving 0/0/0/0, Lanes "no loom:lanes
        # comment", Brain "no goal journal found", Results "Lane pull
        # requests merged 0" and a PR column of "-" (loom#779).
        read_at = dict(self._snapshot.get("source_read_at") or {})
        carried = [source for source in VIEW_SOURCES if read_at.get(source)]
        for source in carried:
            data.update({key: self._snapshot[key] for key in VIEW_SOURCES[source] if key in self._snapshot})
        data["source_read_at"], data["sources_carried"] = read_at, carried
        # The goal cards keep the last pass that read them. Dropping them
        # here left every Goals card at "-" with "No report on this goal
        # in the last two weeks" whenever the budget dipped (2026-10-17).
        # The Operators page keeps the last claims read, too.
        data["operator_claims"] = self._snapshot.get("operator_claims") or {}
        previous = self._snapshot.get("goal_context")
        data["goal_context"] = previous or {}
        data["goal_context_error"] = self._snapshot.get("goal_context_error", "") if previous else (
            "GitHub goal comments were not read: the request budget ran low and no earlier pass read them; "
            "this refreshes %s" % reset)

    def _read_goal_context(self, data: Dict[str, Any], homes, reports, metric_comments) -> None:
        rows = [row for found in data.get("tickets", {}).values() if found for row in found]
        configured = set(self.repos) | {repo for repo, _ in homes}
        titles, parents = self._goal_info(rows, configured)
        epics = self._assign_epics(rows)
        # Reports on the goal homes read in full above count too: a goal home
        # in LOOM_IMPROVEMENT_HOMES keeps its report even if its repo's
        # comment feed failed this pass.
        unread = dict(data.get("comment_errors") or {})
        for repo in {str(row.get("goal") or "").partition("#")[0] for row in rows if row.get("goal")}:
            if repo not in self.repos and (repo, None) not in homes:
                unread.setdefault(repo, "%s comments: the repo is not in LOOM_REPOS, so its comments"
                                        " are not read" % repo)
        data["goal_context"] = goals.goal_context(
            rows, list(data.get("issue_comments", [])) + reports, titles, metric_comments,
            os.environ.get("LOOM_METRICS_GOAL", "").strip() or None, datetime.now(timezone.utc),
            parents=parents, unread=unread, epics=epics)
        data["goal_context_error"] = ""

    def _read_dashboard(self, data: Dict[str, Any]) -> None:
        """Read optional view sources; errors stay visible without failing tickets."""
        if self._below_floor():
            self._skip_dashboard_below_floor(data)
            return

        journals, reports, metric_comments, prs, lanes = [], [], [], [], []
        errors = []
        homes, status_home = _dashboard_homes()
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            home_results = list(pool.map(lambda home: self._dashboard_home_result(*home), homes))
        for found_journals, found_reports, found_metrics, found_lanes, found_errors in home_results:
            journals.extend(found_journals)
            reports.extend(found_reports)
            metric_comments.extend(found_metrics)
            lanes.extend(found_lanes)
            errors.extend(found_errors)
        if status_home and tickets.REPO_RE.fullmatch(status_home):
            self._read_lane_status(status_home, lanes, errors)
        # The comment feed keeps only LOOM_BRIEF_AUTHORS logins when that is
        # set; the goal-home reads get the same filter here, before any view
        # reads them (Floor standup and next move, Improving, Lanes, Goals),
        # so a marker-shaped comment from anyone else counts nowhere.
        trusted = {login.lower() for login in brain.trusted_authors()}
        journals, reports, metric_comments, lanes = (
            [c for c in found if _by_trusted(c, trusted)]
            for found in (journals, reports, metric_comments, lanes))
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            pull_results = list(pool.map(self._pull_requests_for_repo, self.repos))
        for found, repo_errors in pull_results:
            prs.extend(found)
            errors.extend(repo_errors)
        data["improving"] = {"journals": journals, "metrics_comments": metric_comments}
        data["floor_context"] = _floor_context(reports, journals, self.operator_items, errors)
        data["lanes"] = lanes
        data["pull_requests"] = prs
        # One pass at refresh, not per page (server.footer_models).
        data["footer_models"] = views.footer_models(prs)
        data["footer_runs"] = views.footer_runs(prs)
        data["dashboard_errors"] = errors
        data["brain_step"], data["brain_step_error"] = self._read_brain_step(status_home)
        self._read_hot_spot_comments(data)
        self._read_goal_context(data, homes, reports, metric_comments)
        now = int(time.time())
        data["source_read_at"], data["sources_carried"] = {source: now for source in VIEW_SOURCES}, []
        self._read_operator_claims(data)

    def _read_operator_claims(self, data: Dict[str, Any]) -> None:
        """The Operators page's source (loom#786): every open item another
        operator holds, from one list per foreign claim:<login> label per repo
        and the ticket and pull request records this pass already read, with
        the checks of each open linked pull request. Read again only every
        OPERATOR_CLAIMS_SECONDS; between reads the last result is kept. A
        repo whose claims could not be read keeps its last items, says why,
        and is read again on the next pass."""
        previous = self._snapshot.get("operator_claims") or {}
        now = time.time()
        if (previous.get("read_at") and not previous.get("retry")
                and now - float(previous["read_at"]) < OPERATOR_CLAIMS_SECONDS):
            data["operator_claims"] = previous
            return
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            results = list(pool.map(self._claims_for_repo, self.repos))
        claimed, errors = [], []
        for repo, (found, error) in zip(self.repos, results, strict=True):
            if error:
                errors.append(error)
                found = [item for item in previous.get("items") or [] if item.get("repo") == repo]
            claimed.extend(found)
        rows = [row for found in (data.get("tickets") or {}).values() if found for row in found]
        items = operators.collect(claimed, rows, data.get("pull_requests") or [])
        checks = self._read_claim_checks(items, errors)
        data["operator_claims"] = {"items": items, "checks": checks, "error": "; ".join(errors),
                                   "read_at": now, "every": OPERATOR_CLAIMS_SECONDS, "retry": bool(errors)}

    def _claims_for_repo(self, repo: str) -> Tuple[List[Dict[str, Any]], str]:
        path = quote(repo, safe="/")
        found: List[Dict[str, Any]] = []
        try:
            names = [label.get("name") for label in self.gh.paginate("/repos/%s/labels?per_page=100" % path)
                     if isinstance(label, dict) and isinstance(label.get("name"), str)]
            for name in operators.foreign_claim_labels(names)[:OPERATOR_CLAIM_LABELS]:
                issues = self.gh.paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
                                          % (path, quote(name, safe="")))
                found.extend(operators.claim_record(repo, issue) for issue in issues if isinstance(issue, dict))
        except (GitHubError, OSError, ValueError) as exc:
            return found, "%s claims: %s" % (repo, self._error_reason(exc))
        return found, ""

    def _read_claim_checks(self, items, errors) -> Dict[str, Dict[str, Any]]:
        """repo@sha -> operators.checks_summary of each open linked pull
        request's head, at most OPERATOR_CHECK_READS reads a pass (an
        unchanged head answers 304 from the GitHub client's ETag cache)."""
        out: Dict[str, Dict[str, Any]] = {}
        for repo, sha in operators.checks_wanted(items)[:OPERATOR_CHECK_READS]:
            try:
                resp = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100"
                                    % (quote(repo, safe="/"), quote(sha, safe="")))
            except (GitHubError, OSError, ValueError) as exc:
                errors.append("%s checks: %s" % (repo, self._error_reason(exc)))
                continue
            runs = resp.data.get("check_runs") if isinstance(resp.data, dict) else None
            out["%s@%s" % (repo, sha)] = operators.checks_summary(runs or [])
        return out

    def _goal_info(self, rows, configured=None) -> Tuple[Dict[str, Optional[str]], Dict[str, Optional[str]]]:
        """Each goal home's title and the goal it rolls up into (a `Parent
        goal:` line or a sub-issue parent), read for every goal the tickets
        name and every parent above them. A goal home is read again after
        GOAL_INFO_TTL (a parent line can be added at any time; a conditional
        read of an unchanged issue costs no request budget); an unreadable
        one stays None and is read again next pass.

        A parent counts only in a repo the dashboard is configured for
        (`configured`: LOOM_REPOS and the goal-home repos), and only when it is
        itself a goal home: a goal the tickets name, or an issue labelled
        loom:goal. Any other parent is never read, and the goal stays
        top-level."""
        configured = set(self.repos) if configured is None else set(configured)
        cache = self.__dict__.setdefault("_goal_info_cache", {})
        named = {row["goal"] for row in rows if row.get("goal")}
        pending = sorted(named)
        seen = set()
        now = time.time()
        while pending and len(seen) < GOAL_INFO_MAX:
            goal = pending.pop(0)
            if goal in seen:
                continue
            seen.add(goal)
            entry = cache.get(goal)
            if not entry or not entry.get("title") or now - entry.get("read_at", 0) >= GOAL_INFO_TTL:
                entry = self._read_goal_home(goal, entry, now, configured)
                cache[goal] = entry
            if entry.get("parent") and entry["parent"] not in seen:
                pending.append(entry["parent"])
            if entry.get("is_epic") and entry.get("epic_goal") and entry["epic_goal"] not in seen:
                pending.append(entry["epic_goal"])

        def accepted(parent):
            if not parent or parent.partition("#")[0] not in configured:
                return None
            return parent if parent in named or (cache.get(parent) or {}).get("is_goal") else None

        titles = {goal: entry.get("title") for goal, entry in cache.items()}
        # An epic is not a smaller goal: it never rolls up as one.
        parents = {goal: None if entry.get("is_epic") else accepted(entry.get("parent"))
                   for goal, entry in cache.items()}
        return titles, parents

    def _assign_epics(self, rows) -> Dict[str, Dict[str, Any]]:
        """Move each ticket whose named issue is an epic (labelled epic) to
        `epic`, with the epic's goal in `goal`, from the goal-home reads
        _goal_info just made (no request of its own). Returns each epic the
        tickets name: its title, whether it is open, and its goal."""
        cache = self.__dict__.get("_goal_info_cache") or {}
        epics: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            named = row.get("epic") or row.get("goal")
            entry = cache.get(named) if named else None
            if not entry or not entry.get("is_epic"):
                continue
            row["epic"], row["goal"] = named, entry.get("epic_goal")
            epics[named] = {"title": entry.get("title"), "open": entry.get("open", True),
                            "goal": entry.get("epic_goal")}
        return epics

    def _read_goal_home(self, goal, entry, now, configured):
        repo, _, number = goal.partition("#")
        try:
            issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (quote(repo, safe="/"), int(number))).data
        except (GitHubError, OSError, ValueError):
            # Keep what an earlier read found; try again next pass.
            return dict(entry or {}, read_at=0)
        if not isinstance(issue, dict):
            return {"title": None, "parent": None, "is_goal": False, "read_at": 0}
        names = [name.lower() for name in _label_names(issue)]
        is_epic = "epic" in names and "loom:goal" not in names
        return {"title": str(issue.get("title") or "") or None,
                "parent": goals.parent_of(repo, issue, configured),
                "is_goal": "loom:goal" in names, "is_epic": is_epic,
                "epic_goal": goals.epic_goal_of(repo, issue, configured) if is_epic else None,
                "open": issue.get("state", "open") != "closed", "read_at": now}

    def _read_brain_step(self, status_home):
        """The brain's tick record from the lock branch (bus.brain step --show)."""
        if not (status_home and tickets.REPO_RE.fullmatch(status_home)):
            return None, "LOOM_STATUS_REPO is not set, so the lock branch is unknown"
        try:
            return brain.tick_step(self.gh, status_home), ""
        except (GitHubError, OSError, ValueError, KeyError, TypeError) as exc:
            return None, "%s lock branch: %s" % (status_home, self._error_reason(exc))

    def _read_hot_spot_comments(self, data):
        """Issue and review comments from the last two weeks, one feed per repo.
        The window starts at a UTC midnight, so the URL holds all day and a
        refresh is usually a 304. With LOOM_BRIEF_AUTHORS set, only those
        logins' issue comments count as station outcomes and rungs."""
        day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        since = (day - 2 * HOT_SPOT_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        trusted = {login.lower() for login in brain.trusted_authors()}
        issue_comments, review_comments, errors = [], [], []
        comment_errors = {}
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            results = list(pool.map(lambda repo: self._comments_for_repo(repo, since, trusted), self.repos))
        for repo, (issues, reviews, error) in zip(self.repos, results, strict=True):
            issue_comments.extend(issues)
            review_comments.extend(reviews)
            if error:
                errors.append(error)
                comment_errors[repo] = error
        data["issue_comments"], data["review_comments"] = issue_comments, review_comments
        data["comment_errors"] = comment_errors
        data["hot_spots_error"] = "; ".join(errors) if errors else (
            "" if self.repos else "LOOM_REPOS is not set")

    def _comments_for_repo(self, repo, since, trusted):
        path = quote(repo, safe="/")
        issues, reviews = [], []
        try:
            found = self.gh.paginate("/repos/%s/issues/comments?since=%s&per_page=100" % (path, since))
            issues = [comment_record(c, repo) for c in found if isinstance(c, dict) and _by_trusted(c, trusted)]
            found = self.gh.paginate("/repos/%s/pulls/comments?since=%s&per_page=100" % (path, since))
            reviews = [comment_record(c, repo) for c in found if isinstance(c, dict)]
        except (GitHubError, OSError, ValueError) as exc:
            return issues, reviews, "%s comments: %s" % (repo, self._error_reason(exc))
        return issues, reviews, ""

    def _read_dashboard_home(self, repo, issue_number, journals, reports, metric_comments, lanes, errors):
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
                    if body.startswith("<!-- loom:report v1 -->"):
                        reports.append(comment)
                    if body.startswith("<!-- loom:metrics v1 -->"):
                        metric_comments.append(comment)
                    if body.startswith("<!-- loom:lanes v1 -->") or body.startswith("<!-- loom:remote-heartbeat v1"):
                        lanes.append(comment)
        except (GitHubError, OSError, ValueError, KeyError) as exc:
            errors.append("%s: %s" % (repo, self._error_reason(exc)))

    def _dashboard_home_result(self, repo, issue_number):
        journals, reports, metrics, lanes, errors = [], [], [], [], []
        self._read_dashboard_home(repo, issue_number, journals, reports, metrics, lanes, errors)
        return journals, reports, metrics, lanes, errors

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
                             and (str(comment.get("body") or "").startswith("<!-- loom:lanes v1 -->")
                                  or str(comment.get("body") or "").startswith("<!-- loom:remote-heartbeat v1")))
        except (GitHubError, OSError, ValueError, KeyError) as exc:
            errors.append("%s lane status: %s" % (status_home, self._error_reason(exc)))

    def _read_pull_requests(self, repo, prs, errors):
        """The repo's pull requests, newest created first.

        Each page of pulls?state=all embeds the repo itself, whose pushed_at
        changes on every push, so one push changed every page's ETag and a
        refresh paid for all of them (2026-10-17: 40 pages a minute for the
        largest repo). Between full reads, a pass asks for the pull requests
        sorted by update, newest first, and stops at the first page that
        reaches back past the newest update it already has: one page, most
        minutes. A full read every PR_FULL_READ_SECONDS catches anything the
        incremental reads could miss."""
        path = quote(repo, safe="/")
        with self._kept_lock:
            kept = self._kept_prs.get(repo)
        now = time.time()
        try:
            if kept and kept.get("newest") and now - kept["full_at"] < PR_FULL_READ_SECONDS:
                records = self._read_pull_requests_since(path, kept)
                kept = dict(kept, prs=records, newest=_newest_update(records, kept["newest"]))
            else:
                items = self.gh.paginate_parallel("/repos/%s/pulls?state=all&per_page=100" % path,
                                                  workers=READ_WORKERS)
                records = _unique_by_number(
                    pull_request_record(item) for item in items if isinstance(item, dict))
                kept = {"prs": records, "newest": _newest_update(records, None), "full_at": now}
        except (GitHubError, OSError, ValueError) as exc:
            errors.append("%s pull requests: %s" % (repo, self._error_reason(exc)))
            return
        with self._kept_lock:
            self._kept_prs[repo] = kept
        prs.extend(kept["prs"])

    def _read_pull_requests_since(self, path, kept):
        """The kept records with every pull request updated at or after the
        kept newest update read again, merged by number."""
        newest = kept["newest"]
        changed: Dict[Any, Dict[str, Any]] = {}
        url: Optional[str] = "/repos/%s/pulls?state=all&sort=updated&direction=desc&per_page=100" % path
        while url:
            resp = self.gh.rest("GET", url)
            page = [item for item in (resp.data if isinstance(resp.data, list) else [])
                    if isinstance(item, dict)]
            for item in page:
                record = pull_request_record(item)
                changed.setdefault(record.get("number"), record)
            oldest = str(page[-1].get("updated_at") or "") if page else ""
            # GitHub's timestamps are ISO 8601 UTC, so text order is time order.
            if not page or not oldest or oldest < newest:
                break
            url = _next_link(resp.headers.get("link", ""))
        records = []
        for record in kept["prs"]:
            records.append(changed.pop(record.get("number"), record))
        # What is left was opened since: newer numbers go first.
        fresh = sorted(changed.values(), key=lambda r: _int_or(r.get("number")), reverse=True)
        return fresh + records

    def _log_requests(self, now: float) -> None:
        """Log the requests this dashboard spent since the last such line, so
        its share of the hourly GitHub budget can be read from its log."""
        kinds = dict(getattr(self.gh, "kinds", {}) or {})
        not_modified = int(getattr(self.gh, "not_modified", 0) or 0)
        if self._request_mark is None:
            self._request_mark = (now, kinds, not_modified)
            return
        since, before, before_304 = self._request_mark
        if now - since < REQUEST_LOG_SECONDS:
            return
        spent = {kind: count - before.get(kind, 0) for kind, count in kinds.items()}
        LOG.info("GitHub requests in the last %d minutes: %d counted (%s), %d not modified",
                 int((now - since) / 60), sum(spent.values()),
                 ", ".join("%s %d" % (kind, n) for kind, n in sorted(spent.items()) if n) or "none",
                 not_modified - before_304)
        self._request_mark = (now, kinds, not_modified)

    def _pull_requests_for_repo(self, repo):
        prs, errors = [], []
        self._read_pull_requests(repo, prs, errors)
        return prs, errors

    def _note_budget(self) -> None:
        remaining, _, reset = self.gh.rate.get("core", (None, 0, 0))
        if isinstance(remaining, int):
            self._last_remaining = remaining
            self._reset_at = reset if isinstance(reset, int) else 0

    def _read_repos(self, data: Dict[str, Any], errors: Dict[str, str]) -> bool:
        """Read each repo into data/errors; return True if the budget floor stopped the pass."""
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            results = list(pool.map(self._read_repo_result, self.repos))
        for repo, found, error in results:
            if error:
                errors[repo] = error
            else:
                data[repo] = found
                errors.pop(repo, None)
        self._note_budget()
        return self._below_floor()

    def _read_repo_result(self, repo):
        try:
            return repo, self._read_repo(repo), ""
        except (GitHubError, OSError, ValueError) as exc:
            return repo, None, self._error_reason(exc)

    def _read_repeated_failures(self):
        """Open repeated-failure invariant issues across configured repos."""
        found = []
        for repo in self.repos:
            issues = self.gh.paginate("/repos/%s/issues?labels=loom%%3Ainvariant&state=open" % quote(repo, safe="/"))
            for issue in issues:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                if "repeated-failure" not in str(issue.get("body") or ""):
                    continue
                found.append({"repo": repo, "number": issue.get("number"),
                              "title": issue.get("title") or "Repeated failure invariant",
                              "html_url": issue.get("html_url"), "open": True})
        return found
    def _stale_metadata(self, stale: bool, errors: Dict[str, str], current: Dict[str, Any]):
        refreshed_at = current.get("ticket_refreshed_at")
        if not stale and not errors:
            refreshed_at = time.time()
        below_floor = self._below_floor()
        # Below the floor a pass reads nothing, so only GitHub's reset time
        # ends the stale spell; without one the next refresh is unknown.
        reset_at = (self._reset_at or None) if below_floor else int(time.time()) + _refresh_interval()
        stale_reason = ("GitHub's request budget ran low" if below_floor else
                        "GitHub could not refresh ticket data" if errors else "")
        return refreshed_at, reset_at, stale_reason

    def _refresh_waits(self, snapshot: Dict[str, Any], now: float) -> None:
        if now - self._waits_refreshed_at < 3600 or self._below_floor():
            snapshot["wait_rows"] = self._snapshot.get("wait_rows", [])
            snapshot["wait_bottleneck"] = self._snapshot.get("wait_bottleneck")
            snapshot["state_entered"] = self._snapshot.get("state_entered", {})
            return
        try:
            entered = {}
            rows, bottleneck = waits_for_repos(self.gh, self.repos, now, entered)
            self.waits = {row["state"]: [row["median_h"]] * row["n"]
                          for row in rows if row.get("median_h") is not None and row.get("n")}
            snapshot["wait_rows"] = rows
            snapshot["wait_bottleneck"] = bottleneck
            snapshot["state_entered"] = entered
            self.waits_error = ""
        except (GitHubError, OSError, ValueError) as exc:
            self.waits_error = self._error_reason(exc)
            snapshot["wait_rows"] = self._snapshot.get("wait_rows", [])
            snapshot["wait_bottleneck"] = self._snapshot.get("wait_bottleneck")
            snapshot["state_entered"] = self._snapshot.get("state_entered", {})
        self._waits_refreshed_at = now

    def refresh(self) -> None:
        with self._refresh_lock:
            started = time.monotonic()
            current = self._snapshot
            data = dict(current["tickets"])
            errors = dict(current["errors"])
            stale = self._read_repos(data, errors) if not self._below_floor() else True
            if self._below_floor():
                operator_items, human_counts, ask_error = self.operator_items, self.human_counts, self.operator_error
            else:
                operator_items, human_counts, ask_error = self._read_operator_items()
            refreshed_at, reset_at, stale_reason = self._stale_metadata(stale, errors, current)
            snapshot = {"tickets": data, "errors": errors, "stale": stale,
                        "ticket_refreshed_at": refreshed_at, "next_refresh_at": reset_at,
                        "stale_reason": stale_reason,
                        "ready": any(rows is not None for rows in data.values())}
            if self._below_floor():
                snapshot["repeated_failures"] = self._snapshot.get("repeated_failures", [])
            else:
                try:
                    snapshot["repeated_failures"] = self._read_repeated_failures()
                except (GitHubError, OSError, ValueError) as exc:
                    snapshot["repeated_failures"] = self._snapshot.get("repeated_failures", [])
                    snapshot.setdefault("dashboard_errors", []).append(
                        "repeated failure invariants: " + self._error_reason(exc))
            self.operator_items, self.human_counts = operator_items, human_counts
            self.operator_error = ask_error
            self._read_dashboard(snapshot)
            self._read_latest_runs(snapshot)
            now = time.time()
            self._refresh_waits(snapshot, now)
            self._read_open_issues(now)
            floor_context = dict(snapshot.get("floor_context") or {})
            floor_context.update({"needs_you": None if ask_error else len(self.operator_items),
                                  "needs_you_reason": ask_error,
                                  "waits_error": self.waits_error})
            snapshot["floor_context"] = floor_context
            with self._snapshot_lock:
                self._snapshot = snapshot
            self._log_requests(now)
            if snapshot["ready"] and not current["ready"]:
                LOG.info("first dashboard snapshot ready in %.1fs", time.monotonic() - started)

    def _read_latest_runs(self, snapshot: Dict[str, Any]) -> None:
        """{ticket: its newest stored run}, read once a pass here so no page
        request waits on the transcript database (#780). No configured store
        reads as empty; a failed read keeps the last pass's map and says why."""
        store = getattr(self, "transcripts", None)
        if store is None:
            try:
                store = transcripts.Store()
            except RuntimeError:
                snapshot["latest_runs"] = {}
                return
            self.transcripts = store
        try:
            snapshot["latest_runs"] = store.latest()
        # Broad on purpose: this runs on the background refresh thread, and a
        # driver error storage_errors() does not list would otherwise end the
        # refresh loop for good instead of failing one read.
        except Exception as exc:  # noqa: BLE001
            LOG.warning("transcript runs read failed: %s", exc)
            snapshot["latest_runs"] = self._snapshot.get("latest_runs", {})
            snapshot["dashboard_errors"] = list(snapshot.get("dashboard_errors", [])) + [
                "transcript runs: " + (self._error_reason(exc) or type(exc).__name__)]

    def response(self) -> Dict[str, Any]:
        with self._snapshot_lock:
            snapshot = self._snapshot
        store = getattr(self, "usage", None)
        usage_rows = [row for items in getattr(store, "providers", {}).values() for row in items] if store else []
        return {"tickets": snapshot["tickets"], "errors": snapshot["errors"],
                "repeated_failures": snapshot.get("repeated_failures", []),
                "stale": snapshot["stale"], "auth": self.auth,
                "ticket_refreshed_at": snapshot.get("ticket_refreshed_at"),
                "next_refresh_at": snapshot.get("next_refresh_at"),
                "stale_reason": snapshot.get("stale_reason", "GitHub could not refresh ticket data"),
                "improving": snapshot.get("improving", {}),
                "lanes": snapshot.get("lanes", []),
                "pull_requests": snapshot.get("pull_requests", []),
                "footer_models": snapshot.get("footer_models"),
                "footer_runs": snapshot.get("footer_runs"),
                "latest_runs": snapshot.get("latest_runs", {}),
                "state_entered": snapshot.get("state_entered", {}),
                "wait_rows": snapshot.get("wait_rows", []),
                "wait_bottleneck": snapshot.get("wait_bottleneck"),
                "floor_context": snapshot.get("floor_context", {}),
                "goal_context": snapshot.get("goal_context", {}),
                "goal_context_error": snapshot.get("goal_context_error", ""),
                "operator_items": list(self.operator_items),
                "dashboard_errors": snapshot.get("dashboard_errors", []),
                "brain_step": snapshot.get("brain_step"),
                "brain_step_error": snapshot.get("brain_step_error", "the dashboard has not read its sources yet"),
                "issue_comments": snapshot.get("issue_comments", []),
                "review_comments": snapshot.get("review_comments", []),
                "hot_spots_error": snapshot.get("hot_spots_error", "the dashboard has not read its sources yet"),
                "source_read_at": dict(snapshot.get("source_read_at") or {}),
                "sources_carried": list(snapshot.get("sources_carried") or []),
                "operator_claims": snapshot.get("operator_claims") or {},
                "usage": usage_rows}


def _by_trusted(comment: Dict[str, Any], trusted) -> bool:
    """Whether a comment counts: any author when no LOOM_BRIEF_AUTHORS is set,
    else only those logins (as the comment feed reads them)."""
    return not trusted or str((comment.get("user") or {}).get("login") or "").lower() in trusted


def journal_next(body: str) -> Optional[str]:
    """A journal's next move: its Next line, else a line saying next or will."""
    step = re.search(r"^\s*-\s*(?:Next(?: step)?|Next move)\s*:\s*(.+)$", body, re.I | re.M)
    if step:
        return step.group(1).strip()
    return next((line.strip()[2:].strip() for line in body.splitlines()[1:]
                 if line.strip().startswith("- ") and re.search(r"\b(next|will)\b", line, re.I)), None)


def _floor_context(reports, journals, asks, errors):
    latest_report = max(reports, key=lambda row: str(row.get("created_at") or ""), default=None)
    latest_journal = max(journals, key=lambda row: str(row.get("created_at") or ""), default=None)
    def sections(comment):
        return goals.sections(str(comment.get("body") or "")) if comment else {}
    report_body = str((latest_report or {}).get("body") or "")
    headline = next((line.strip() for line in report_body.splitlines()[1:] if line.strip()), None)
    journal_body = str((latest_journal or {}).get("body") or "")
    journal_step = journal_next(journal_body)
    # A journal with no Next line still leads with the tick's main action.
    journal_lead = next((line.strip()[2:].strip() for line in journal_body.splitlines()[1:]
                         if line.strip().startswith("- ")), None)
    header = next((line for line in str((latest_journal or {}).get("body") or "").splitlines()[1:2]
                   if line.startswith("Tick ")), "")
    holder_match = re.search(r"^Tick\s+[^,]+,\s*(.+)$", header)
    return {"report_headline": headline, "report_time": (latest_report or {}).get("created_at"),
            "since_report": sections(latest_report), "journal_step": journal_step,
            "journal_lead": journal_lead,
            "journal_holder": holder_match.group(1).strip() if holder_match else None,
            "journal_time": (latest_journal or {}).get("created_at"),
            "needs_you": len(asks),
            "report_reason": "no goal report found" if not reports else "",
            "journal_reason": "no goal journal found" if not journals else ""}


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
