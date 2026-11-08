"""Timeouts on the base check must not become reusable red verdicts."""

import os
import unittest


class BaseCheckTimeoutTest(unittest.TestCase):
    def setUp(self):
        from tests.test_run_lane import BRIEF, LaneRunnerTest

        self.brief = BRIEF
        self.lane = LaneRunnerTest()
        self.lane.setUp()

    def tearDown(self):
        self.lane.tearDown()

    def test_repeated_timeout_checks_are_run_again_and_never_cached(self):
        contract = ("interpreter: loom-test-python\n"
                    "check: echo base-timeout-hit; exit 124\n")
        self.lane.write_tool(self.lane.bin / "sandbox-exec",
                             '#!/bin/sh\nshift 2\nexec "$@"\n')
        original_path = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"
            self.lane.contract_repo(contract=contract)
        finally:
            os.environ["PATH"] = original_path

        self.lane.drop_brief()
        first = self.lane.run_contract_lane()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse((self.lane.loom / "state" / "base-verdicts").exists())

        second_brief = self.brief.replace("T042", "T043")
        self.lane.drop_brief("T043-fix-the-thing.md", second_brief)
        second = self.lane.run_contract_lane()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse((self.lane.loom / "state" / "base-verdicts").exists())

        logs = "\n".join((self.lane.loom / "logs" / "codex" / name).read_text()
                          for name in ("T042-fix-the-thing.log",
                                       "T043-fix-the-thing.log"))
        self.assertEqual(logs.count("base-timeout-hit"), 2)
        self.assertNotIn("base check reused cached verdict", logs)


if __name__ == "__main__":
    unittest.main()
