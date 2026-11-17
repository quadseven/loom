"""assign_queued runs before the heavy reads of a pass, so a pass whose request
target the early steps spend still briefs a queued ticket onto a free lane."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import station  # noqa: E402

REPO = "example-owner/example-repo"


class LaneFeedOrder(unittest.TestCase):
    def run_pass(self, spent_by):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = station.Settings(os.path.join(tmp.name, "loom"), [REPO], [], [],
                                    disk_free_min_gb=0)
        st = station.Station(mock.Mock(cache_dir=None, target=500), settings)
        calls, spent = [], []

        def step(name):
            def run():
                calls.append(name)
                if name == spent_by:
                    spent.append(True)
            return run

        patches = [mock.patch.object(st, "_budget_exhausted", return_value=False),
                   mock.patch.object(st, "check_repo_access", return_value=True),
                   mock.patch.object(st, "_target_reached", side_effect=lambda: bool(spent)),
                   mock.patch.object(st, "budget_low", return_value=None),
                   mock.patch.object(st, "_end_pass_locally"),
                   mock.patch.object(st, "_run_steps"),
                   mock.patch.object(st, "post_metrics"),
                   mock.patch.object(st, "watch_logs"),
                   mock.patch.object(st, "report_outcomes"),
                   mock.patch.object(st, "_publish_lanes_when_old"),
                   mock.patch.object(st, "sync_labels"),
                   mock.patch.object(st, "finish_claims"),
                   mock.patch.object(st, "record_scans"),
                   mock.patch.object(st, "disk_low", return_value=False)]
        for name in ("tend_runners", "reconcile_pull_requests", "hand_back",
                     "claim_new", "assign_queued", "sweep_reviews"):
            patches.append(mock.patch.object(st, name, side_effect=step(name)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        st.run_pass()
        return calls

    def test_assign_queued_runs_before_the_heavy_reads(self):
        calls = self.run_pass(spent_by="claim_new")
        self.assertLess(calls.index("assign_queued"), calls.index("reconcile_pull_requests"))
        self.assertLess(calls.index("assign_queued"), calls.index("claim_new"))

    def test_a_pass_spent_by_the_early_reads_still_runs_assign_queued(self):
        calls = self.run_pass(spent_by="reconcile_pull_requests")
        self.assertIn("assign_queued", calls)

    def test_a_ticket_claimed_this_pass_is_assigned_in_the_same_pass(self):
        calls = self.run_pass(spent_by="never")
        self.assertEqual([c for c in calls if c == "assign_queued"], ["assign_queued"] * 2)
        self.assertGreater(len(calls) - calls[::-1].index("assign_queued") - 1,
                           calls.index("claim_new"))

    def test_the_review_sweep_runs_before_the_heavy_reads(self):
        # claim_new spent the pass's request target pass after pass on
        # 2026-11-16, so the step loop, with the sweep in it, never ran and
        # no open pull request got a review.
        calls = self.run_pass(spent_by="claim_new")
        self.assertLess(calls.index("sweep_reviews"), calls.index("reconcile_pull_requests"))
        self.assertLess(calls.index("sweep_reviews"), calls.index("claim_new"))
        self.assertEqual(calls.count("sweep_reviews"), 1)

    def test_the_review_sweep_is_not_also_a_loop_step(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = station.Settings(os.path.join(tmp.name, "loom"), [REPO], [], [],
                                    disk_free_min_gb=0)
        st = station.Station(mock.Mock(cache_dir=None, target=500), settings)
        seen = []
        with mock.patch.object(st, "_paused_steps", side_effect=lambda steps: seen.extend(
                s.__name__ for s in steps) or set()), \
                mock.patch.object(st, "_deferred", return_value=True):
            st._run_steps()
        self.assertTrue(seen)
        self.assertNotIn("sweep_reviews", seen)


if __name__ == "__main__":
    unittest.main()
