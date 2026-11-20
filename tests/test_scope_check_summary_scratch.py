"""Agent summary scratch rules on real Git commits."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from bus.scope_check import scratch_files


class SummaryScratchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tree = Path(temp.name)
        self.git("init", "-q")
        self.write("README.md", "Project readme\n")
        self.base = self.commit("base")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=self.tree, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, name, content="Run notes\n"):
        target = self.tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def test_unreferenced_top_level_summaries_are_scratch(self):
        names = ("FIX_SUMMARY.md", "deployment_changes_summary.txt",
                 ".loom-summary.md", "Run_Summary.TXT", ".LOOM-SUMMARY")
        for name in names:
            self.write(name)
        head = self.commit("add run summaries")
        self.assertCountEqual(scratch_files(str(self.tree), self.base, head, ""), names)

    def test_referenced_and_nested_summaries_are_kept(self):
        self.write("README.md", "See PROJECT_SUMMARY.md and .loom-summary.patch for details.\n")
        self.write("PROJECT_SUMMARY.md")
        self.write(".loom-summary.patch")
        self.write("docs/summary.md")
        self.write(".github/workflows/summary.md")
        head = self.commit("add project documents")
        self.assertEqual(scratch_files(str(self.tree), self.base, head, ""), [])
