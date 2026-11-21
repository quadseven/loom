"""The base-test retry turn keeps the Intent summary out of its commit."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "lanes/run_lane.sh").read_text()

SCRIPT = r'''
set -eu
TREE="$ROOT/tree"; WORKDIR="$ROOT/work"; TICKET=t1; TITLE=t; LOG="$ROOT/run.log"
SUMMARY_FILE=.loom/summary.md; RUN_BRIEF="$ROOT/brief"
mkdir -p "$TREE/.loom" "$WORKDIR"; : > "$LOG"; echo brief > "$RUN_BRIEF"
safe_git() { git -C "$TREE" "$@"; }
git init -q "$TREE"
echo one > "$TREE/tracked.txt"
safe_git add -A
safe_git -c user.name=t -c user.email=t@example.com commit -qm base
git() { command git -c user.name=t -c user.email=t@example.com "$@"; }
run_agent() {
  [ "$EDIT" != yes ] || echo two > tracked.txt
  printf 'turn summary\n' > .loom/summary.md
}
set +e
base_retry_turn "passes on base: tests.t.A.test_one"; echo $? > "$ROOT/rc"
set -e
safe_git rev-list --count HEAD > "$ROOT/commits"
safe_git show --name-only --format= HEAD > "$ROOT/files"
'''


def run_turn(edit):
    match = re.search(r"^base_retry_turn\(\) \{.*?^\}\n", SOURCE, re.S | re.M)
    assert match
    with tempfile.TemporaryDirectory() as temp:
        proc = subprocess.run(
            ["/bin/bash", "-c", match.group(0) + SCRIPT],
            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                 "ROOT": temp, "EDIT": edit, "HOME": temp},
            capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        root = Path(temp)
        summary = root / "work/t1.summary.md"
        return (int((root / "commits").read_text()),
                (root / "files").read_text().split(),
                summary.read_text() if summary.exists() else None,
                (root / "tree/.loom/summary.md").exists())


class BaseRetrySummaryTest(unittest.TestCase):
    def test_the_commit_leaves_out_the_summary_and_the_text_is_kept(self):
        commits, files, summary, left = run_turn("yes")
        self.assertEqual(commits, 2)
        self.assertEqual(files, ["tracked.txt"])
        self.assertEqual(summary, "turn summary\n")
        self.assertFalse(left)

    def test_a_turn_that_writes_only_the_summary_makes_no_commit(self):
        commits, _, summary, left = run_turn("no")
        self.assertEqual(commits, 1)
        self.assertEqual(summary, "turn summary\n")
        self.assertFalse(left)


if __name__ == "__main__":
    unittest.main()
