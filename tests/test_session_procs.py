"""tests/session_procs.py: a run's session is emptied when it ends or times
out, also a process that moved to a group of its own, and also when ps cannot
run (loom#2438)."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import session_procs

# A shell that starts a job-control subshell (a process group of its own, as
# lanes/run_lane.sh starts its agent and watchdog) that records its pid and
# sleeps, then sleeps itself.
SCRIPT = 'set -m; (exec sh -c \'echo $$ > "$1"; exec sleep 60\' sh "$1") & sleep "$2"'


class SessionProcsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pidfile = Path(self._tmp.name) / "child.pid"

    def child(self):
        for _ in range(50):
            if self.pidfile.exists() and self.pidfile.read_text().strip():
                return int(self.pidfile.read_text())
            time.sleep(0.1)
        self.fail("the child never started")

    def gone(self, pid):
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def run_script(self, wait, timeout):
        return session_procs.run(["bash", "-c", SCRIPT, "bash", str(self.pidfile), str(wait)],
                                 timeout=timeout)

    def test_a_timed_out_run_leaves_no_process_in_another_group(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_script(60, 2)
        self.assertTrue(self.gone(self.child()))

    def test_a_finished_run_leaves_no_process_in_another_group(self):
        result = self.run_script(1, 30)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.gone(self.child()))

    def test_without_ps_the_session_is_still_found_and_emptied(self):
        real = subprocess.run

        def no_ps(argv, *args, **kw):
            if argv[:1] == ["ps"]:
                raise OSError("ps cannot run here")
            return real(argv, *args, **kw)

        with mock.patch.object(session_procs.subprocess, "run", side_effect=no_ps):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_script(60, 2)
        self.assertTrue(self.gone(self.child()))


if __name__ == "__main__":
    unittest.main()
