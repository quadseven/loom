"""The unittest step must fail when the suite fails: it pipes into tee, so it
needs pipefail (on 2026-10-14 a failing ASCII test passed CI for hours)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
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

    def test_parallel_runner_fails_for_test_failures_and_disallowed_skips(self):
        runner = WORKFLOW.parents[1] / "unittest_parallel.py"
        with tempfile.TemporaryDirectory() as temp:
            tests = Path(temp, "tests")
            tests.mkdir()
            Path(tests, "test_failure.py").write_text(
                "import unittest\nclass Failure(unittest.TestCase):\n"
                " def test_fails(self): self.fail('expected failure')\n")
            failed = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests)],
                                    text=True, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("expected failure", failed.stdout + failed.stderr)

            Path(tests, "test_failure.py").unlink()
            Path(tests, "test_skip.py").write_text(
                "import unittest\nclass Skip(unittest.TestCase):\n"
                " @unittest.skip('not allowed')\n def test_skips(self): pass\n")
            skipped = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests)],
                                     text=True, capture_output=True)
            self.assertNotEqual(skipped.returncode, 0)
            self.assertIn("skipped", skipped.stdout + skipped.stderr)

            Path(tests, "test_skip.py").write_text(
                "import unittest\nclass Skip(unittest.TestCase):\n"
                " @unittest.skip('LOOM_TEST_OPENCODE2 is not configured')\n"
                " def test_skips(self): pass\n")
            allowed = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests)],
                                     text=True, capture_output=True)
            self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)

    def test_parallel_runner_reports_the_discovered_test_total(self):
        runner = WORKFLOW.parents[1] / "unittest_parallel.py"
        with tempfile.TemporaryDirectory() as temp:
            tests = Path(temp, "tests")
            tests.mkdir()
            for name in ("alpha", "beta"):
                Path(tests, "test_%s.py" % name).write_text(
                    "import unittest\nclass Sample(unittest.TestCase):\n"
                    " def test_one(self): pass\n")
            package = Path(tests, "nested")
            package.mkdir()
            Path(package, "__init__.py").write_text("")
            Path(package, "test_gamma.py").write_text(
                "import unittest\nclass Sample(unittest.TestCase):\n"
                " def test_one(self): pass\n")
            result = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests)],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Parallel unittest total: Ran 3 tests across 3 modules",
                          result.stdout)


class ConcurrencyTest(unittest.TestCase):
    def test_runs_are_grouped_per_branch_and_pull_requests_cancel_older_runs(self):
        text = WORKFLOW.read_text()
        self.assertIn("concurrency:\n  group: test-${{ github.ref }}\n", text)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text)


if __name__ == "__main__":
    unittest.main()
