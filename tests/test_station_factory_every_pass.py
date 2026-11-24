"""The factory (merge ready lane pull requests, bring the rest up to date,
send conflicts and change requests back as fix rounds) runs on every pass it
is due, however much of the request target the steps before it spent.

On 2026-11-24 assign_queued, reconcile_pull_requests and claim_new spent the
500-request target pass after pass, so the step loop broke at its first step
and run_factory, one of its steps, ran a few times a day. Thirteen approved,
green lane pull requests waited up to six days and filled their repos' open
pull request caps (loom#3534)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import station  # noqa: E402
from bus.github import TargetReached  # noqa: E402

REPO = "example-owner/example-repo"


class Counter:
    """A client whose counted requests the test sets."""

    def __init__(self, counted=0, target=500):
        self.cache_dir = None
        self.counted = counted
        self.requests = counted
        self.target = target


class FactoryEveryPass(unittest.TestCase):
    def station(self, gh=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = station.Settings(os.path.join(tmp.name, "loom"), [REPO], [], [],
                                    disk_free_min_gb=0)
        return station.Station(gh or Counter(), settings)

    def run_pass(self, spent_by, low=None):
        st = self.station(mock.Mock(cache_dir=None, target=500, counted=0, requests=0))
        calls, spent = [], []

        def step(name):
            def run():
                calls.append(name)
                if name == spent_by:
                    spent.append(True)
            run.__name__ = name
            return run

        patches = [mock.patch.object(st, "_budget_exhausted", return_value=False),
                   mock.patch.object(st, "check_repo_access", return_value=True),
                   mock.patch.object(st, "_target_reached", side_effect=lambda: bool(spent)),
                   mock.patch.object(st, "budget_low", return_value=low),
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
        for name in ("tend_runners", "tend_remote_checks", "reconcile_pull_requests",
                     "hand_back", "claim_new", "assign_queued", "sweep_reviews",
                     "settle_ended_runs", "run_factory"):
            patches.append(mock.patch.object(st, name, side_effect=step(name)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        st.run_pass()
        return calls

    def test_a_pass_whose_target_the_claim_pass_spent_still_runs_the_factory(self):
        calls = self.run_pass(spent_by="claim_new")
        self.assertEqual(calls.count("run_factory"), 1)

    def test_a_pass_whose_target_the_early_reads_spent_still_runs_the_factory(self):
        calls = self.run_pass(spent_by="assign_queued")
        self.assertEqual(calls.count("run_factory"), 1)

    def test_the_factory_runs_after_the_claim_pass_so_claims_keep_their_share(self):
        calls = self.run_pass(spent_by="never")
        self.assertLess(calls.index("claim_new"), calls.index("run_factory"))

    def test_a_low_budget_skips_the_factory(self):
        calls = self.run_pass(spent_by="never", low=("core", 100, 5000, 0))
        self.assertNotIn("run_factory", calls)

    def test_the_factory_is_not_also_a_loop_step(self):
        st = self.station()
        seen = []
        with mock.patch.object(st, "_paused_steps", side_effect=lambda steps: seen.extend(
                s.__name__ for s in steps) or set()), \
                mock.patch.object(st, "_deferred", return_value=True):
            st._run_steps()
        self.assertTrue(seen)
        self.assertNotIn("run_factory", seen)

    def test_a_paused_factory_does_not_run(self):
        st = self.station()
        ran = []
        with mock.patch.dict(os.environ, {"LOOM_PAUSED_STEPS": "run_factory"}), \
                mock.patch.object(st, "budget_low", return_value=None), \
                mock.patch.object(st, "run_factory", side_effect=lambda: ran.append(1)):
            st._run_factory_every_pass()
        self.assertEqual(ran, [])

    def test_the_factory_spends_past_the_target_by_its_reserve_at_most(self):
        gh = Counter(counted=640, target=500)
        st = self.station(gh)
        seen = []
        with mock.patch.object(st, "budget_low", return_value=None), \
                mock.patch.object(st, "run_factory",
                                  side_effect=lambda: seen.append(gh.target)):
            st._run_factory_every_pass()
        self.assertEqual(seen, [640 + station.FACTORY_REQUEST_RESERVE])
        self.assertEqual(gh.target, 500)

    def test_a_factory_carried_over_at_its_reserve_restores_the_target(self):
        gh = Counter(counted=100, target=500)
        st = self.station(gh)

        def carry():
            raise TargetReached("comments read carried over at request target")
        carry.__name__ = "run_factory"
        with mock.patch.object(st, "budget_low", return_value=None), \
                mock.patch.object(st, "run_factory", new=carry):
            st._run_factory_every_pass()
        self.assertEqual(gh.target, 500)


if __name__ == "__main__":
    unittest.main()
