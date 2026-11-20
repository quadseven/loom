"""scratch_files drops unreferenced top-level agent summary files."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bus.scope_check import scratch_files


class TestScopeCheckSummaryScratch(unittest.TestCase):
    def setUp(self):
        self.tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tree)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.tree, capture_output=True,
                              text=True, check=True).stdout.strip()

    def _commit(self, files):
        for path, text in files.items():
            full = os.path.join(self.tree, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(text)
        self._git("add", "-A")
        self._git("commit", "-qm", "c")
        return self._git("rev-parse", "HEAD")

    def test_unreferenced_top_level_summaries_are_scratch(self):
        base = self._commit({"app.py": "x = 1\n"})
        head = self._commit({
            "FIX_SUMMARY.md": "done\n",
            "deployment_changes_summary.txt": "done\n",
            ".loom-summary.md": "done\n",
        })
        self.assertEqual(
            sorted(scratch_files(self.tree, base, head, "Paths: app.py")),
            [".loom-summary.md", "FIX_SUMMARY.md", "deployment_changes_summary.txt"])

    def test_referenced_or_nested_summaries_are_kept(self):
        base = self._commit({"app.py": "x = 1\n"})
        head = self._commit({
            "app.py": "x = 1  # see RUN_SUMMARY.md\n",
            "RUN_SUMMARY.md": "done\n",
            "docs/summary.md": "done\n",
        })
        self.assertEqual(scratch_files(self.tree, base, head, "Paths: app.py"), [])


if __name__ == "__main__":
    unittest.main()
