"""The step loop keeps at least half the pass request target, however much
the steps before it spent, and the log says how long a full turn of the
rotation took (loom#3119).

On 2026-11-26 the claim pass kept to its share (41 to 250 of 500), but
reconcile_pull_requests, assign_queued and run_factory spent the rest of the
target before the step loop began, so the loop broke at its first step and
check_invariants ran only when it was four hours overdue."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import station  # noqa: E402

REPO = "example-owner/example-repo"


class Counter:
    """A client whose counted requests the steps set."""

    def __init__(self, counted=0, target=500):
        self.cache_dir = None
        self.counted = counted
        self.requests = counted
        self.target = target


class StepLoopReserve(unittest.TestCase):
    def station(self, gh):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = station.Settings(os.path.join(tmp.name, "loom"), [REPO], [], [],
                                    disk_free_min_gb=0)
        return station.Station(gh, settings)

    def steps(self, gh, spends, seen):
        """Fake loop steps: each records the target it ran under and spends
        its share of counted requests."""
        made = []
        for i, cost in enumerate(spends):
            def run(cost=cost):
                seen.append(gh.target)
                gh.counted += cost
                gh.requests += cost
            run.__name__ = "step_%d" % i
            made.append(run)
        return tuple(made)

    def run_loop(self, st, steps):
        err = io.StringIO()
        with mock.patch.object(st, "_loop_steps", return_value=steps), \
                mock.patch.object(st, "_note_every_pass"), \
                mock.patch.object(st, "_run_due_steps"), \
                mock.patch.object(st, "_paused_steps", return_value=set()), \
                mock.patch.object(st, "budget_low", return_value=None), \
                contextlib.redirect_stderr(err):
            st._run_steps()
        return err.getvalue()

    def rotation(self, st):
        return station._read_json(os.path.join(st.state, station.STEP_ROTATION_FILE), {})

    def test_a_loop_entered_past_the_target_still_runs_with_half_the_target(self):
        gh = Counter(counted=900, target=500)
        st = self.station(gh)
        seen = []
        self.run_loop(st, self.steps(gh, [100, 100, 100, 100], seen))
        reserve = int(500 * station.STEP_LOOP_SHARE)
        self.assertEqual(seen, [900 + reserve] * 3,
                         "the loop runs until its reserve is spent, then stops")
        self.assertEqual(gh.target, 500, "the pass target is restored")
        self.assertEqual(self.rotation(st)["next"], 3)

    def test_a_loop_entered_under_the_target_keeps_the_rest_of_it(self):
        gh = Counter(counted=100, target=500)
        st = self.station(gh)
        seen = []
        self.run_loop(st, self.steps(gh, [50, 50], seen))
        self.assertEqual(seen, [500, 500], "no extra spend when the target leaves more")
        self.assertEqual(gh.target, 500)

    def test_the_loop_reserve_is_at_least_half_the_target(self):
        self.assertGreaterEqual(station.STEP_LOOP_SHARE, 0.5)

    def test_a_low_budget_still_stops_the_loop(self):
        gh = Counter(counted=900, target=500)
        st = self.station(gh)
        seen = []
        steps = self.steps(gh, [10, 10], seen)
        with mock.patch.object(st, "_loop_steps", return_value=steps), \
                mock.patch.object(st, "_note_every_pass"), \
                mock.patch.object(st, "_run_due_steps"), \
                mock.patch.object(st, "_paused_steps", return_value=set()), \
                mock.patch.object(st, "budget_low", return_value=("core", 100, 5000, 0)), \
                mock.patch.object(st, "_budget_state", return_value={}), \
                contextlib.redirect_stderr(io.StringIO()):
            st._run_steps()
        self.assertEqual(seen, [])
        self.assertEqual(gh.target, 500)

    def test_a_full_turn_logs_how_long_it_took(self):
        gh = Counter(counted=0, target=500)
        st = self.station(gh)
        steps = self.steps(gh, [1, 1, 1], [])
        log = self.run_loop(st, steps)
        self.assertNotIn("full turn", log, "the first turn has no recorded start")
        started = self.rotation(st)["turn_started"]
        self.assertEqual(started, st.now)
        st.now += 5400
        log = self.run_loop(st, steps)
        self.assertIn("step rotation: a full turn of 3 steps took 5400s", log)
        self.assertEqual(self.rotation(st)["turn_started"], st.now)

    def test_a_turn_cut_short_keeps_its_start(self):
        gh = Counter(counted=0, target=500)
        st = self.station(gh)
        self.run_loop(st, self.steps(gh, [1, 1, 1], []))
        started = self.rotation(st)["turn_started"]
        st.now += 60
        gh.counted = gh.requests = 10000
        log = self.run_loop(st, self.steps(gh, [400, 400, 400], []))
        self.assertNotIn("full turn", log)
        self.assertEqual(self.rotation(st)["turn_started"], started)
        self.assertEqual(self.rotation(st)["next"], 1)


if __name__ == "__main__":
    unittest.main()
