"""The unittest step must fail when the suite fails: it pipes into tee, so it
needs pipefail (on 2026-10-14 a failing ASCII test passed CI for hours)."""
from __future__ import annotations

import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml"


class UnittestStep(unittest.TestCase):
    def test_the_piped_unittest_step_runs_with_pipefail(self):
        text = WORKFLOW.read_text()
        step = text[text.index("- name: unittest"):]
        step = step[:step.index("tee unittest.out")]
        self.assertIn("shell: bash", step)
        self.assertIn("set -o pipefail", step)


class ConcurrencyTest(unittest.TestCase):
    def test_runs_are_grouped_per_branch_and_pull_requests_cancel_older_runs(self):
        text = WORKFLOW.read_text()
        self.assertIn("concurrency:\n  group: test-${{ github.ref }}\n", text)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text)


if __name__ == "__main__":
    unittest.main()
