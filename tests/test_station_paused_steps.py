"""LOOM_PAUSED_STEPS: named pass steps skipped with no station reload (loom#1630).

Bulk cleanups need triage, rewrite and the outside-epic move held, or each
relabel draws bot comments. The setting comes from the environment or from
state/station/paused_steps.txt, read again on every pass.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import station  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401


class PausedStepsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.st = station.Station.__new__(station.Station)
        self.st.state = folder.name
        self.ran = []
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LOOM_PAUSED_STEPS", None)

    def run_steps(self):
        def timed(_self, step):
            self.ran.append(step.__name__)
            return True

        err = io.StringIO()
        self.ran = []
        with mock.patch.object(station.Station, "_timed", timed), \
                mock.patch.object(station.Station, "_note_every_pass", lambda _self: None), \
                mock.patch.object(station.Station, "_deferred", lambda _self: False), \
                mock.patch.object(station, "PASS_STEP_BUDGET", 10 ** 6), \
                contextlib.redirect_stderr(err):
            self.st._run_steps()
        return err.getvalue()

    def write_file(self, text):
        with open(os.path.join(self.st.state, station.PAUSED_STEPS_FILE), "w") as fh:
            fh.write(text)

    def test_a_step_named_in_the_environment_does_not_run_and_is_logged(self):
        os.environ["LOOM_PAUSED_STEPS"] = "triage_backlogs rewrite_triaged"
        log = self.run_steps()
        self.assertNotIn("triage_backlogs", self.ran)
        self.assertNotIn("rewrite_triaged", self.ran)
        self.assertIn("close_done_tickets", self.ran)
        self.assertIn("step triage_backlogs paused (LOOM_PAUSED_STEPS)", log)
        self.assertIn("step rewrite_triaged paused (LOOM_PAUSED_STEPS)", log)

    def test_a_step_named_in_the_state_file_does_not_run_and_is_logged(self):
        self.write_file("move_outside_tickets\n")
        log = self.run_steps()
        self.assertNotIn("move_outside_tickets", self.ran)
        self.assertIn("check_invariants", self.ran)
        self.assertIn("step move_outside_tickets paused (LOOM_PAUSED_STEPS)", log)

    def test_removing_the_name_runs_the_step_on_the_next_pass_with_no_reload(self):
        self.write_file("triage_backlogs\n")
        self.run_steps()
        self.assertNotIn("triage_backlogs", self.ran)
        self.write_file("")
        log = self.run_steps()
        self.assertIn("triage_backlogs", self.ran)
        self.assertNotIn("paused", log)

    def test_an_unknown_name_is_logged_and_every_other_step_runs(self):
        os.environ["LOOM_PAUSED_STEPS"] = "no_such_step"
        log = self.run_steps()
        self.assertIn("unknown paused step no_such_step, ignored", log)
        self.assertIn("triage_backlogs", self.ran)
        self.assertIn("apply_decision_defaults", self.ran)

    def test_a_paused_step_counts_as_done_so_the_rotation_moves_on(self):
        os.environ["LOOM_PAUSED_STEPS"] = "triage_backlogs"
        self.run_steps()
        rotation = station._read_json(os.path.join(self.st.state, station.STEP_ROTATION_FILE), {})
        self.assertEqual(rotation, {"next": 0, "carried": None})


if __name__ == "__main__":
    unittest.main()
