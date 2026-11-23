"""A base-test refusal gives the agent one more turn in the same run."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "lanes/run_lane.sh").read_text()

SCRIPT = r'''
set -u
LOG="$ROOT/run.log"; : > "$LOG"
BRANCH=b; BASE=base; TREE="$ROOT"; LOOM_ROOT="$SOURCE_ROOT"; BASELINE_EXIT=0
C_CHECK=fakecheck; C_AFFECTED=""; CHECK_TIMEOUT=5; CHECK_SLOT_WAIT=5; LOG_START=0
PARK_CLASS=""; HOST_RED_BASE=""; WORKDIR="$ROOT"; TICKET=t1; TITLE=t
RUN_BRIEF="$ROOT/brief"; printf 'Build the thing.\n' > "$RUN_BRIEF"
BASE_RETRY_USED=""; SUMMARY_FILE=.loom/summary.md
safe_git() { case "$1" in rev-parse) printf 0123456789abcdef ;; status) cat "$ROOT/dirty" 2> /dev/null ;;
  commit) : > "$ROOT/dirty"; echo commit >> "$ROOT/events" ;; esac; return 0; }
remove_untracked_venvs() { return 0; }
tracked_changes() { return 0; }
validation_interpreter_denied() { return 1; }
note_factory_incident() { return 0; }
drop_check_home() { return 0; }
failing_summary() { return 0; }
assess_validation_paths() { return 0; }
python3() { return 1; }
record() { return 0; }
contract_run() { return 0; }
# The gate refuses until the agent's extra turn has fixed the tests.
final_base_tests() {
  if [ -e "$ROOT/fixed" ]; then return 0; fi
  VALIDATION_REASON="validation failed: a changed test passed on base: passes on base: tests.t.A.test_one;passes on base: tests.t.A.test_two"
  return 1
}
run_agent() {
  echo turn >> "$ROOT/events"
  cp "$1" "$ROOT/last-brief"
  echo dirty > "$ROOT/dirty"
  [ "$FIXES" != yes ] || : > "$ROOT/fixed"
}
final_validation; rc=$?
printf '%s' "$VALIDATION_REASON" > "$ROOT/reason"; printf '%s' "$rc" > "$ROOT/rc"
'''


def run_final(fixes, gate_passes=False):
    functions = ""
    for name in ["base_retry_turn", "join_detail", "final_validation"]:
        match = re.search(r"^%s\(\) \{.*?^\}\n" % name, SOURCE, re.S | re.M)
        assert match, name
        functions += match.group(0)
    script = SCRIPT
    if gate_passes:
        script = script.replace('if [ -e "$ROOT/fixed" ]; then return 0; fi',
                                'return 0')
    with tempfile.TemporaryDirectory() as temp:
        proc = subprocess.run(
            ["/bin/bash", "-c", functions + script],
            env={"PATH": "/usr/bin:/bin", "ROOT": temp, "SOURCE_ROOT": str(ROOT),
                 "FIXES": fixes},
            capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, proc.stderr
        root = Path(temp)
        events = (root / "events").read_text().split() if (root / "events").exists() else []
        brief = (root / "last-brief").read_text() if (root / "last-brief").exists() else ""
        return ((root / "rc").read_text(), (root / "reason").read_text(), events, brief)


class BaseTestRetryTurnTest(unittest.TestCase):
    def test_a_turn_that_fixes_the_tests_passes_the_run(self):
        rc, reason, events, brief = run_final("yes")
        self.assertEqual(rc, "0")
        self.assertEqual(events, ["turn", "commit"])
        self.assertIn("- tests.t.A.test_one", brief)
        self.assertIn("- tests.t.A.test_two", brief)
        self.assertIn("Build the thing.", brief)

    def test_a_second_refusal_ends_the_run_with_the_same_reason(self):
        rc, reason, events, _ = run_final("no")
        self.assertEqual(rc, "1")
        self.assertEqual(events.count("turn"), 1)
        self.assertTrue(reason.startswith("validation failed: a changed test passed on base"))
        self.assertIn("passes on base: tests.t.A.test_one", reason)

    def test_a_gate_that_passes_first_time_takes_no_extra_turn(self):
        rc, _, events, _ = run_final("yes", gate_passes=True)
        self.assertEqual(rc, "0")
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
