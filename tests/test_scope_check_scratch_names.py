"""Test that scratch_files drops unreferenced top-level test/verify/check/scratch/tmp Python files."""
import sys
import unittest
import tempfile
import os
import subprocess
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bus.scope_check import stray_files, scratch_files


class TestScopeCheckScratchNames(unittest.TestCase):
    """Test that scratch_files correctly identifies unreferenced top-level Python scratch files."""

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

    def test_combined_scratch_behavior(self):
        """Test that scratch_files returns exactly the unreferenced test.py.

        Tree contains:
        - unreferenced test.py (should be in scratch)
        - verify_implementation.py referenced by tracked file (should NOT be in scratch)
        - verify.py named in brief (should NOT be in scratch)
        """
        # Base: tracked.py exists, verify.py exists (will be named in brief)
        base = self._write_and_commit(self.repo_dir, "main", {
            "tracked.py": "# tracked file\n",
            "verify.py": "# named in brief\n",
        }, "base")

        # Head: add unreferenced test.py, add verify_implementation.py and reference it in tracked.py
        head = self._write_and_commit(self.repo_dir, "main", {
            "tracked.py": "# tracked file\nimport verify_implementation\n",
            "verify.py": "# named in brief\n",
            "verify_implementation.py": "# referenced verify_implementation.py\n",
            "test.py": "# unreferenced test.py\n",
        }, "work")

        brief_text = "Paths: verify.py"
        stray = stray_files(self.repo_dir, base, head, brief_text)
        scratch = scratch_files(self.repo_dir, base, head, brief_text)

        # Only test.py should be in stray (unreferenced, not named in brief)
        self.assertEqual(sorted(stray), ["test.py"])

        # Only test.py should be in scratch (unreferenced test.py matches scratch pattern)
        self.assertEqual(scratch, ["test.py"])


if __name__ == "__main__":
    unittest.main()