"""bus/flake.py: which failed Actions jobs failed on the infrastructure, and
the once-only re-run of them (loom#1962)."""

import unittest

from bus import flake
from tests.test_bus_factory import REPO, FakeGitHub

INSTALL = ("##[error]The action 'Install what the core's own Dockerfile installs' "
           "has timed out after 10 minutes.")


def actions_run(job, run, name="adapter", conclusion="failure"):
    return {"id": job, "name": name, "status": "completed", "conclusion": conclusion,
            "details_url": "https://github.com/%s/actions/runs/%d/job/%d" % (REPO, run, job)}


class InfraFlake(unittest.TestCase):
    def test_a_setup_or_install_step_timeout_is_a_flake(self):
        self.assertTrue(flake.infra_flake(INSTALL))
        self.assertTrue(flake.infra_flake(
            "##[error]The action 'Set up Python' has timed out after 5 minutes."))
        self.assertTrue(flake.infra_flake(
            "##[error]The action 'apt-get update' has timed out after 3 minutes."))

    def test_a_test_or_build_step_timeout_is_not_a_flake(self):
        self.assertFalse(flake.infra_flake(
            "##[error]The action 'Run unit tests' has timed out after 20 minutes."))
        self.assertFalse(flake.infra_flake(
            "##[error]The action 'Build the module' has timed out after 30 minutes."))

    def test_runner_and_mirror_failures_are_flakes(self):
        self.assertTrue(flake.infra_flake("##[error]The runner has received a shutdown signal."))
        self.assertTrue(flake.infra_flake(
            "The self-hosted runner: lane-1 lost communication with the server."))
        self.assertTrue(flake.infra_flake(
            "E: Failed to fetch http://archive.example/pool/x.deb  Connection timed out"))
        self.assertTrue(flake.infra_flake(
            "Err:1 http://archive.example jammy InRelease\n"
            "  Temporary failure resolving 'archive.example'"))

    def test_an_ordinary_failure_is_not_a_flake(self):
        self.assertFalse(flake.infra_flake(None))
        self.assertFalse(flake.infra_flake(""))
        self.assertFalse(flake.infra_flake(
            "FAILED tests/test_a.py::test_x - assert 1 == 2\n"
            "##[error]Process completed with exit code 1."))


class FlakyNamesAndRerun(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()

    def test_only_failed_actions_jobs_with_a_flake_log_are_named(self):
        self.gh.logs.update({"1": INSTALL, "2": "FAILED test_x\n##[error]exit code 1",
                             "3": INSTALL})
        runs = [actions_run(1, 10), actions_run(2, 11, "unit"),
                actions_run(3, 12, "review", conclusion="failure"),
                actions_run(4, 13, "lint", conclusion="success"),
                {"id": 5, "name": "external", "status": "completed", "conclusion": "failure",
                 "details_url": "https://ci.example/5", "output": {"summary": INSTALL}}]
        self.assertEqual(flake.flaky_names(self.gh, REPO, runs, skip=("review",)), ["adapter"])

    def test_check_output_is_read_when_no_log_is_readable(self):
        run = dict(actions_run(1, 10), output={"title": "x", "summary": INSTALL})
        run.pop("id")
        self.assertEqual(flake.flaky_names(self.gh, REPO, [run]), ["adapter"])

    def test_each_workflow_run_is_rerun_once_and_never_on_its_second_attempt(self):
        runs = [actions_run(1, 10), actions_run(2, 10, "adapter (heads)"),
                actions_run(3, 11, "other")]
        self.gh.attempts["11"] = 2
        done = flake.rerun_once(self.gh, REPO, runs, ["adapter", "adapter (heads)", "other"])
        self.assertEqual(done, ["10"])
        self.assertEqual([w[1] for w in self.gh.writes],
                         ["/repos/%s/actions/runs/10/rerun-failed-jobs" % REPO])

    def test_a_check_not_named_is_not_rerun(self):
        self.assertEqual(flake.rerun_once(self.gh, REPO, [actions_run(1, 10)], ["unit"]), [])
        self.assertEqual(self.gh.writes, [])


if __name__ == "__main__":
    unittest.main()
