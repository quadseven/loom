"""The validation-gap reader (bus/ci_gap.py) and the failure facts of a red
CI log (bus/ci_facts.py), loom#1773."""
import base64
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

    def test_output_plumbing_and_the_python3_name_do_not_make_a_gap(self):
        # The suite CI tees to a log under pipefail is the suite the contract
        # runs; python and python3 are the same interpreter on a lane
        # (loom#1977: the lane contract already ran this command).
        contract = "python3 -B .github/unittest_parallel.py --allow-skips"
        self.assertTrue(ci_gap.covered(
            "set -o pipefail\npython -B .github/unittest_parallel.py --allow-skips 2>&1 | tee unittest.out",
            contract))
        self.assertTrue(ci_gap.covered("python3 -m pytest -q > out.log 2>&1", "python -m pytest -q"))
        # A pipe into another check is not plumbing, and a flag the contract
        # leaves out is still a gap.
        self.assertFalse(ci_gap.covered("make t | grep -q ok", "make t"))
        self.assertFalse(ci_gap.covered(
            "python -B .github/unittest_parallel.py --strict 2>&1 | tee unittest.out",
            "python3 -B .github/unittest_parallel.py --allow-skips"))

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


SETUP_WORKFLOW = """name: adapter
on:
  pull_request:
jobs:
  adapter:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - name: Install packages
        timeout-minutes: 10
        run: |
          set -euo pipefail
          sudo apt-get update -qq || echo "::warning::update failed"
          for attempt in 1 2 3; do
            if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \\
              cmake clang; then
              break
            fi
            if [ "$attempt" -eq 3 ]; then
              echo "stalled" >&2
              exit 1
            fi
            sleep 5
          done
          clang++ --version | head -1
      - name: Tests
        timeout-minutes: 10
        run: python -m unittest discover -s tests
"""


class _Data:
    def __init__(self, data):
        self.data = data


class SetupGh:
    """What find_gaps reads: the Actions job and run, and the workflow."""

    def __init__(self, steps, job=None):
        self.job = dict(job or {}, run_id=77, name="adapter", steps=steps)

    def rest(self, method, path, data=None, ok=()):
        if path == "/repos/o/r/actions/jobs/501":
            return _Data(self.job)
        if path == "/repos/o/r/actions/runs/77":
            return _Data({"path": ".github/workflows/adapter.yml"})
        if path.startswith("/repos/o/r/contents/.github/workflows/adapter.yml"):
            return _Data({"content": base64.b64encode(SETUP_WORKFLOW.encode()).decode()})
        raise AssertionError(path)


def _step(name, conclusion="failure", minutes=1):
    return {"name": name, "conclusion": conclusion, "started_at": "2026-10-26T22:00:00Z",
            "completed_at": "2026-10-26T22:%02d:00Z" % minutes}


class RunnerSetupTest(unittest.TestCase):
    """loom#1942: a failing runner setup step (installing system packages, a
    cancelled or timed-out step) was filed as a validation gap asking every
    lane validation to run sudo apt-get install."""

    RUN = {"id": 501, "name": "adapter", "conclusion": "failure", "app": {"slug": "github-actions"}}

    def gaps(self, steps, run=None, job=None):
        said = []
        found = ci_gap.find_gaps(SetupGh(steps, job), "o/r", 7, "a" * 40, {"check": "true"},
                                 [dict(self.RUN, **(run or {}))], lambda: [], said.append)
        return found, said

    def test_a_step_that_only_installs_system_packages_is_logged_not_filed(self):
        gaps, said = self.gaps([_step("Install packages")])
        self.assertEqual(gaps, [])
        self.assertEqual(said, ["validation gap: o/r#7 CI job adapter step Install packages only "
                                "installs system packages; not a gap"])

    def test_a_failing_step_that_runs_the_repos_tests_is_still_a_gap(self):
        gaps, said = self.gaps([_step("Install packages", "success"), _step("Tests")])
        self.assertEqual([g.step for g in gaps], ["Tests"])
        self.assertEqual(gaps[0].command, "python -m unittest discover -s tests")
        self.assertEqual(said, [])

    def test_a_step_that_ran_into_its_timeout_is_not_a_gap(self):
        gaps, _ = self.gaps([_step("Install packages", "success"), _step("Tests", minutes=10)])
        self.assertEqual(gaps, [])

    def test_a_cancelled_step_or_run_or_a_timed_out_job_is_not_a_gap(self):
        self.assertEqual(self.gaps([_step("Tests", "cancelled")])[0], [])
        self.assertEqual(self.gaps([_step("Tests")], run={"conclusion": "cancelled"})[0], [])
        self.assertEqual(self.gaps([_step("Tests")], run={"conclusion": "timed_out"})[0], [])
        self.assertEqual(self.gaps([_step("Tests")], job={
            "started_at": "2026-10-26T22:00:00Z", "completed_at": "2026-10-26T22:20:05Z"})[0], [])

    def test_what_counts_as_installing_system_packages(self):
        for command in ("command -v timeout || brew install coreutils", "apk add --no-cache bash",
                        "sudo dnf install -y gcc", "sudo -E env X=1 apt-get -qq update"):
            self.assertTrue(ci_gap.setup_only(command), command)
        for command in ("pip install -r requirements.txt", "sudo apt-get install -y x && make test",
                        "python -m pytest", "set -e", "echo hi"):
            self.assertFalse(ci_gap.setup_only(command), command)


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


