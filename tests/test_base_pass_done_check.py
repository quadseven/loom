"""A run whose changed tests all pass on base is held for a done-check, not
climbed (loom#3005)."""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import factory, history  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
import test_bus_factory  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "lanes/run_lane.sh").read_text()
START = "validation failed: every changed test passes on base"
FULL = START + "; the ticket may already hold on the default branch"

SCRIPT = r'''
set -u
LOG="$ROOT/run.log"; : > "$LOG"
TREE="$ROOT"; BASE=base; LOOM_ROOT="$SOURCE_ROOT"; CHECK_HOME="$ROOT"
FIX_PR="${FIX:-}"; FIX_DEFAULT=""; FIX_UPSTREAM=""
PARK_CLASS=""; VALIDATION_REASON=""
safe_git() { return 1; }
record() { return 0; }
cp() { return 0; }
contract_run() { printf '%b' "$GATE" >> "$LOG"; return 1; }
final_base_tests abc123 || true
printf '%s' "$VALIDATION_REASON" > "$ROOT/reason"
'''


def gate_reason(output, fix=""):
    match = re.search(r"^final_base_tests\(\) \{.*?^\}\n", SOURCE, re.S | re.M)
    assert match
    with tempfile.TemporaryDirectory() as temp:
        proc = subprocess.run(
            ["/bin/bash", "-c", match.group(0) + SCRIPT],
            env={"PATH": "/usr/bin:/bin", "ROOT": temp, "SOURCE_ROOT": str(ROOT),
                 "GATE": output, "FIX": fix},
            capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, proc.stderr
        return (Path(temp) / "reason").read_text()


class GateReason(unittest.TestCase):
    def test_only_base_pass_names_give_the_done_check_reason(self):
        reason = gate_reason("base-pass: tests/test_a.py\nbase-pass: tests/test_b.py\n")
        self.assertTrue(reason.startswith(FULL), reason)
        self.assertIn("passes on base: tests/test_a.py;passes on base: tests/test_b.py", reason)

    def test_a_timeout_or_unrunnable_name_keeps_the_old_reason(self):
        for other in ("base-timeout", "base-unrunnable"):
            reason = gate_reason("base-pass: tests/test_a.py\n%s: tests/test_b.py\n" % other)
            self.assertTrue(reason.startswith("validation failed: a changed test passed"), reason)

    def test_a_fix_round_keeps_the_old_reason(self):
        reason = gate_reason("base-pass: tests/test_a.py\n", fix="12")
        self.assertTrue(reason.startswith("validation failed: a changed test passed"), reason)


class Reader(unittest.TestCase):
    def test_the_newest_failure_names_the_tests(self):
        body = "Lane zai failed: [work] %s: passes on base: tests/test_a.py" % FULL
        self.assertEqual(history.base_pass_tests([{"body": body}]),
                         "tests/test_a.py")
        self.assertIsNone(history.base_pass_tests(
            [{"body": body}, {"body": "Lane zai failed: [work] agent made no commit"}]))


class Hold(test_bus_factory.Tickets):
    def test_a_run_whose_tests_pass_on_base_is_asked_for_a_done_check_not_climbed(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.gh.comments[6] += [self.ZAI_CLAIM, {"body": (
            "Lane zai failed: [work] %s: passes on base: tests/test_a.py" % FULL),
            "user": test_bus_factory.ME}]
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))
        self.assertIn({"name": "needs-brain"}, self.gh.issues[6]["labels"])
        note = self.gh.comments[6][-1]["body"]
        self.assertIn("tests/test_a.py", note)
        self.assertIn("is done", note)
        self.assertNotIn(factory.RUNG_MARKER % 1, note)
        posted = len(self.gh.comments[6])
        self.go()
        self.assertEqual((self.moved, len(self.gh.comments[6])), ([], posted))


for _name in dir(test_bus_factory.Tickets):
    if _name.startswith("test_"):
        setattr(Hold, _name, None)

if __name__ == "__main__":
    unittest.main()
