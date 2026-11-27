"""A waiter must recheck its stale PID observation before removing a slot."""

from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CheckSlotReclaimTest(unittest.TestCase):
    def wait_for(self, path):
        deadline = time.monotonic() + 8
        while not path.exists():
            self.assertLess(time.monotonic(), deadline, f"missing {path.name}")
            time.sleep(0.01)

    def test_two_reapers_cannot_take_the_same_slot(self):
        self.race_reapers(second_before_rename=False)

    def test_second_reaper_waits_while_the_first_renames(self):
        self.race_reapers(second_before_rename=True)

    def race_reapers(self, *, second_before_rename):
        functions = re.search(
            r"^CHECK_SLOTS=.*?^release_check_slot\(\) \{\n.*?^\}\n",
            (ROOT / "lanes/run_lane.sh").read_text(),
            re.S | re.M,
        ).group(0)
        script = (
            r"""
LOG="$CASE/log"
kill() {
  if [ "$2" = "$STALE_PID" ] && [ ! -e "$CASE/saw-$LANE" ]; then
    touch "$CASE/saw-$LANE"
    while [ ! -e "$CASE/go-$LANE" ]; do sleep 0.01; done
  fi
  builtin kill "$@"
}
mv() {
  if [ "$PAUSE_RENAME" = 1 ] && [ "$LANE" = first ]; then
    touch "$CASE/renaming"
    while [ ! -e "$CASE/rename" ]; do sleep 0.01; done
  fi
  command mv "$@"
}
"""
            + functions
            + r"""
if try_check_slot; then
  printf 'took\n' > "$CASE/result-$LANE"
  touch "$CASE/done-$LANE"
  while [ ! -e "$CASE/release" ]; do sleep 0.01; done
  release_check_slot
else
  printf 'busy\n' > "$CASE/result-$LANE"
  touch "$CASE/done-$LANE"
fi
"""
        )
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            home = case / "home"
            slot = home / "run/check-slots/slot-1"
            slot.mkdir(parents=True)
            with subprocess.Popen(["true"]) as exited:
                exited.wait(timeout=5)
            (slot / "pid").write_text(f"{exited.pid}\n")
            env = {
                "PATH": "/usr/bin:/bin",
                "LOOM_ROOT": str(ROOT),
                "LOOM_HOME": str(home),
                "LOOM_CHECK_SLOTS": "1",
                "CASE": directory,
                "STALE_PID": str(exited.pid),
                "PAUSE_RENAME": str(int(second_before_rename)),
            }
            processes = {}
            try:
                for lane in ("first", "second"):
                    processes[lane] = subprocess.Popen(
                        ["bash", "-c", script],
                        env=dict(env, LANE=lane),
                    )
                    self.wait_for(case / f"saw-{lane}")
                (case / "go-first").touch()
                self.wait_for(
                    case / ("renaming" if second_before_rename else "done-first")
                )
                (case / "go-second").touch()
                self.wait_for(case / "done-second")
                self.assertEqual((case / "result-second").read_text(), "busy\n")
                (case / "rename").touch()
                self.wait_for(case / "done-first")
                self.assertEqual((case / "result-first").read_text(), "took\n")
                self.assertEqual(
                    int((slot / "pid").read_text()), processes["first"].pid
                )
            finally:
                for marker in ("go-first", "go-second", "rename", "release"):
                    (case / marker).touch()
                for process in processes.values():
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
