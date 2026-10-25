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
    def test_parallel_runner_caps_workers_to_avoid_oversubscribing_lane_checks(self):
        runner = WORKFLOW.parents[1] / "unittest_parallel.py"
        spec = __import__("importlib.util").util.spec_from_file_location("unittest_parallel", runner)
        module = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.worker_count(145, 12), 4)
        self.assertEqual(module.worker_count(2, 12), 2)
        self.assertEqual(module.worker_count(145, 2), 2)

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

    def test_allow_skips_lets_a_lane_sandbox_skip_but_still_fails_a_failure(self):
        # The lane check runs this runner inside a sandbox where tests that need
        # sandbox-exec, ps or a localhost port skip by design (about 118 of
        # them); CI still refuses every skip.
        runner = WORKFLOW.parents[1] / "unittest_parallel.py"
        with tempfile.TemporaryDirectory() as temp:
            tests = Path(temp, "tests")
            tests.mkdir()
            Path(tests, "test_skip.py").write_text(
                "import unittest\nclass Skip(unittest.TestCase):\n"
                " @unittest.skip('needs sandbox-exec')\n def test_skips(self): pass\n")
            allowed = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests),
                                      "--allow-skips"], text=True, capture_output=True)
            self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
            Path(tests, "test_failure.py").write_text(
                "import unittest\nclass Failure(unittest.TestCase):\n"
                " def test_fails(self): self.fail('expected failure')\n")
            failed = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests),
                                     "--allow-skips"], text=True, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)

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
            # Each module lists its slowest tests, so a stalled one is named.
            self.assertEqual(result.stdout.count("Slowest test durations"), 3)

    def test_parallel_runner_shards_a_large_module_and_keeps_its_rules(self):
        # One module bounds the step's wall time unless its tests are split
        # across processes; a failure or a disallowed skip in any shard must
        # still fail the run, and the shards' totals must add up.
        runner = WORKFLOW.parents[1] / "unittest_parallel.py"
        with tempfile.TemporaryDirectory() as temp:
            tests = Path(temp, "tests")
            tests.mkdir()
            body = "".join(" def test_%d(self): %s\n" % (index, action)
                           for index, action in enumerate(
                               ["pass", "pass", "self.fail('middle shard')", "pass", "pass"]))
            Path(tests, "test_big.py").write_text(
                "import unittest\nclass Big(unittest.TestCase):\n" + body)
            failed = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests),
                                     "--shard-size", "2"], text=True, capture_output=True)
            for shard in (1, 2, 3):
                self.assertIn("===== test_big [%d/3] =====" % shard, failed.stdout)
            self.assertIn("Parallel unittest total: Ran 5 tests", failed.stdout)
            self.assertIn("middle shard", failed.stdout)
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)

            Path(tests, "test_big.py").write_text(
                "import unittest\nclass Big(unittest.TestCase):\n"
                " def test_0(self): pass\n def test_1(self): pass\n"
                " @unittest.skip('not allowed')\n def test_2(self): pass\n")
            skipped = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests),
                                      "--shard-size", "2"], text=True, capture_output=True)
            self.assertIn("===== test_big [2/2] =====", skipped.stdout)
            self.assertIn("::error::a test skipped", skipped.stdout)
            self.assertEqual(skipped.returncode, 1, skipped.stdout + skipped.stderr)

    def test_parallel_runner_runs_a_module_whole_when_its_ids_cannot_load(self):
        runner = WORKFLOW.parents[1] / "unittest_parallel.py"
        with tempfile.TemporaryDirectory() as temp:
            tests = Path(temp, "tests")
            tests.mkdir()
            Path(tests, "test_broken.py").write_text("import a_module_that_does_not_exist\n")
            result = subprocess.run([sys.executable, str(runner), "--tests-dir", str(tests),
                                     "--shard-size", "2"], text=True, capture_output=True)
            self.assertIn("===== test_broken =====", result.stdout)
            self.assertIn("a_module_that_does_not_exist", result.stdout)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


class ConcurrencyTest(unittest.TestCase):
    def test_runs_are_grouped_per_branch_and_pull_requests_cancel_older_runs(self):
        text = WORKFLOW.read_text()
        self.assertIn("concurrency:\n  group: test-${{ github.ref }}\n", text)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text)


if __name__ == "__main__":
    unittest.main()