# The Elder output that loom#665's round 1 brief read as a command (loom#1898).
ELDER_665 = (
    "Elder markings - 1 blocking, 1 total\n"
    "<details>\n<summary>Prompt for AI agents (all findings)</summary>\n\n"
    "```\n"
    "Verify each finding against the current code. Fix only if still valid.\n"
    "- lanes/run_lane.sh:3226 [high | robustness | missing-error-handling] the gh call counter "
    "treats a missing log as zero calls.\n"
    "  Suggested fix: AGENT_GH_CALLS=$(tail -c +$((LOG_START + 1)) $LOG 2>/dev/null | "
    "grep -cE '(^|[[:space:]])gh [^[:space:]]+' || true); [ -s $LOG ] || AGENT_GH_CALLS=unknown\n"
    "```\n</details>\n\n"
    "**Required fixes**\n\n"
    "1. `lanes/run_lane.sh:3226` `missing-error-handling` - AGENT_GH_CALLS=$(tail -c "
    "+$((LOG_START + 1)) $LOG 2>/dev/null | grep -cE 'gh ' || true)\n")


class _Gh:
    """Just enough of GitHub for _failing_checks: one check-runs page."""

    def __init__(self, runs):
        self.runs = runs

    def rest(self, method, path):
        data = {"check_runs": self.runs} if "check-runs" in path else {"statuses": []}
        return type("Resp", (), {"data": data})()


class ReviewFactsTest(unittest.TestCase):
    """A review app's output: only what the reviewer marks is a command
    (loom#1898)."""

    def test_a_suggested_fix_is_quoted_as_guidance_not_a_command(self):
        tail = ci_facts.ReviewText(ELDER_665)
        facts = ci_facts.facts(tail, review=True)
        self.assertEqual(facts.regenerate, [])
        self.assertEqual(len(facts.suggestions), 1)
        self.assertTrue(facts.suggestions[0].startswith("Suggested fix: AGENT_GH_CALLS="))
        body = brain._fix_body("o/r", 8, "loom/665-x", "", [("Grug - Elder", tail)], [])
        self.assertNotIn("First step", body)
        self.assertNotIn("run `", body)
        self.assertIn("Suggested fixes (the reviewer's guidance on the finding to address, "
                      "not a command to run):\n\n    Suggested fix: AGENT_GH_CALLS=", body)

    def test_the_same_text_as_a_log_still_reads_a_command(self):
        # Without the fix this is what the station did with Elder's output.
        self.assertTrue(ci_facts.facts(ELDER_665).regenerate)

    def test_a_fenced_shell_block_is_the_only_command(self):
        text = ci_facts.ReviewText(
            "The generated file is stale.\nSuggested fix: edit it by hand > out.yaml\n"
            "```bash\n$ python3 render.py --render-rbac > rbac.yaml\n```\n"
            "```suggestion\nrm -rf build --force\n```\n"
            "```\nmake all --jobs 4\n```\n")
        self.assertEqual(ci_facts.facts(text, review=True).regenerate,
                         ["python3 render.py --render-rbac > rbac.yaml"])
        body = brain._fix_body("o/r", 8, "loom/9-x", "", [("Grug - Elder", text)], [])
        self.assertEqual([l for l in body.splitlines() if l.startswith("First step")],
                         ["First step: run `python3 render.py --render-rbac > rbac.yaml`; the review "
                          "in check `Grug - Elder` marks it as a command to run."])

    def test_a_run_or_regenerate_line_is_a_command(self):
        self.assertEqual(ci_facts.review_commands(
            "Run: `make render --all`\n- Regenerate: python3 r.py --out x.yaml\nrun: yaml key"),
            ["make render --all", "python3 r.py --out x.yaml"])

    def test_a_finding_with_no_command_gives_no_run_instruction(self):
        text = ci_facts.ReviewText("The counter treats a missing log as zero; it is stale and "
                                   "should be regenerated with care > later.\n")
        self.assertEqual(ci_facts.facts(text, review=True).regenerate, [])
        body = brain._fix_body("o/r", 8, "loom/9-x", "", [("Grug - Elder", text)], [])
        self.assertNotIn("First step", body)

    def test_a_review_apps_output_is_review_text_and_an_actions_log_is_not(self):
        runs = [{"id": 1, "name": "Grug - Elder", "conclusion": "failure",
                 "app": {"slug": "grug-tribe"}, "output": {"summary": ELDER_665}}]
        checks = brain._failing_checks(_Gh(runs), "o/r", "abc")
        self.assertIsInstance(checks[0][1], ci_facts.ReviewText)


if __name__ == "__main__":
    unittest.main()
