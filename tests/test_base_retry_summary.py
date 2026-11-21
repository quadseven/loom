"""Runner metadata stays out of commits made by the base retry turn."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "lanes/run_lane.sh").read_text()
GIT = shutil.which("git", path="/usr/bin:/bin")
FUNCTION = re.search(r"^base_retry_turn\(\) \{.*?^\}\n", SOURCE, re.S | re.M)
assert FUNCTION, "base_retry_turn not found"


def git(tree, *args):
    return subprocess.run([GIT, "-C", str(tree), "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class BaseRetrySummaryTest(unittest.TestCase):
    def run_turn(self, write_tracked):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = root / "tree"
            work = root / "work"
            tree.mkdir()
            work.mkdir()
            git(tree, "init", "-q")
            (tree / ".loom").mkdir()
            (tree / "tracked.txt").write_text("base\n")
            git(tree, "add", "-A")
            git(tree, "commit", "-qm", "base")
            (work / "brief").write_text("brief\n")
            script = r'''
TREE="$ROOT/tree" WORKDIR="$ROOT/work" TICKET=t3451
RUN_BRIEF="$ROOT/work/brief" LOG="$ROOT/log" SUMMARY_FILE=.loom/summary.md
TITLE="T3451: retry" FIX_PR=""
safe_git() { "$GIT" -C "$TREE" -c user.name=t -c user.email=t@example.invalid "$@"; }
run_agent() {
  if [ "$WRITE_TRACKED" = yes ]; then printf 'changed\n' > "$TREE/tracked.txt"; fi
  printf 'retry summary\n' > "$TREE/.loom/summary.md"
}
base_retry_turn ""
'''
            proc = subprocess.run(["/bin/bash", "-c", FUNCTION.group(0) + script],
                                  env={"PATH": "/usr/bin:/bin", "GIT": GIT, "ROOT": str(root),
                                       "WRITE_TRACKED": "yes" if write_tracked else "no"},
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            files = git(tree, "show", "--pretty=", "--name-only", "HEAD").splitlines()
            count = git(tree, "rev-list", "--count", "HEAD")
            summary = (work / "t3451.summary.md").read_text() if (work / "t3451.summary.md").exists() else None
            summary_in_tree = (tree / ".loom/summary.md").exists()
            return files, count, summary, summary_in_tree

    def test_summary_is_saved_as_runner_metadata_when_turn_commits_code(self):
        files, _, summary, _ = self.run_turn(True)
        self.assertNotIn(".loom/summary.md", files)
        self.assertEqual(summary, "retry summary\n")

    def test_summary_only_turn_makes_no_commit(self):
        _, count, _, summary_in_tree = self.run_turn(False)
        self.assertEqual(count, "1")
        self.assertFalse(summary_in_tree)


if __name__ == "__main__":
    unittest.main()
