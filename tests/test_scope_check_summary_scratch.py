"""Test that scratch_files drops unreferenced top-level summary files."""
import sys
import unittest
import tempfile
import os
import subprocess
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus.scope_check import stray_files, scratch_files


class TestScopeCheckSummaryScratch(unittest.TestCase):
    """Test that scratch_files correctly identifies unreferenced top-level summary files."""

    def setUp(self):
        """Create a temporary git repository for testing."""
        self.test_dir = tempfile.mkdtemp()
        self.repo_dir = os.path.join(self.test_dir, "repo")
        os.makedirs(self.repo_dir)

        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo_dir, capture_output=True)

    def tearDown(self):
        """Clean up the temporary directory."""
        shutil.rmtree(self.test_dir)

    def _write_and_commit(self, tree, branch, files_dict, message):
        """Write files and commit them on a branch."""
        subprocess.run(["git", "checkout", "-q", branch], cwd=tree, capture_output=True)
        for path, content in files_dict.items():
            full_path = os.path.join(tree, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
        subprocess.run(["git", "add", "."], cwd=tree, capture_output=True)
        subprocess.run(["git", "commit", "-qm", message], cwd=tree, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree, capture_output=True, text=True).stdout.strip()

    def test_unreferenced_top_level_summary_files_are_scratch(self):
        """Test that new unreferenced top-level summary files are returned by scratch_files.

        Tree contains:
        - unreferenced FIX_SUMMARY.md (should be in scratch)
        - unreferenced deployment_changes_summary.txt (should be in scratch)
        - unreferenced .loom-summary.md (should be in scratch)
        """
        # Base: no summary files
        base = self._write_and_commit(self.repo_dir, "main", {
            "README.md": "readme\n",
        }, "base")

        # Head: add summary files
        head = self._write_and_commit(self.repo_dir, "main", {
            "README.md": "readme\n",
            "FIX_SUMMARY.md": "fixed issues\n",
            "deployment_changes_summary.txt": "deployment changes\n",
            ".loom-summary.md": "loom summary\n",
        }, "work")

        brief_text = "Nothing named.\n"
        stray = stray_files(self.repo_dir, base, head, brief_text)
        scratch = scratch_files(self.repo_dir, base, head, brief_text)

        # All three summary files should be in stray (unreferenced, not named in brief)
        self.assertEqual(sorted(stray), [".loom-summary.md", "FIX_SUMMARY.md",
                                        "deployment_changes_summary.txt"])

        # All three summary files should be in scratch (unreferenced summary files match scratch pattern)
        self.assertEqual(sorted(scratch), [".loom-summary.md", "FIX_SUMMARY.md",
                                          "deployment_changes_summary.txt"])

    def test_referenced_top_level_summary_file_is_kept(self):
        """Test that a top-level summary file a tracked file references is not returned as scratch.

        Tree contains:
        - tracked.py references summary_file.md (should NOT be in scratch)
        - summary_file.md is in scratch pattern (should NOT be in scratch due to reference)
        """
        # Base: tracked.py exists
        base = self._write_and_commit(self.repo_dir, "main", {
            "tracked.py": "# tracked file\n",
        }, "base")

        # Head: add summary file and reference it
        head = self._write_and_commit(self.repo_dir, "main", {
            "tracked.py": "# tracked file\nimport summary_file\n",
            "summary_file.md": "summary data\n",
        }, "work")

        brief_text = "Nothing named.\n"
        stray = stray_files(self.repo_dir, base, head, brief_text)
        scratch = scratch_files(self.repo_dir, base, head, brief_text)

        # summary_file.md should be in stray (unreferenced, not named in brief)
        self.assertEqual(sorted(stray), ["summary_file.md"])

        # summary_file.md should NOT be in scratch (referenced by tracked file)
        self.assertEqual(scratch, [])

    def test_subdirectory_summary_file_is_not_scratch(self):
        """Test that a summary file inside a subdirectory is not returned as scratch.

        Tree contains:
        - docs/summary.md (should NOT be in scratch - in subdirectory)
        """
        # Base: no summary files
        base = self._write_and_commit(self.repo_dir, "main", {
            "README.md": "readme\n",
        }, "base")

        # Head: add subdirectory summary file
        head = self._write_and_commit(self.repo_dir, "main", {
            "README.md": "readme\n",
            "docs/summary.md": "documentation summary\n",
        }, "work")

        brief_text = "Nothing named.\n"
        stray = stray_files(self.repo_dir, base, head, brief_text)
        scratch = scratch_files(self.repo_dir, base, head, brief_text)

        # docs/summary.md should be in stray (unreferenced, not named in brief)
        self.assertEqual(sorted(stray), ["docs/summary.md"])

        # docs/summary.md should NOT be in scratch (in subdirectory)
        self.assertEqual(scratch, [])

    def test_uppercase_summary_file_case_insensitive_match(self):
        """Test that summary file matching is case-insensitive.

        Tree contains:
        - FIX_SUMMARY.md (uppercase extension, should be in scratch)
        """
        # Base: no summary files
        base = self._write_and_commit(self.repo_dir, "main", {
            "README.md": "readme\n",
        }, "base")

        # Head: add summary file with uppercase extension
        head = self._write_and_commit(self.repo_dir, "main", {
            "README.md": "readme\n",
            "FIX_SUMMARY.md": "fixed issues\n",
        }, "work")

        brief_text = "Nothing named.\n"
        stray = stray_files(self.repo_dir, base, head, brief_text)
        scratch = scratch_files(self.repo_dir, base, head, brief_text)

        # FIX_SUMMARY.md should be in stray (unreferenced, not named in brief)
        self.assertEqual(sorted(stray), ["FIX_SUMMARY.md"])

        # FIX_SUMMARY.md should be in scratch (case-insensitive match for *summary*.md)
        self.assertEqual(sorted(scratch), ["FIX_SUMMARY.md"])

    def test_loom_summary_pattern_match(self):
        """Test that .loom-summary* pattern matches various files.

        Tree contains:
        - .loom-summary.md (should be in scratch)
        - .loom-summary.txt (should be in scratch)
        - .loom-summary-backup (should be in scratch)
        """
        # Base: no summary files
        base = self._write_and_commit(self.repo_dir, "main", {
            "README.md": "readme\n",
        }, "base")

        # Head: add various .loom-summary* files
        head = self._write_and_commit(self.repo_dir, "main", {
            "README.md": "readme\n",
            ".loom-summary.md": "loom summary md\n",
            ".loom-summary.txt": "loom summary txt\n",
            ".loom-summary-backup": "loom summary backup\n",
        }, "work")

        brief_text = "Nothing named.\n"
        stray = stray_files(self.repo_dir, base, head, brief_text)
        scratch = scratch_files(self.repo_dir, base, head, brief_text)

        # All three .loom-summary* files should be in stray (unreferenced, not named in brief)
        self.assertEqual(sorted(stray), [".loom-summary.md", ".loom-summary-backup",
                                        ".loom-summary.txt"])

        # All three .loom-summary* files should be in scratch (match .loom-summary* pattern)
        self.assertEqual(sorted(scratch), [".loom-summary.md", ".loom-summary-backup",
                                          ".loom-summary.txt"])


if __name__ == "__main__":
    unittest.main()