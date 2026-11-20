"""The final check's failure reason names the failing tests (loom#3198)."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "lanes/run_lane.sh").read_text()
NAMES = ["tests.test_a.A.test_%d" % n for n in range(1, 6)]

SCRIPT = r'''
set -u
LOG="$ROOT/run.log"; : > "$LOG"
BRANCH=b; BASE=base; TREE="$ROOT"; LOOM_ROOT="$SOURCE_ROOT"; BASELINE_EXIT=0
C_CHECK=fakecheck; C_AFFECTED=""; CHECK_TIMEOUT=5; CHECK_SLOT_WAIT=5; LOG_START=0
PARK_CLASS=""; HOST_RED_BASE=""
safe_git() { [ "$1" != rev-parse ] || printf 0123456789abcdef; }
remove_untracked_venvs() { return 0; }
tracked_changes() { return 0; }
final_base_tests() { return 0; }
validation_interpreter_denied() { return 1; }
note_factory_incident() { return 0; }
drop_check_home() { return 0; }
assess_validation_paths() { return 0; }
python3() { return 1; }
record() { printf '%s|%s\n' "$1" "${6:-}" >> "$ROOT/records"; }
contract_run() {
  local n
  for n in $FAILS; do printf 'FAIL: test_x (tests.test_a.A.test_%s)\n' "$n" >> "$LOG"; done
  return "$EXIT"
}
final_validation
printf '%s' "$VALIDATION_REASON" > "$ROOT/reason"
'''


def run_final(fails, exit_code):
    names = ["check_failure_names", "failing_summary", "join_detail", "final_validation"]
    functions = ""
    for name in names:
        match = re.search(r"^%s\(\) \{.*?^\}\n" % name, SOURCE, re.S | re.M)
        assert match, name
        functions += match.group(0)
    with tempfile.TemporaryDirectory() as temp:
        proc = subprocess.run(
            ["/bin/bash", "-c", functions + SCRIPT],
            env={"PATH": "/usr/bin:/bin", "ROOT": temp, "SOURCE_ROOT": str(ROOT),
                 "FAILS": " ".join(str(n) for n in fails), "EXIT": str(exit_code)},
            capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, proc.stderr
        records = (Path(temp) / "records").read_text().splitlines()
        return (Path(temp) / "reason").read_text(), records


class FinalCheckFailureNamesTest(unittest.TestCase):
    def test_the_reason_names_both_failing_tests(self):
        reason, _ = run_final([1, 2], 1)
        self.assertIn("(failing: %s, %s)" % (NAMES[0], NAMES[1]), reason)
        self.assertNotIn("\n", reason)

    def test_five_failures_list_three_and_count_the_rest(self):
        reason, records = run_final([1, 2, 3, 4, 5], 1)
        listed = "%s, %s, %s, and 2 more" % tuple(NAMES[:3])
        self.assertIn("(failing: %s)" % listed, reason)
        self.assertIn("failing: " + listed, records[-1])
        self.assertLessEqual(len(reason), 520)

    def test_load_signal_and_environment_reasons_are_unchanged(self):
        for code, text in ((124, "the check timed out after 5s"),
                           (75, "no check slot free within 5s"),
                           (143, "validation interrupted by SIGTERM"),
                           (126, "validation environment unavailable: the check exited 126")):
            reason, _ = run_final([1], code)
            self.assertIn(text, reason)
            self.assertNotIn("failing:", reason)


if __name__ == "__main__":
    unittest.main()
