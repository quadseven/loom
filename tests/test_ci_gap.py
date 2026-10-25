"""The validation-gap reader (bus/ci_gap.py) and the failure facts of a red
CI log (bus/ci_facts.py), loom#1773."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, ci_facts, ci_gap  # noqa: E402

WORKFLOW = """name: deploy tests
on:
  pull_request:
    paths:
      - "deploy/**"
jobs:
  render:
    name: render (${{ matrix.dir }})
    strategy:
      matrix:
        dir: [rbac, pins]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: pip install -r requirements.txt
      - name: Unit
        working-directory: deploy/${{ matrix.dir }}
        run: |
          python -m pytest -q \\
            tests
          echo done
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
"""


class WorkflowReaderTest(unittest.TestCase):
    def test_jobs_steps_and_the_pull_request_paths_are_read(self):
        jobs = ci_gap.workflow_jobs(WORKFLOW)
        self.assertEqual([(j.ident, j.name) for j in jobs],
                         [("render", "render (${{ matrix.dir }})"), ("lint", "lint")])
        self.assertEqual([(s.name, s.workdir) for s in jobs[0].steps],
                         [("Install", ""), ("Unit", "deploy/${{ matrix.dir }}")])
        self.assertEqual(jobs[0].steps[1].command, "python -m pytest -q \\\n  tests\necho done")
        self.assertEqual(ci_gap.pull_request_paths(WORKFLOW), ["deploy/**"])

    def test_a_check_run_finds_its_job_by_name_matrix_suffix_or_id(self):
        jobs = ci_gap.workflow_jobs(WORKFLOW)
        self.assertEqual(ci_gap.find_job(jobs, "render (rbac)").ident, "render")
        self.assertEqual(ci_gap.find_job(jobs, "lint").ident, "lint")
        self.assertEqual(ci_gap.find_job(jobs, "caller / lint").ident, "lint")
        self.assertIsNone(ci_gap.find_job(jobs, "deploy"))

    def test_the_failing_step_is_matched_by_name_or_by_its_run_line(self):
        jobs = ci_gap.workflow_jobs(WORKFLOW)
        steps = ci_gap.failing_steps(jobs[0], [{"name": "Unit", "conclusion": "failure"}])
        self.assertEqual([s.name for s in steps], ["Unit"])
        lint = ci_gap.failing_steps(jobs[1], [{"name": "Run ruff check .", "conclusion": "failure"}])
        self.assertEqual([s.command for s in lint], ["ruff check ."])
        # A failed step it cannot match: every run step of the job.
        self.assertEqual(len(ci_gap.failing_steps(
            jobs[0], [{"name": "Composite", "conclusion": "failure"}])), 2)
        # No failed step (a cancelled or timed-out job): no command is a gap.
        self.assertEqual(ci_gap.failing_steps(jobs[0], []), [])
        self.assertEqual(ci_gap.failing_steps(
            jobs[0], [{"name": "Unit", "conclusion": "success"}]), [])


class CoverageTest(unittest.TestCase):
    def test_a_command_the_contract_runs_is_covered(self):
        self.assertTrue(ci_gap.covered("cd x\nruff check .\necho ok", "ruff check . && pytest"))
        self.assertTrue(ci_gap.covered("python -m pytest -q \\\n  tests",
                                       "cd deploy/rbac && python -m pytest -q tests", "deploy/rbac"))

    def test_a_command_or_directory_the_contract_does_not_run_is_a_gap(self):
        self.assertFalse(ci_gap.covered("ruff check . && mypy .", "ruff check ."))
        # The same command from the root runs another suite.
        self.assertFalse(ci_gap.covered("python -m pytest", "python -m pytest", "deploy/rbac"))

    def test_the_line_to_add_is_affected_for_a_path_scoped_job_else_check(self):
        self.assertEqual(ci_gap.suggestion("make t", ["deploy/**"], {"check": "make lint"}),
                         "affected: make t")
        self.assertEqual(ci_gap.suggestion("make t", ["deploy/**"],
                                           {"check": "make lint", "affected": "./a.sh"}),
                         "affected: ./a.sh && make t")
        self.assertEqual(ci_gap.suggestion("make t", [], {"check": "make lint"}),
                         "check: make lint && make t")
        self.assertEqual(ci_gap.suggestion("make t", [], {"check": "true"}), "check: make t")


class FirstPushStatusTest(unittest.TestCase):
    def test_the_rate_is_per_repo_and_a_pull_request_counts_once(self):
        with tempfile.TemporaryDirectory() as home:
            state = os.path.join(home, "state", "station")
            self.assertTrue(ci_gap.note_first_push(state, "o/a", 1, True, 1.0))
            self.assertFalse(ci_gap.note_first_push(state, "o/a", 1, False, 2.0))
            ci_gap.note_first_push(state, "o/a", 2, False, 3.0)
            ci_gap.note_first_push(state, "o/b", 5, False, 4.0)
            lines = brain.validation_gap_lines({"LOOM_HOME": home})
        self.assertEqual(lines, [
            "## Validation gaps (first-push CI red rate per repo)",
            "- o/a: 1 of 2 lane pull requests red on first push (50%); 0 gaps filed",
            "- o/b: 0 of 1 lane pull requests red on first push (0%); 0 gaps filed"])

    def test_status_without_the_station_home_says_so(self):
        self.assertIn("not looked", brain.validation_gap_lines({})[1])


class FailureFactsTest(unittest.TestCase):
    LOG = ("FAIL: test_rbac (deploy.tests.test_render.RenderTest.test_rbac)\n"
           "Traceback (most recent call last):\n"
           "AssertionError: rbac.yaml is stale: regenerate it with "
           "`python3 deploy/render.py --render-rbac > deploy/rbac.yaml`\n"
           "FAILED tests/test_pin.py::test_image_pinned - assert 0 == 1\n"
           "--- FAIL: TestPinned (0.00s)\n"
           "##[error]Process completed with exit code 1.\n")

    def test_tests_assertions_and_the_regenerate_command_are_read(self):
        facts = ci_facts.facts(self.LOG)
        self.assertEqual(facts.tests, ["deploy.tests.test_render.RenderTest.test_rbac",
                                       "tests/test_pin.py::test_image_pinned", "TestPinned"])
        self.assertEqual(facts.assertions[0][:44], "AssertionError: rbac.yaml is stale: regenera")
        self.assertEqual(facts.regenerate, ["python3 deploy/render.py --render-rbac > deploy/rbac.yaml"])

    def test_a_render_redirect_counts_without_a_hint_and_prose_does_not(self):
        self.assertEqual(ci_facts.regenerate_commands("diff: run tool --render-rbac > out/rbac.yaml"),
                         ["tool --render-rbac > out/rbac.yaml"])
        self.assertEqual(ci_facts.regenerate_commands("the cache is stale, see the docs"), [])
        self.assertEqual(ci_facts.regenerate_commands("echo hi > x"), [])

    def test_the_log_excerpt_keeps_a_regenerate_line_far_above_the_tail(self):
        lines = (["rbac.yaml is out of date; run python3 r.py --render-rbac > rbac.yaml"]
                 + ["noise %d" % i for i in range(100)] + ["##[error]exit 1"])
        self.assertIn("--render-rbac > rbac.yaml", brain.log_excerpt(lines))

    def test_the_lists_are_bounded(self):
        log = "\n".join("FAILED t.py::test_%d - assert False" % i for i in range(50))
        facts = ci_facts.facts(log)
        self.assertEqual(len(facts.tests), ci_facts.TESTS_MAX)
        self.assertEqual(len(facts.assertions), ci_facts.ASSERTIONS_MAX)


if __name__ == "__main__":
    unittest.main()
