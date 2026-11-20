"""The job-cap lane test under a refused nested sandbox-exec."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A sandbox-exec that can never apply a profile, as inside a lane's own
# check sandbox: it prints the refusal the real one prints there and
# exits with the real one's setup code, 71.
FAKE_SANDBOX = ("#!/bin/sh\n"
                "echo 'sandbox-exec: sandbox_apply: Operation not permitted' >&2\n"
                "exit 71\n")


class JobCapNestedSandboxTest(unittest.TestCase):
    def test_the_job_cap_lane_test_reports_no_failure_when_nested_sandbox_exec_is_refused(self):
        # The job-cap test in test_run_lane runs a whole lane pass. On a
        # host whose nested sandbox-exec is refused, that pass's setup
        # fails before the agent runs, so the test failed on a green main
        # and no loom ticket could be built there. Run it in a subprocess
        # whose first sandbox-exec is the refused one above: the run must
        # report no failure (a pass, or a skip with a reason).
        with tempfile.TemporaryDirectory() as shim:
            fake = Path(shim) / "sandbox-exec"
            fake.write_text(FAKE_SANDBOX)
            fake.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = shim + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                [sys.executable, "-B", "-m", "unittest",
                 "tests.test_run_lane.LaneRunnerTest.test_a_lane_checks_steps_run_under_the_job_cap"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        self.assertNotIn("FAILED", result.stderr, result.stderr[-2000:])
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
