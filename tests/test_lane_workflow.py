""".github/workflows/lane.yml, one lane run on a cluster runner
(quadseven/loom#1824): the runner set, least privilege, a bound, one run per
slot at a time, and no dispatch input in a step's script or environment
(GitHub prints a step's environment in the public log)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "lane.yml"


class LaneWorkflow(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), "lane.yml is missing")
        self.text = WORKFLOW.read_text()

    def steps(self):
        body = self.text[self.text.index("    steps:\n"):]
        return re.split(r"\n      - ", body)[1:]

    def test_it_is_dispatched_with_the_runs_inputs(self):
        self.assertIn("  workflow_dispatch:\n", self.text)
        for name in ("slot", "ticket", "brief", "run_id", "settings", "report_key", "report_mac_key"):
            self.assertRegex(self.text, r"\n      %s:\n" % name)
        # The station finds a run by this name (bus/runners.py _find_run).
        self.assertIn("run-name: lane ${{ inputs.slot }} ${{ inputs.run_id }}\n", self.text)

    def test_it_runs_on_the_lane_runner_set_with_least_privilege(self):
        self.assertIn("runs-on: arc-loom-lanes\n", self.text)
        self.assertIn("\npermissions: {}\n", self.text)
        job = self.text[self.text.index("    permissions:\n"):]
        job = job[:job.index("    steps:")]
        self.assertEqual(re.findall(r"^      ([a-z-]+): (\w+)", job, re.M),
                         [("contents", "read"), ("id-token", "write")])
        self.assertRegex(self.text, r"\n    timeout-minutes: \d+\n")
        self.assertIn("group: lane-${{ inputs.slot }}\n", self.text)
        self.assertIn("cancel-in-progress: false\n", self.text)
        # The role trusts the OIDC subject of main with no environment.
        self.assertNotRegex(self.text, r"(?m)^\s+environment:")

    def test_no_input_reaches_a_script_or_an_environment(self):
        checked = 0
        for step in self.steps():
            for line in step.splitlines():
                line = line.strip()
                if line.startswith("run:") or re.match(r"^[A-Z][A-Z0-9_]*:", line):
                    checked += 1
                    self.assertNotIn("inputs.", line, step)
                    self.assertNotIn("github.event", line, step)
        self.assertGreater(checked, 5)

    def test_egress_is_proved_before_the_role_is_assumed_and_the_session_stays_in_one_step(self):
        steps = self.steps()
        names = [s.split("\n", 1)[0] for s in steps]
        egress = next(i for i, n in enumerate(names) if "lane_job.py egress" in steps[i])
        role = next(i for i, s in enumerate(steps) if "configure-aws-credentials@" in s)
        ssm = next(i for i, s in enumerate(steps) if "lane_job.py ssm" in s)
        run = next(i for i, s in enumerate(steps) if "lane_job.py run" in s)
        self.assertLess(egress, role, names)
        self.assertLess(role, ssm)
        self.assertLess(ssm, run)
        self.assertIn("output-env-credentials: false", steps[role])
        self.assertIn("role-to-assume: ${{ vars.LOOM_LANE_ROLE_ARN }}", steps[role])
        self.assertIn("AWS_SESSION_TOKEN: ${{ steps.aws.outputs.aws-session-token }}", steps[ssm])
        self.assertNotIn("AWS_", steps[run])

    def test_only_a_trusted_dispatch_on_main_runs(self):
        # A pull request could add a workflow naming this runner label; the
        # job runs only when dispatched on main, and a first step refuses any
        # dispatcher outside LOOM_LANE_DISPATCHERS.
        self.assertIn("    if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'\n",
                      self.text)
        first = self.steps()[0]
        self.assertIn("ACTOR: ${{ github.triggering_actor }}", first)
        self.assertIn("TRUSTED: ${{ vars.LOOM_LANE_DISPATCHERS }}", first)
        self.assertIn("exit 1", first)

    def test_the_report_is_uploaded_whatever_the_run_did(self):
        upload = next(s for s in self.steps() if "actions/upload-artifact@" in s)
        self.assertIn("name: lane-report-${{ inputs.run_id }}", upload)
        # Uploaded whatever the run did, but only once prepare checked the
        # run_id that names it.
        self.assertIn("if: ${{ !cancelled() && steps.prepare.outcome == 'success' }}", upload)
        seal = next(s for s in self.steps() if "lane_job.py report" in s)
        self.assertIn("steps.prepare.outcome == 'success'", seal)
        prepare = next(s for s in self.steps() if "lane_job.py prepare" in s)
        self.assertIn("id: prepare", prepare)


if __name__ == "__main__":
    unittest.main()
