"""Whole-command baseline retry evidence, without live commands."""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BaseRetryEvidenceTests(unittest.TestCase):
    def run_baseline(self, mode="flake"):
        source = (ROOT / "lanes/run_lane.sh").read_text()
        match = re.search(r"^prepare_validation\(\) \{\n.*?^\}\n", source, re.S | re.M)
        self.assertIsNotNone(match, "the real baseline entry point must be present")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "check-dir").mkdir()
            helper = re.search(r"^record_base_flake\(\) \{[^\n]*\n.*?^\}\n", source, re.S | re.M)
            script = r'''
set -u
LOG="$ROOT/run.log"
REPO=example/project
TICKET=fixture-1
LOOM_ROOT="$SOURCE_ROOT"
[ "$MODE" != write-failed ] || mkdir "$ROOT/run.base-flakes.jsonl"
CALLS="$ROOT/calls"
CHECK_HOME="$ROOT/check-home"
TREE="$ROOT"
BASE=source-head
FIX_PR=""
CONFLICTS=""
BASELINE_EXIT=0
DEFAULT_CHANGED=""
CONTRACT_FILE=.loom/validation.conf
C_SETUP=""
C_INTERP=""
C_SUBMODULES=no
HAS_CONTRACT=1
C_CHECK='cd "$ROOT/check-dir" && export CHECK_STATE=kept && passing && flaky && later'
printf 'earlier setup output\n' > "$LOG"
: > "$CALLS"
load_contract() { return 0; }
sandbox_ready() { return 0; }
sha256sum() { return 0; }
tool_runs() { return 0; }
record() { printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "${6:-}" >> "$ROOT/records"; }
base_verdict_key() { printf fixture-key; }
base_verdict_read() { return 1; }
base_verdict_write() { return 0; }
tracked_changes() {
  [ "$MODE" = mutated ] && [ -e "$ROOT/failed-once" ] && printf ' M generated.txt'
  return 0
}
resolve_factory_incidents() { printf resolved >> "$ROOT/resolved"; }
exclude_untracked() { return 0; }
safe_git() { [ "$1" != rev-parse ] || printf source-head; }
check_exit_is_load() { [ "$1" = 75 ] || [ "$1" = 124 ]; }
log_size() { wc -c < "$LOG"; }
check_failures() { printf 'tests.test_retry.Checks.test_flaky'; }
join_detail() { printf '%s' "$*"; }
repair_baseline_allowed() { return 1; }
host_red_base() { return 1; }
note_load_failure() { printf load >> "$ROOT/load"; }
note_base_check_failure() { printf '%s\n' "$1" >> "$ROOT/incidents"; }
passing() { printf 'passing\n' >> "$CALLS"; }
flaky() {
  printf 'flaky\n' >> "$CALLS"
  [ "$CHECK_STATE" = kept ] && [ "$PWD" = "$ROOT/check-dir" ] || return 9
  if [ "$MODE" = permanent ] || { [ "$MODE" != green ] && [ ! -e "$ROOT/failed-once" ]; }; then
    : > "$ROOT/failed-once"
    if [ "$MODE" = long-line ]; then
      printf '%10000s\n' x
    fi
    if [ "$MODE" = flake ]; then
      for i in {1..25}; do printf 'first-output-%02d\n' "$i"; done
    fi
    printf 'FAIL: test_flaky (tests.test_retry.Checks.test_flaky)\n'
    [ "$MODE" != other-exit ] || return 2
    return 1
  fi
  [ "$MODE" != interrupted ] || return 143
  [ "$MODE" != loaded ] || return 75
  [ "$MODE" != unavailable ] || return 125
  printf 'second-run output\n'
}
later() {
  [ "$CHECK_STATE" = kept ] && [ "$PWD" = "$ROOT/check-dir" ] || return 9
  printf 'later\n' >> "$CALLS"
}
contract_run() { ( eval "$1" ) >> "$LOG" 2>&1; }
prepare_validation
result=$?
printf '%s' "$result" > "$ROOT/result"
exit 0
'''
            functions = (helper.group(0) if helper else "") + match.group(0)
            result = subprocess.run(["/bin/bash", "-c", functions + script],
                                    env={"PATH": "/usr/bin:/bin", "ROOT": str(root),
                                         "MODE": mode, "SOURCE_ROOT": str(ROOT)},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "", "fixture must not hide shell errors")
            event_file = root / "run.base-flakes.jsonl"
            events = ([json.loads(line) for line in event_file.read_text().splitlines()]
                      if event_file.is_file() else [])
            return {"events": events, "calls": (root / "calls").read_text().splitlines(),
                    "log": (root / "run.log").read_text(),
                    "result": (root / "result").read_text(),
                    "incidents": (root / "incidents").read_text().splitlines()
                    if (root / "incidents").exists() else []}

    def test_a_flake_repeats_the_whole_command_and_runs_later_checks(self):
        run = self.run_baseline()
        self.assertEqual(run["result"], "0")
        self.assertEqual(run["calls"], ["passing", "flaky", "passing", "flaky", "later"])
        self.assertEqual(run["incidents"], [])

    def test_a_flake_log_records_the_first_exit_and_a_bounded_first_run_tail(self):
        run = self.run_baseline()
        self.assertEqual(run["log"].count("base check flaked:"), 1)
        line = "base check flaked: first exit 1; whole command passed on retry"
        self.assertIn(line, run["log"])
        summary = run["log"].split(line + "\n", 1)[1]
        self.assertTrue(summary.startswith("first-run output (last 20 lines):\n"))
        excerpt = summary.split("first-run output (last 20 lines):\n", 1)[1].splitlines()[:20]
        expected = ["first-output-%02d" % i for i in range(7, 26)]
        expected.append("FAIL: test_flaky (tests.test_retry.Checks.test_flaky)")
        self.assertEqual(excerpt, expected)
        self.assertNotIn("earlier setup output", summary)
        self.assertNotIn("second-run output", summary)

    def test_two_failures_still_file_one_incident(self):
        run = self.run_baseline("permanent")
        self.assertEqual(run["result"], "1")
        self.assertEqual(run["calls"].count("flaky"), 2)
        self.assertNotIn("later", run["calls"])
        self.assertEqual(run["incidents"], ["base-check"])
        self.assertNotIn("base check flaked:", run["log"])

    def test_green_check_keeps_shell_state_and_runs_later_checks(self):
        run = self.run_baseline("green")
        self.assertEqual(run["result"], "0")
        self.assertEqual(run["calls"], ["passing", "flaky", "later"])
        self.assertNotIn("base check flaked:", run["log"])
        self.assertEqual(run["incidents"], [])

    def test_a_green_retry_that_changes_tracked_files_is_not_a_flake_pass(self):
        run = self.run_baseline("mutated")
        self.assertEqual(run["result"], "1")
        self.assertNotIn("base check flaked:", run["log"])

    def test_unsuccessful_retries_never_log_a_flake_pass(self):
        for mode in ("interrupted", "loaded", "unavailable"):
            with self.subTest(mode=mode):
                run = self.run_baseline(mode)
                self.assertEqual(run["result"], "1")
                self.assertNotIn("base check flaked:", run["log"])

    def test_the_log_keeps_the_actual_first_exit(self):
        run = self.run_baseline("other-exit")
        self.assertEqual(run["result"], "0")
        self.assertIn("base check flaked: first exit 2;", run["log"])
