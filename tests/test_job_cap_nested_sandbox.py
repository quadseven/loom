"""The job-cap lane test under a refused nested sandbox-exec."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The lane runner in test_run_lane starts with a fixed PATH whose first entry
# is the test's own bin directory, so a sandbox-exec put on this process's
# PATH never reaches it. The refused sandbox-exec goes into that bin
# directory instead, written after setUp: it prints the refusal the real one
# prints inside a lane's own check sandbox and exits with its setup code, 71.
INNER = r"""
import sys
import unittest
sys.path.insert(0, "tests")
import test_run_lane

REFUSED = ("#!/bin/sh\n"
           "echo 'sandbox-exec: sandbox_apply: Operation not permitted' >&2\n"
           "exit 71\n")


class Refused(test_run_lane.LaneRunnerTest):
    def setUp(self):
        super().setUp()
        self.write_tool(self.bin / "sandbox-exec", REFUSED)


suite = unittest.TestSuite([Refused("test_a_lane_checks_steps_run_under_the_job_cap")])
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
"""


class JobCapNestedSandboxTest(unittest.TestCase):
    def test_the_job_cap_lane_test_reports_no_failure_when_nested_sandbox_exec_is_refused(
        self,
    ):
        # On a host whose nested sandbox-exec is refused (a lane's own check
        # sandbox), the job-cap test's lane pass failed at setup (exit 71)
        # on a green main, and no loom ticket could be built there. With the
        # refused sandbox-exec first on the lane runner's PATH, the test must
        # still pass with its job-cap assertions.
        result = subprocess.run(
            [sys.executable, "-B", "-c", INNER],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertNotIn("FAILED", result.stderr, result.stderr[-2000:])
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
