"""Tests for PassView: single per-pass view of GitHub state owning reads,
deadlines, and request budget (loom#2458)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from typing import Any, Dict, List, Tuple

from bus import issue_reads
from bus import pass_view
from bus import station
from bus.github import GitHub, GitHubError, Response
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
NOW = 1700000000.0


class DummyGitHub:
    def __init__(self, requests: int = 0, not_modified: int = 0) -> None:
        self.requests = requests
        self.not_modified = not_modified
        self.kinds: Dict[str, int] = {}
        self.carried_over: Dict[str, int] = {}
        self.rate: Dict[str, Tuple[int, int, int]] = {}
        self.limited_until: Any = None
        self.auth = "token"
        self.api = "https://api.github.com"
        self.issue_writes: List[Tuple[str, int]] = []
        self.comment_edits = False
        self.target: Any = None

    def rest(self, method: str, path: str, body: Any = None,
             etag: Any = None, ok: Any = ()) -> Response:
        return Response(200, {"path": path})

    def paginate(self, path: str) -> List[Any]:
        return []

    def prune_cache(self, keep: int, now: float) -> None:
        pass


class PassViewConstructionTest(unittest.TestCase):
    def test_pass_view_wraps_client_or_returns_pass_view(self) -> None:
        gh = DummyGitHub()
        view = pass_view.pass_view(gh, now=NOW)
        self.assertIsInstance(view, pass_view.PassView)
        self.assertIs(pass_view.pass_view(view), view)

    def test_pass_view_memory_factory_seeds_issues_and_lists(self) -> None:
        issue_data = {"number": 1, "title": "Test Issue", "updated_at": "2026-11-09T00:00:00Z"}
        key = "%s#%d" % (REPO, 1)
        view = pass_view.PassView.memory(
            issues={key: issue_data},
            lists={"/repos/%s/issues?labels=loom:briefed" % REPO: [issue_data]},
            rates={"core": (4000, 5000, int(NOW + 3600))},
            now=NOW,
        )
        fetched = view.issue(REPO, 1)
        self.assertEqual(fetched["title"], "Test Issue")
        self.assertEqual(fetched["repository_url"], "https://api.github.com/repos/%s" % REPO)
        paginated = view.paginate("/repos/%s/issues?labels=loom:briefed" % REPO)
        self.assertEqual(len(paginated), 1)
        self.assertEqual(paginated[0]["number"], 1)
        self.assertEqual(view.rates()["core"], (4000, 5000, int(NOW + 3600)))


class PassViewReadsCachingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_single_issue_read_cached_in_pass(self) -> None:
        call_count = [0]

        def transport(method: str, url: str, headers: Any, body: Any) -> Tuple[int, Dict[str, str], bytes]:
            call_count[0] += 1
            data = {"number": 1, "title": "Issue 1", "updated_at": "2026-11-09T00:00:00Z"}
            return 200, {"ETag": '"etag-1"'}, json.dumps(data).encode("utf-8")

        gh = GitHub("fake-token", transport=transport)
        view = pass_view.PassView(gh, state_dir=self.state_dir, now=NOW)
        res1 = view.issue(REPO, 1)
        res2 = view.issue(REPO, 1)
        self.assertEqual(res1["number"], 1)
        self.assertEqual(res2["number"], 1)
        self.assertEqual(call_count[0], 1)

    def test_listing_populates_issue_stamps_and_single_issue_reads(self) -> None:
        issue1 = {"number": 1, "updated_at": "2026-11-09T01:00:00Z"}
        issue2 = {"number": 2, "updated_at": "2026-11-09T02:00:00Z"}
        view = pass_view.PassView.memory(
            lists={"/repos/%s/issues?state=open" % REPO: [issue1, issue2]},
            now=NOW,
        )
        items = view.paginate("/repos/%s/issues?state=open" % REPO)
        self.assertEqual(len(items), 2)
        self.assertEqual(view._issue_stamps.get((REPO, 1)), "2026-11-09T01:00:00Z")
        self.assertEqual(view._issue_stamps.get((REPO, 2)), "2026-11-09T02:00:00Z")

    def test_comments_and_events_caching_across_passes(self) -> None:
        stamp = "2026-11-09T01:00:00Z"
        ticket_key = "%s#%d/comments" % (REPO, 1)
        ticket_reads = {
            ticket_key: {
                "stamp": stamp,
                "data": [{"id": 101, "body": "hello"}],
                "seen": NOW - 100,
            }
        }
        with open(os.path.join(self.state_dir, "ticket_reads.json"), "w", encoding="utf-8") as fh:
            json.dump(ticket_reads, fh)

        gh = DummyGitHub()
        view = pass_view.PassView(gh, state_dir=self.state_dir, now=NOW)
        view._issue_stamps[(REPO, 1)] = stamp
        comments = view.paginate("/repos/%s/issues/1/comments" % REPO)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["body"], "hello")

    def test_comments_cache_invalidated_by_issue_writes_or_edits(self) -> None:
        stamp = "2026-11-09T01:00:00Z"
        ticket_key = "%s#%d/comments" % (REPO, 1)
        ticket_reads = {
            ticket_key: {
                "stamp": stamp,
                "data": [{"id": 101, "body": "old body"}],
                "seen": NOW - 100,
            }
        }
        with open(os.path.join(self.state_dir, "ticket_reads.json"), "w", encoding="utf-8") as fh:
            json.dump(ticket_reads, fh)

        gh = DummyGitHub()
        gh.issue_writes = [(REPO, 1)]
        called = [False]

        def fake_paginate(path: str) -> List[Any]:
            called[0] = True
            return [{"id": 102, "body": "new body"}]

        gh.paginate = fake_paginate  # type: ignore[assignment]
        view = pass_view.PassView(gh, state_dir=self.state_dir, now=NOW)
        view._issue_stamps[(REPO, 1)] = stamp
        comments = view.paginate("/repos/%s/issues/1/comments" % REPO)
        self.assertTrue(called[0])
        self.assertEqual(comments[0]["body"], "new body")


class PassViewDeadlineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = tempfile.mkdtemp()
        self.station_dir = os.path.join(self.state_dir, "station")
        os.makedirs(self.station_dir, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_pass_deadline_env_parsing(self) -> None:
        self.assertEqual(pass_view.pass_deadline("0"), 0)
        self.assertEqual(pass_view.pass_deadline("120"), 120)
        self.assertEqual(pass_view.pass_deadline(""), pass_view.PASS_DEADLINE)
        self.assertEqual(pass_view.pass_deadline("30"), pass_view.PASS_DEADLINE)
        self.assertEqual(pass_view.pass_deadline("not-a-number"), pass_view.PASS_DEADLINE)

    def test_arm_and_disarm_pass_deadline(self) -> None:
        logs: List[str] = []
        view = pass_view.PassView(
            DummyGitHub(),
            state_dir=self.state_dir,
            now=NOW,
            deadline=120,
            log=logs.append,
        )
        marker = view.arm_deadline()
        self.assertIsNotNone(marker)
        self.assertTrue(os.path.isfile(marker or ""))
        with open(marker or "", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data.get("deadline"), 120)

        view.disarm_deadline()
        self.assertFalse(os.path.exists(marker or ""))

    def test_overrun_detection_from_previous_marker(self) -> None:
        logs: List[str] = []
        marker_path = os.path.join(self.station_dir, pass_view.DEADLINE_MARKER_FILE)
        with open(marker_path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 99999, "started": NOW - 500, "deadline": 300}, fh)

        view = pass_view.PassView(
            DummyGitHub(),
            state_dir=self.state_dir,
            now=NOW,
            deadline=300,
            log=logs.append,
        )
        view.arm_deadline()
        view.disarm_deadline()
        self.assertTrue(any("overran its 300s deadline" in line for line in logs))


class PassViewBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_counted_requests_excludes_304s(self) -> None:
        gh = DummyGitHub(requests=10, not_modified=3)
        view = pass_view.PassView(gh, state_dir=self.state_dir, now=NOW)
        self.assertEqual(view.counted, 7)

    def test_rates_merges_saved_state_and_live_rates(self) -> None:
        saved = {
            "rates": {
                "core": [4500, 5000, int(NOW + 1000)],
                "search": [25, 30, int(NOW + 500)],
                "expired": [10, 10, int(NOW - 100)],
            }
        }
        with open(os.path.join(self.state_dir, "budget.json"), "w", encoding="utf-8") as fh:
            json.dump(saved, fh)

        gh = DummyGitHub()
        gh.rate = {"search": (20, 30, int(NOW + 600))}
        view = pass_view.PassView(gh, state_dir=self.state_dir, now=NOW)
        rates = view.rates()
        self.assertEqual(rates["core"], (4500, 5000, int(NOW + 1000)))
        self.assertEqual(rates["search"], (20, 30, int(NOW + 600)))
        self.assertNotIn("expired", rates)

    def test_budget_low_detects_floor(self) -> None:
        gh = DummyGitHub()
        gh.rate = {
            "core": (500, 5000, int(NOW + 1000)),
            "search": (25, 30, int(NOW + 1000)),
        }
        view = pass_view.PassView(gh, state_dir=self.state_dir, now=NOW)
        low = view.budget_low()
        self.assertIsNotNone(low)
        if low:
            res, rem, lim, rst = low
            self.assertEqual(res, "core")
            self.assertEqual(rem, 500)

    def test_target_reached_and_deferred(self) -> None:
        gh = DummyGitHub(requests=550, not_modified=50)
        view = pass_view.PassView(gh, state_dir=self.state_dir, now=NOW, target=500)
        self.assertTrue(view.target_reached())
        self.assertTrue(view.deferred())

    def test_end_pass_saves_budget_and_ticket_reads(self) -> None:
        logs: List[str] = []
        gh = DummyGitHub(requests=12, not_modified=2)
        gh.kinds = {"issues": 10}
        gh.rate = {"core": (4900, 5000, int(NOW + 3600))}
        view = pass_view.PassView(
            gh,
            state_dir=self.state_dir,
            now=NOW,
            target=500,
            log=logs.append,
        )
        view._ticket_reads["%s#%d/comments" % (REPO, 1)] = {
            "stamp": "stamp-1",
            "data": [],
            "seen": NOW,
        }
        view.end_pass()

        budget_path = os.path.join(self.state_dir, "budget.json")
        reads_path = os.path.join(self.state_dir, "ticket_reads.json")
        self.assertTrue(os.path.exists(budget_path))
        self.assertTrue(os.path.exists(reads_path))

        with open(budget_path, encoding="utf-8") as fh:
            b_data = json.load(fh)
        self.assertIn("core", b_data.get("rates", {}))

        with open(reads_path, encoding="utf-8") as fh:
            r_data = json.load(fh)
        self.assertIn("%s#%d/comments" % (REPO, 1), r_data)
        self.assertTrue(any("requests: 12, 10 counted" in log for log in logs))


class StationPassViewIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, "state")
        os.makedirs(os.path.join(self.state, "station"), exist_ok=True)
        self.settings = station.Settings(
            home=self.home,
            repos=[REPO],
            authors=["operator"],
            owners=["owner"],
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def test_station_delegates_reads_deadlines_and_budget_to_pass_view(self) -> None:
        gh = DummyGitHub(requests=50, not_modified=10)
        gh.rate = {"core": (4000, 5000, int(NOW + 3600))}
        st = station.Station(gh, self.settings, now=NOW)

        self.assertIsInstance(st.view, pass_view.PassView)
        self.assertIs(st.gh, st.view)
        self.assertEqual(st._pass_counted(), 40)
        self.assertEqual(st._rates()["core"], (4000, 5000, int(NOW + 3600)))
        self.assertIsNone(st.budget_low())


if __name__ == "__main__":
    unittest.main()
