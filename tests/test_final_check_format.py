"""A lane whose only defect is ruff formatting is formatted before the second
final-check run, on the files its own commits changed and no others."""

import subprocess
import unittest
from pathlib import Path

from tests import test_run_lane as base

# A stand-in for ruff: fix.txt is badly formatted until it holds a line
# `formatted`, once the lane has written `fixed` to it; the file named
# in place of EXTRA is also reported then.
FAKE_RUFF = """#!/bin/sh
[ "$1" = format ] || exit 2
shift
if [ "$1" = --check ]; then
  bad=0
  grep -qx fixed fix.txt 2> /dev/null || exit 0
  if ! grep -qx formatted fix.txt; then echo "Would reformat: fix.txt"; bad=1; fi
  if [ -n "EXTRA" ]; then echo "Would reformat: EXTRA"; bad=1; fi
  if [ "RETRY_FAILS" = yes ]; then
    if [ "$bad" -eq 0 ]; then echo "FAIL: after_format"; else echo "FAIL: before_format"; fi
    bad=1
  fi
  exit "$bad"
fi
[ "RETRY_FAILS" != yes ] || echo "FAIL: formatter_output"
for f in "$@"; do grep -qx formatted "$f" || echo formatted >> "$f"; done
"""


class FinalCheckFormatTest(base.LaneRunnerTest):
    def format_lane(self, extra="", retry_fails=False):
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        ruff = FAKE_RUFF.replace("EXTRA", extra).replace("RETRY_FAILS", "yes" if retry_fails else "no")
        self.write_tool(self.bin / "ruff", ruff)
        self.contract_repo(contract="interpreter: loom-test-python\ncheck: ruff format --check .\n")
        self.drop_brief()
        return self.run_contract_lane()

    def test_a_format_only_failure_is_formatted_and_passes_the_second_run(self):
        result = self.format_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), self.lane_log())
        self.assertIn("formatted 1 files the change touched before the second run", self.lane_log())
        finals = [r for r in self.records() if r["phase"] == "final"]
        self.assertEqual([r["exit"] for r in finals], [1, 0])
        subjects = subprocess.run(
            ["git", "-C", str(self.remote), "log", "--all", "--format=%s"],
            capture_output=True, text=True, check=True).stdout
        self.assertIn("format: apply ruff format to the lane's changed files", subjects)

    def test_retry_reports_only_failures_from_the_second_check(self):
        result = self.format_lane(retry_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        finals = [r for r in self.records() if r["phase"] == "final"]
        self.assertEqual(finals[-1]["detail"], "second run after exit 1; failing: after_format")

    def test_a_file_the_lane_did_not_change_is_never_formatted(self):
        result = self.format_lane(extra="check.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists(),
                        self.lane_log())
        self.assertNotIn("before the second run", self.lane_log())
        self.assertIn("validation failed: the check exited 1", self.lane_log())


for _name in dir(base.LaneRunnerTest):
    if _name.startswith("test_"):
        setattr(FinalCheckFormatTest, _name, None)
