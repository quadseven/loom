"""Snapshot behavior with a GitHub boundary that controls request latency."""
import os
import threading
import time
import unittest
from unittest.mock import patch

from bus.github import GitHubError
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


class SnapshotTest(unittest.TestCase):
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
