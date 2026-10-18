"""Snapshot behavior with a GitHub boundary that controls request latency."""
import os
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bus.github import GitHub, GitHubError
from dashboard.github import TicketStore


REPOS = ["example-owner/one", "example-owner/two", "example-owner/three"]


class SlowGitHub:
    def __init__(self, failing=None, delay=0.04):
        self.rate = {}
        self.failing = failing
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.request_time = 0.0
        self.lock = threading.Lock()

    def paginate(self, path):
        started = time.monotonic()
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            repo = next(repo for repo in REPOS if "/" + repo + "/" in path)
            if self.failing and repo in self.failing and "labels=loom%3Aticket" in path:
                raise GitHubError("ticket read failed")
            if "labels=loom%3Aticket&state=all" in path:
                return [{"number": REPOS.index(repo) + 1, "title": repo,
                         "labels": [{"name": "loom:ticket"}]}]
            if "/pulls?" in path:
                return [{"number": REPOS.index(repo) + 11}]
            if "/issues/comments?" in path:
                return [{"id": REPOS.index(repo) + 21, "user": {"login": "bot"}}]
            if "/pulls/comments?" in path:
                return [{"id": REPOS.index(repo) + 31}]
            return []
        finally:
            with self.lock:
                self.active -= 1
                self.request_time += time.monotonic() - started

    def paginate_parallel(self, path, workers=8):
        return self.paginate(path)


class HomeGitHub(SlowGitHub):
    """SlowGitHub plus a status home with a goal journal and a lanes comment,
    and a lock branch with no tick record, counting every request."""

    def __init__(self):
        super().__init__(delay=0)
        self.requests = 0

    def paginate(self, path):
        self.requests += 1
        if "labels=loom%3Agoal" in path:
            return [{"number": 5, "title": "Goal"}]
        if path.endswith("/issues/5/comments"):
            return [{"id": 41, "body": "<!-- loom:journal v1 -->\n- Friction: slow reads",
                     "user": {"login": "bot"}, "created_at": "2030-01-01T00:00:00Z"},
                    {"id": 42, "body": "<!-- loom:lanes v1 -->\n| codex | idle |",
                     "user": {"login": "bot"}, "created_at": "2030-01-01T00:00:00Z"}]
        return super().paginate(path)

    def rest(self, method, path, **kwargs):
        self.requests += 1

        class Missing:
            status, data, headers = 404, {}, {}
        return Missing()


