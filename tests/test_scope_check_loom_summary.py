"""Tests for .loom/summary.md scratch file handling in bus/scope_check.py."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import scope_check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def git(tree, *args):
    return subprocess.run(["git", "-C", str(tree), "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class LoomSummaryScratchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self._tmp.name) / "repo"
        git(self._tmp.name, "init", "-q", "-b", "main", str(self.tree))
        self.write("README.md", "readme\n")
        self.write("bus/station.py", "x = 1\n")
        self.write(".loom/validation.conf", "check: true\n")
        self.write(".loom/sensitive-paths", "dashboard/\n")
        self.base = self.commit("base with .loom tracked")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, path, text):
        target = self.tree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git(self.tree, "add", "-A")
        git(self.tree, "commit", "-qm", message)
        return git(self.tree, "rev-parse", "HEAD")

    def scratch(self, brief="", also=()):
        return scope_check.scratch_files(str(self.tree), self.base, "HEAD", brief, also)

    def test_loom_summary_md_is_scratch_when_base_does_not_track_it(self):
        """A committed .loom/summary.md is scratch when base tracks .loom/ but not summary.md."""
        self.write(".loom/summary.md", "Risk: app-code, two-way door, blast radius fleet\n")
        self.commit("work with summary")
        found = self.scratch()
        self.assertIn(".loom/summary.md", found)

    def test_loom_summary_md_not_scratch_when_base_already_tracks_it(self):
        """A .loom/summary.md already on base is not scratch."""
        self.write(".loom/summary.md", "existing\n")
        self.base = self.commit("base with summary")
        self.write(".loom/summary.md", "changed\n")
        self.commit("work")
        found = self.scratch()
        self.assertNotIn(".loom/summary.md", found)

    def test_loom_summary_md_not_scratch_when_brief_names_it(self):
        """A .loom/summary.md named in the brief is not scratch."""
        self.write(".loom/summary.md", "Risk: app-code, two-way door, blast radius fleet\n")
        self.commit("work with summary")
        brief = "Update .loom/summary.md with the risk assessment."
        found = self.scratch(brief)
        self.assertNotIn(".loom/summary.md", found)

    def test_existing_scratch_rules_still_work(self):
        """Existing scratch rules from test_scope_check.py still apply."""
        self.write("test_debug.py", "print('scratch')\n")
        self.write("module_debug.txt", "scratch\n")
        self.write("artifact.o", "object\n")
        self.write("build_test/result.o", "object\n")
        self.write("nested/result.o", "object\n")
        self.write(".loom/test.txt", "probe\n")
        self.write(".loom/validation.conf", "check: true\n")
        self.write(".github/workflows/extra.yml", "on: push\n")
        self.commit("work")
        found = self.scratch()
        expected = [".loom/test.txt", "artifact.o",
                    "build_test/result.o", "module_debug.txt",
                    "nested/result.o", "test_debug.py"]
        self.assertEqual(sorted(found), sorted(expected))


if __name__ == "__main__":
    unittest.main()