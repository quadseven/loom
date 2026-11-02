"""Tests for brain status and hourly auto-merge cap refusals (loom#1765)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timezone
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
NOW = datetime(2026, 10, 25, 14, 0, tzinfo=timezone.utc)


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


def ready_node(checks="SUCCESS", reviews=(), threads=()):
    return {"commits": {"nodes": [{"commit": {"committedDate": "2026-10-25T13:45:00Z",
                                              "statusCheckRollup": {"state": checks}}}]},
            "reviews": {"nodes": list(reviews)}, "reviewThreads": {"nodes": list(threads)}}


class StubTransport:
    def __init__(self, routes=None, search_counts=None, search_error=False):
        self.routes = dict(routes or {})
        self.search_counts = search_counts or {}
        self.search_error = search_error
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[1]
        self.calls.append((method, path))
        if path.startswith("/search/issues"):
            if self.search_error:
                return 500, {}, b'{"message": "Server Error"}'
            query = unquote(path.split("q=")[1].split("&")[0])
            count = self.search_counts.get("default", 0)
            for key, val in self.search_counts.items():
                if key != "default" and key in query:
                    count = val
                    break
            return 200, {}, json.dumps({"total_count": count}).encode()
        for (m, suffix), answer in self.routes.items():
            if m == method and path.split("?")[0].endswith(suffix):
                return answer
        if method == "POST" and "/graphql" in path:
            body_str = body.decode() if isinstance(body, bytes) else (body or "")
            import re
            repo_data = {}
            for match in re.finditer(r"(p\d+):\s*pullRequest", body_str):
                repo_data[match.group(1)] = ready_node()
            return 200, {}, json.dumps({"data": {"repository": repo_data}}).encode()
        return 200, {}, json.dumps([] if method == "GET" else {}).encode()


class HourlyMergeCapStatusTests(unittest.TestCase):
    PR = {"number": 9, "title": "Work", "head": {"ref": "loom/7-work", "sha": "h"},
          "labels": [{"name": "class:logic"}]}

    def routes(self):
        issue = {"number": 7, "state": "open", "body": "",
                 "labels": [{"name": "loom:ticket"}, {"name": "loom:" + "pr"}]}
        return {
            ("GET", "/issues/7"): reply(200, issue),
            ("GET", "/pulls/9"): reply(200, {"number": 9, "mergeable": True,
                                             "mergeable_state": "clean"}),
            ("GET", "/pulls/9/files"): reply(200, [{"filename": "bus/a.py", "patch": "+x"}]),
        }

    def test_overall_cap_blocks_ready_pull_request(self):
        # Stub GitHub whose search count is at the overall cap (4).
        # Per-repo count is 0 (< 2), overall count is 4 (>= 4).
        transport = StubTransport(self.routes(), search_counts={"repo:": 0, "user:": 4})
        gh = GitHub("t", transport=transport)
        with unittest.mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"}):
            lines = brain._pull_request_lines(gh, REPO, [self.PR], {"prs": {9: ready_node()}},
                                              "grug-tribe", now=NOW)
        expected_suffix = ("blocked: held by the hourly merge cap: 4 loom/ pull requests merged in "
                           "the last hour, the cap is 4 (LOOM_AUTO_MERGE_PER_HOUR)")
        self.assertTrue(lines[1].endswith(expected_suffix), lines[1])
        self.assertIn("blocked: held by the hourly merge cap", lines[1])

    def test_per_repo_cap_blocks_ready_pull_request(self):
        # Per-repo search count is at the per-repo cap (2).
        transport = StubTransport(self.routes(), search_counts={"repo:": 2, "user:": 2})
        gh = GitHub("t", transport=transport)
        with unittest.mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"}):
            lines = brain._pull_request_lines(gh, REPO, [self.PR], {"prs": {9: ready_node()}},
                                              "grug-tribe", now=NOW)
        expected = ("- #9 Work - blocked: held by the hourly merge cap: 2 loom/ pull requests "
                    "merged in %s in the last hour, the cap is 2 (LOOM_AUTO_MERGE_PER_REPO_HOUR)" % REPO)
        self.assertEqual(lines[1], expected)

    def test_counts_under_both_caps_line_still_ends_in_ready(self):
        # Counts under both caps: the line still ends in READY.
        transport = StubTransport(self.routes(), search_counts={"default": 0})
        gh = GitHub("t", transport=transport)
        with unittest.mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"}):
            lines = brain._pull_request_lines(gh, REPO, [self.PR], {"prs": {9: ready_node()}},
                                              "grug-tribe", now=NOW)
        self.assertEqual(lines[1], "- #9 Work - READY")

    def test_status_asks_github_for_counts_at_most_once_per_repo(self):
        # Multiple READY pull requests in the same repo query GitHub at most once per repo.
        pr8 = {"number": 8, "title": "Work 8", "head": {"ref": "loom/6-work", "sha": "h8"},
               "labels": [{"name": "class:logic"}]}
        pr9 = self.PR
        routes = self.routes()
        routes[("GET", "/issues/6")] = reply(200, {"number": 6, "state": "open",
                                                   "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]})
        routes[("GET", "/pulls/8")] = reply(200, {"number": 8, "mergeable": True,
                                                  "mergeable_state": "clean"})
        routes[("GET", "/pulls/8/files")] = reply(200, [{"filename": "bus/b.py", "patch": "+x"}])

        transport = StubTransport(routes, search_counts={"repo:": 0, "user:": 4})
        gh = GitHub("t", transport=transport)
        with unittest.mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"}):
            lines = brain._pull_request_lines(gh, REPO, [pr8, pr9],
                                              {"prs": {8: ready_node(), 9: ready_node()}},
                                              "grug-tribe", now=NOW)
        self.assertIn("blocked: held by the hourly merge cap", lines[1])
        self.assertIn("blocked: held by the hourly merge cap", lines[2])
        search_calls = [c for c in transport.calls if c[1].startswith("/search/issues")]
        # Exactly one in-repo search and one overall search for the whole repo across both PRs.
        self.assertEqual(len(search_calls), 2)

    def test_count_github_will_not_give_shows_as_hourly_merge_cap_blocker(self):
        # When GitHub will not give the count, the cap fails closed and status names it.
        transport = StubTransport(self.routes(), search_error=True)
        gh = GitHub("t", transport=transport)
        with unittest.mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"}):
            lines = brain._pull_request_lines(gh, REPO, [self.PR], {"prs": {9: ready_node()}},
                                              "grug-tribe", now=NOW)
        self.assertIn("blocked: held by the hourly merge cap: could not count the last hour's merges",
                      lines[1])

    def test_status_report_asks_counts_at_most_once_per_repo(self):
        pr8 = {"number": 8, "title": "Work 8", "head": {"ref": "loom/6-work", "sha": "h8"},
               "labels": [{"name": "class:logic"}]}
        pr9 = self.PR
        routes = self.routes()
        routes[("GET", "/issues/6")] = reply(200, {"number": 6, "state": "open",
                                                   "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]})
        routes[("GET", "/pulls/8")] = reply(200, {"number": 8, "mergeable": True,
                                                  "mergeable_state": "clean"})
        routes[("GET", "/pulls/8/files")] = reply(200, [{"filename": "bus/b.py", "patch": "+x"}])
        routes[("GET", "/issues")] = reply(200, [])
        routes[("GET", "/pulls")] = reply(200, [pr8, pr9])

        transport = StubTransport(routes, search_counts={"repo:": 0, "user:": 4})
        gh = GitHub("t", transport=transport)
        env = {"LOOM_BRIEF_AUTHORS": "example-brain", "LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"}
        report = brain.status(gh, [REPO], REPO, env=env, now=NOW)
        search_calls = [c for c in transport.calls if c[1].startswith("/search/issues")]
        self.assertEqual(len(search_calls), 2)
        self.assertIn("- #8 Work 8 - blocked: held by the hourly merge cap", report)
        self.assertIn("- #9 Work - blocked: held by the hourly merge cap", report)


class RepoCapRefusalTests(unittest.TestCase):
    def test_threads_reading_one_repo_make_one_count(self):
        reads = []

        def count(gh, repo, now=None):
            reads.append(repo)
            time.sleep(0.05)
            return "held"

        caps = {}
        answers = []
        with unittest.mock.patch.object(brain.merge_tier, "cap_refusal", count):
            threads = [threading.Thread(target=lambda: answers.append(
                brain._repo_cap_refusal(None, REPO, caps, None))) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(reads, [REPO])
        self.assertEqual(answers, ["held"] * 6)

