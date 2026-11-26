"""A new run whose agent commits nothing is not checked a second time: the
tree is the base the base check already covered, so the run goes straight to
its no-commit outcome instead of holding a check slot and ending as a
validation failure."""

import unittest

from tests import test_run_lane as base

# The check passes on the base and fails once the agent has written its
# summary, so a final check that ran after a no-change agent would be red.
CONTRACT = ("interpreter: loom-test-python\n"
            "check: test ! -e .loom/summary.md && loom-test-python check.py\n")
CHECK_PY = """import pathlib, sys
fix = pathlib.Path("fix.txt")
if fix.exists() and "fixed" in fix.read_text():
    sys.exit("the change fails the check")
"""


class NoCommitSkipsFinalCheckTest(base.LaneRunnerTest):
    def lane(self, **env):
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        self.contract_repo(contract=CONTRACT)
        self.drop_brief()
        return self.run_contract_lane(**env)

    def contract_repo(self, contract=CONTRACT):
        pinned = super().contract_repo(contract=contract)
        work = self.remote.parent / "check-work"
        base.git("clone", "-q", str(self.remote), str(work))
        (work / "check.py").write_text(CHECK_PY)
        base.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qam", "check", cwd=work)
        base.git("push", "-q", "origin", "main", cwd=work)
        return pinned

    def phases(self):
        return [r["phase"] for r in self.records()]

    def test_a_run_with_no_commit_and_no_reason_is_parked_as_no_commit_without_a_final_check(self):
        result = self.lane(FAKE_AGENT="noop")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.lane_log()
        self.assertIn("baseline", self.phases(), log)
        self.assertNotIn("final", self.phases(), log)
        self.assertIn("no commit on the lane branch: the final check is not run", log)
        self.assertIn("agent made no commit and wrote no reason", self.failure_line())
        self.assertNotIn("validation failed", self.failure_line())
        self.assertNotIn("-unvalidated-", self.remote_heads())

    def test_a_no_change_answer_is_accepted_without_a_final_check(self):
        result = self.lane(FAKE_AGENT="no-change",
                           FAKE_SUMMARY="nothing to change: the finding no longer holds")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), self.lane_log())
        self.assertNotIn("final", self.phases(), self.lane_log())

    def test_a_run_with_a_commit_is_still_checked_and_parked_when_the_check_fails(self):
        result = self.lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        finals = [r for r in self.records() if r["phase"] == "final"]
        self.assertTrue(finals, self.lane_log())
        self.assertFalse(finals[-1]["ok"])
        self.assertIn("validation failed: the check exited 1", self.failure_line())


for _name in dir(base.LaneRunnerTest):
    if _name.startswith("test_"):
        setattr(NoCommitSkipsFinalCheckTest, _name, None)


if __name__ == "__main__":
    unittest.main()