class SnapshotTest(unittest.TestCase):
    def test_a_pass_below_the_floor_keeps_every_view_source_of_the_last_read(self):
        # Emptying them showed false zeros while the budget stayed low:
        # Improving 0/0/0/0, Lanes "no loom:lanes comment", Brain "no goal
        # journal found", Results "Lane pull requests merged 0" (loom#779).
        gh = HomeGitHub()
        with patch.dict(os.environ, {"LOOM_STATUS_REPO": REPOS[0]}, clear=True):
            store = TicketStore(REPOS, gh=gh, budget_floor=500)
            store._waits_refreshed_at = float("inf")
            store.refresh()
            first = store.response()
            store._last_remaining, store._reset_at = 10, int(time.time()) + 3600
            before = gh.requests
            store.refresh()
            second = store.response()
        self.assertEqual(gh.requests, before)
        for key in ("improving", "lanes", "pull_requests", "issue_comments", "review_comments",
                    "brain_step"):
            self.assertTrue(first[key], key)
            self.assertEqual(second[key], first[key], key)
        self.assertTrue(first["improving"]["journals"])
        self.assertEqual(second["footer_runs"], first["footer_runs"])
        self.assertEqual(second["brain_step_error"], "")
        self.assertEqual(second["hot_spots_error"], "")
        self.assertEqual(set(first["source_read_at"]),
                         {"improving", "lanes", "pull_requests", "brain_step", "comments"})
        self.assertEqual(second["source_read_at"], first["source_read_at"])
        self.assertEqual(first["sources_carried"], [])
        self.assertEqual(set(second["sources_carried"]), set(first["source_read_at"]))
        self.assertIn("request budget ran low", second["dashboard_errors"][0])

    def test_a_first_pass_below_the_floor_records_no_read_time(self):
        gh = HomeGitHub()
        with patch.dict(os.environ, {"LOOM_STATUS_REPO": REPOS[0]}, clear=True):
            store = TicketStore(REPOS, gh=gh, budget_floor=500)
            store._waits_refreshed_at = float("inf")
            store._last_remaining, store._reset_at = 10, int(time.time()) + 3600
            store.refresh()
            payload = store.response()
        self.assertEqual(payload["source_read_at"], {})
        self.assertEqual(payload["sources_carried"], [])
        self.assertEqual(payload["pull_requests"], [])
        self.assertIn("request budget ran low", payload["hot_spots_error"])

    def test_pull_request_pages_overlap_within_a_repo_and_keep_page_order(self):
        repo = REPOS[0]
        active = 0
        max_active = 0
        first_started = None
        last_finished = None
        request_time = 0.0
        lock = threading.Lock()

        def transport(method, url, headers, body):
            nonlocal active, max_active, first_started, last_finished, request_time
            path = urlsplit(url).path
            if path != "/repos/%s/pulls" % repo:
                return 200, {}, b"[]"
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            started = time.monotonic()
            with lock:
                first_started = first_started or started
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.08)
                links = "" if page == 8 else (
                    '<https://api.github.com/repos/%s/pulls?state=all&per_page=100&page=%d>; rel="next", '
                    '<https://api.github.com/repos/%s/pulls?state=all&per_page=100&page=8>; rel="last"'
                    % (repo, page + 1, repo))
                return 200, {"Link": links}, json.dumps([{"number": page}]).encode()
            finally:
                with lock:
                    active -= 1
                    last_finished = time.monotonic()
                    request_time += last_finished - started

        gh = GitHub("", transport=transport)
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore([repo], gh=gh)
            store.refresh()
        self.assertGreater(max_active, 1)
        self.assertLess(last_finished - first_started, request_time * 0.7)
        self.assertEqual([row["number"] for row in store.response()["pull_requests"]],
                         [1, 2, 3, 4, 5, 6, 7, 8])

    def test_repo_reads_overlap_and_keep_input_order(self):
        gh = SlowGitHub()
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=gh)
            started = time.monotonic()
            store.refresh()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, gh.request_time * 0.7)
        self.assertGreater(gh.max_active, 1)
        self.assertEqual(list(store.data), REPOS)
        self.assertEqual([row["repo"] for row in store.response()["issue_comments"]], REPOS)

    def test_one_repo_error_keeps_other_repo_data(self):
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=SlowGitHub(failing=REPOS[1]))
            store.refresh()
        self.assertEqual(store.errors, {REPOS[1]: "ticket read failed"})
        self.assertEqual([store.data[repo][0]["number"] for repo in (REPOS[0], REPOS[2])], [1, 3])
        self.assertEqual([row["number"] for row in store.response()["pull_requests"]], [11, 12, 13])

    def test_pull_request_page_error_stays_with_its_repo(self):
        repos = REPOS[:2]

        def transport(method, url, headers, body):
            path = urlsplit(url).path
            if "/pulls" not in path or "/pulls/comments" in path:
                return 200, {}, b"[]"
            repo = next(repo for repo in repos if "/repos/" + repo + "/pulls" == path)
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            if repo == repos[1] and page == 2:
                raise GitHubError("pull page failed", status=403)
            links = ("<https://api.github.com/repos/%s/pulls?state=all&per_page=100&page=2>; rel=\"last\""
                     % repo) if page == 1 else ""
            return 200, {"Link": links}, json.dumps([{"repo": repo, "number": page}]).encode()

        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(repos, gh=GitHub("", transport=transport))
            store.refresh()
        self.assertEqual(store.response()["pull_requests"],
                         [{"repo": repos[0], "number": 1}, {"repo": repos[0], "number": 2}])
        self.assertIn("%s pull requests: pull page failed" % repos[1],
                      store.response()["dashboard_errors"])

    def test_empty_snapshot_is_not_ready(self):
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore([], gh=SlowGitHub())
            self.assertFalse(store.ready)
            store.refresh()
        self.assertFalse(store.ready)

    def test_unreadable_snapshot_is_not_ready(self):
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=SlowGitHub(failing=REPOS))
            store.refresh()
        self.assertFalse(store.ready)
        self.assertEqual(list(store.errors), REPOS)

    def test_first_ready_snapshot_logs_its_wall_time_once(self):
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=SlowGitHub(delay=0.001))
            with self.assertLogs("dashboard.github", level="INFO") as logs:
                store.refresh()
                store.refresh()
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(logs.output[0], r"first dashboard snapshot ready in [0-9.]+s")
