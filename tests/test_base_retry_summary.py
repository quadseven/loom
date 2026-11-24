"""The base-test retry turn keeps the runner's summary file out of its commit."""
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
mkdir -p "$TREE" "$WORKDIR"; : > "$LOG"; printf 'Build it.\n' > "$RUN_BRIEF"
safe_git() { git -C "$TREE" "$@"; }
commit_msg() { printf '%s' "$1"; }
git init -q "$TREE"
safe_git config user.email t@example.com; safe_git config user.name t
echo one > "$TREE/a.txt"; safe_git add -A; safe_git commit -qm base
before="$(safe_git rev-parse HEAD)"
run_agent() {
  [ "$EDIT" != yes ] || echo two > "$TREE/a.txt"
  mkdir -p "$TREE/.loom"; printf 'Risk: door stated\n' > "$TREE/.loom/summary.md"
  printf '[]' > "$TREE/.loom/replies.json"
}
if [ "$FAIL_COPY" = yes ]; then
  cp() { echo 'could not copy summary' >&2; return 1; }
fi
rc=0
base_retry_turn "passes on base: tests.t.A.test_one" || rc=$?
printf '%s\n' "$rc" > "$ROOT/rc"
after="$(safe_git rev-parse HEAD)"
[ "$before" = "$after" ] && echo same > "$ROOT/head" || echo moved > "$ROOT/head"
safe_git show --name-only --format= HEAD > "$ROOT/files"
safe_git status --porcelain > "$ROOT/status"
'''


def run_turn(edit, fail_copy=False):
    match = re.search(r"^base_retry_turn\(\) \{.*?^\}\n", SOURCE, re.S | re.M)
    assert match
    with tempfile.TemporaryDirectory() as temp:
        proc = subprocess.run(
            ["/bin/bash", "-c", match.group(0) + SCRIPT],
            env={"PATH": "/usr/bin:/bin", "ROOT": temp, "EDIT": edit,
                 "FAIL_COPY": "yes" if fail_copy else "no"},
            capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        root = Path(temp)
        summary = root / "work/t1.summary.md"
        source = root / "tree/.loom/summary.md"
        return ((root / "head").read_text().strip(), (root / "files").read_text().split(),
                summary.read_text() if summary.exists() else None,
                (root / "status").read_text(), int((root / "rc").read_text()),
                source.read_text() if source.exists() else None,
                (root / "run.log").read_text())


class BaseRetrySummaryTest(unittest.TestCase):
    def test_the_commit_leaves_out_the_summary_and_the_text_is_kept(self):
        head, files, summary, status, rc, *_ = run_turn("yes")
        self.assertEqual(rc, 0)
        self.assertEqual(head, "moved")
        self.assertEqual(files, ["a.txt"])
        self.assertEqual(summary, "Risk: door stated\n")
        self.assertEqual(status, "")

    def test_a_turn_that_writes_only_the_summary_makes_no_commit(self):
        head, _, summary, status, rc, *_ = run_turn("no")
        self.assertEqual(rc, 0)
        self.assertEqual(head, "same")
        self.assertEqual(summary, "Risk: door stated\n")
        self.assertEqual(status, "")

    def test_summary_copy_failure_preserves_source_and_aborts_commit(self):
        head, _, _, _, rc, source, log = run_turn("yes", fail_copy=True)
        self.assertNotEqual(rc, 0)
        self.assertEqual(head, "same")
        self.assertEqual(source, "Risk: door stated\n")
        self.assertIn("could not copy summary", log)


if __name__ == "__main__":
    unittest.main()
