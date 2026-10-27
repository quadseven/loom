"""A station pass that overruns LOOM_PASS_DEADLINE dumps its stack and exits
(loom#1921)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import station  # noqa: E402

REPO = "example-owner/example-repo"

# A pass whose step loop blocks for far longer than its 1-second deadline.
BLOCKING_PASS = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from bus import station

    def stuck_in_a_read(self):
        time.sleep(60)

    class FakeGitHub:
        cache_dir = None

    station.Station.run_pass = stuck_in_a_read
    station._trust_app_login = lambda settings, gh: None
    settings = station.Settings(sys.argv[2], [%r], [], [], pass_deadline=1)
    station.run(settings, FakeGitHub())
    print("the pass returned", file=sys.stderr)
""" % REPO)


class PassDeadline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "loom")
        self.marker = os.path.join(self.home, "state", "station", station.DEADLINE_MARKER_FILE)

    def quiet_pass(self, settings, run_pass=lambda self: None):
        err = io.StringIO()
        with mock.patch.object(station.Station, "run_pass", run_pass), \
                mock.patch.object(station.Station, "end_pass", lambda self: None), \
                mock.patch.object(station, "_trust_app_login", lambda s, gh: None), \
                contextlib.redirect_stderr(err):
            gh = mock.Mock(cache_dir="set", app=None)
            code = station.run(settings, gh)
        return code, err.getvalue()

    def test_an_overrunning_pass_writes_its_stack_to_the_log_and_exits(self):
        log_path = os.path.join(self._tmp.name, "station.log")
        started = time.monotonic()
        with open(log_path, "w") as log:
            proc = subprocess.run([sys.executable, "-c", BLOCKING_PASS, ROOT, self.home],
                                  stdout=subprocess.DEVNULL, stderr=log, timeout=50)
        took = time.monotonic() - started
        with open(log_path) as fh:
            log_text = fh.read()
        self.assertNotEqual(proc.returncode, 0)
        self.assertLess(took, 30, "the pass was not stopped at its deadline")
        self.assertIn("Timeout", log_text)
        self.assertIn("stuck_in_a_read", log_text)
        self.assertNotIn("the pass returned", log_text)
        # The marker stays behind for the next pass to report.
        with open(self.marker) as fh:
            marker = json.load(fh)
        self.assertEqual(marker["deadline"], 1)

        # The next pass names the overrun once, then clears the marker.
        code, said = self.quiet_pass(station.Settings(self.home, [REPO], [], [],
                                                      pass_deadline=600))
        self.assertEqual(code, 0)
        overrun = [line for line in said.splitlines() if "overran" in line]
        self.assertEqual(len(overrun), 1, said)
        self.assertIn("overran its 1s deadline", overrun[0])
        self.assertIn("pid %d" % marker["pid"], overrun[0])
        self.assertFalse(os.path.exists(self.marker))
        _, said = self.quiet_pass(station.Settings(self.home, [REPO], [], [],
                                                   pass_deadline=600))
        self.assertNotIn("overran", said)

    def test_a_pass_that_ends_in_time_cancels_its_deadline_and_leaves_no_marker(self):
        seen = {}

        def look(st):
            seen["marker"] = os.path.exists(self.marker)

        with mock.patch.object(station.faulthandler, "dump_traceback_later") as arm, \
                mock.patch.object(station.faulthandler, "cancel_dump_traceback_later") as cancel:
            code, said = self.quiet_pass(
                station.Settings(self.home, [REPO], [], [], pass_deadline=600), look)
        self.assertEqual(code, 0)
        self.assertTrue(seen["marker"], "the marker is written while the pass runs")
        self.assertEqual(arm.call_count, 1)
        self.assertEqual(arm.call_args.args, (600,))
        self.assertTrue(arm.call_args.kwargs["exit"])
        self.assertEqual(cancel.call_count, 1)
        self.assertFalse(os.path.exists(self.marker))
        self.assertNotIn("overran", said)

    def test_a_pass_that_raises_still_cancels_its_deadline(self):
        def boom(st):
            raise RuntimeError("boom")

        with mock.patch.object(station.faulthandler, "dump_traceback_later"), \
                mock.patch.object(station.faulthandler, "cancel_dump_traceback_later") as cancel:
            with self.assertRaises(RuntimeError):
                self.quiet_pass(station.Settings(self.home, [REPO], [], [],
                                                 pass_deadline=600), boom)
        self.assertEqual(cancel.call_count, 1)
        self.assertFalse(os.path.exists(self.marker))

    def test_a_killed_pass_is_not_called_an_overrun(self):
        os.makedirs(os.path.dirname(self.marker))
        with open(self.marker, "w") as fh:
            json.dump({"pid": 4242, "started": time.time() - 30, "deadline": 3600}, fh)
        with mock.patch.object(station.faulthandler, "dump_traceback_later"), \
                mock.patch.object(station.faulthandler, "cancel_dump_traceback_later"):
            _, said = self.quiet_pass(station.Settings(self.home, [REPO], [], [],
                                                       pass_deadline=600))
        self.assertNotIn("overran", said)
        self.assertIn("without a clean finish", said)

    def test_settings_read_the_deadline_from_the_environment(self):
        self.assertEqual(station._pass_deadline(""), station.PASS_DEADLINE)
        self.assertEqual(station._pass_deadline("0"), 0)
        self.assertEqual(station._pass_deadline("900"), 900)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(station._pass_deadline("5"), station.PASS_DEADLINE)
            self.assertEqual(station._pass_deadline("soon"), station.PASS_DEADLINE)
        self.assertGreater(station.PASS_DEADLINE, 1800)
        settings = station.Settings.from_env({"LOOM_HOME": self.home, "LOOM_REPOS": REPO,
                                              "LOOM_PASS_DEADLINE": "1200"})
        self.assertEqual(settings.pass_deadline, 1200)
        self.assertEqual(station.Settings(self.home, [REPO], [], []).pass_deadline, 0)


if __name__ == "__main__":
    unittest.main()
