"""Tests for the scratch_files function with various Python file naming patterns."""
import sys
import unittest
import tempfile
import os
import subprocess
import shutil
from pathlib import Path

# Add the parent directory to the path so we can import bus.scope_check
sys.path.insert(0, str(Path(__file__).parent.parent))

from bus.scope_check import stray_files, scratch_files, added_files


class TestScopeCheckScratchNames(unittest.TestCase):
    """Test that scratch_files correctly identifies various Python file naming patterns."""

    def setUp(self):
        """Create a temporary git repository for testing."""
        self.test_dir = tempfile.mkdtemp()
        self.repo_dir = os.path.join(self.test_dir, "repo")
        self.base_dir = os.path.join(self.test_dir, "base")
        self.head_dir = os.path.join(self.test_dir, "head")
        os.makedirs(self.repo_dir)
        os.makedirs(self.base_dir)
        os.makedirs(self.head_dir)

        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                      cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"],
                      cwd=self.repo_dir, capture_output=True)

    def tearDown(self):
        """Clean up the temporary directory."""
        shutil.rmtree(self.test_dir)

    def _commit_and_checkout(self, tree, branch, files_dict, checkout=False):
        """Commit files to a tree and optionally checkout to a branch."""
        subprocess.run(["git", "checkout", "-b", branch], cwd=tree, capture_output=True)
        for path, content in files_dict.items():
            full_path = os.path.join(tree, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
        subprocess.run(["git", "add", "."], cwd=tree, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tree, capture_output=True)

        if checkout:
            subprocess.run(["git", "checkout", "-b", "base"], cwd=tree, capture_output=True)

    def test_unreferenced_test_py_is_scratch(self):
        """Test that test.py (unreferenced) is dropped by scratch_files."""
        subprocess.run(["git", "checkout", "-b", "base"], cwd=self.repo_dir, capture_output=True)

        # Create base commit with some tracked file that is not a .py module
        with open(os.path.join(self.repo_dir, "README.md"), "w") as f:
            f.write("# README\n# tracked file\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=self.repo_dir, capture_output=True)

        # Create new unreferenced test.py in head by creating a new commit on head
        subprocess.run(["git", "checkout", "base"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "head"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "rm", "README.md"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Remove README"], cwd=self.repo_dir, capture_output=True)

        with open(os.path.join(self.repo_dir, "test.py"), "w") as f:
            f.write("# unreferenced test.py\n")
        subprocess.run(["git", "add", "test.py"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add test.py"], cwd=self.repo_dir, capture_output=True)

        # Run the check
        brief_text = "Brief with no paths"
        stray = stray_files(self.repo_dir, "base", "head", brief_text)
        scratch = scratch_files(self.repo_dir, "base", "head", brief_text)

        # test.py should be in stray and also in scratch (since it's an unreferenced test file)
        self.assertIn("test.py", stray, "test.py should be in stray files")
        self.assertIn("test.py", scratch, "test.py should be in scratch files since it's unreferenced")

    def test_unreferenced_verify_implementation_py_is_scratch(self):
        """Test that verify_implementation.py (unreferenced) is dropped by scratch_files."""
        subprocess.run(["git", "checkout", "-b", "base"], cwd=self.repo_dir, capture_output=True)

        # Create base commit with some tracked file that is not a .py module
        with open(os.path.join(self.repo_dir, "README.md"), "w") as f:
            f.write("# README\n# tracked file\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=self.repo_dir, capture_output=True)

        # Create new unreferenced verify_implementation.py in head by creating a new commit on head
        subprocess.run(["git", "checkout", "base"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "head"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "rm", "README.md"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Remove README"], cwd=self.repo_dir, capture_output=True)

        with open(os.path.join(self.repo_dir, "verify_implementation.py"), "w") as f:
            f.write("# unreferenced verify_implementation.py\n")
        subprocess.run(["git", "add", "verify_implementation.py"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add verify_implementation.py"], cwd=self.repo_dir, capture_output=True)

        # Run the check
        brief_text = "Brief with no paths"
        stray = stray_files(self.repo_dir, "base", "head", brief_text)
        scratch = scratch_files(self.repo_dir, "base", "head", brief_text)

        # verify_implementation.py should be in stray and also in scratch (since it's an unreferenced verify file)
        self.assertIn("verify_implementation.py", stray)
        self.assertIn("verify_implementation.py", scratch)

    def test_referenced_verify_implementation_py_is_not_scratch(self):
        """Test that verify_implementation.py (referenced) is NOT dropped by scratch_files."""
        subprocess.run(["git", "checkout", "-b", "base"], cwd=self.repo_dir, capture_output=True)

        # Create base commit with some tracked file
        with open(os.path.join(self.repo_dir, "tracked.py"), "w") as f:
            f.write("# tracked file\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=self.repo_dir, capture_output=True)

        # Create new verify_implementation.py that is referenced in head
        subprocess.run(["git", "checkout", "-b", "head"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "rm", "--cached", "."], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "checkout", "--", "."], cwd=self.repo_dir, capture_output=True)

        with open(os.path.join(self.repo_dir, "verify_implementation.py"), "w") as f:
            f.write("# referenced verify_implementation.py\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True)

        # Edit tracked.py to reference verify_implementation.py
        with open(os.path.join(self.repo_dir, "tracked.py"), "a") as f:
            f.write("\nimport verify_implementation\n")
        subprocess.run(["git", "add", "tracked.py"], cwd=self.repo_dir, capture_output=True)

        # Run the check
        brief_text = "Brief with no paths"
        stray = stray_files(self.repo_dir, "base", "head", brief_text)
        scratch = scratch_files(self.repo_dir, "base", "head", brief_text)

        # verify_implementation.py should NOT be in stray (it's in scope because it's referenced)
        self.assertNotIn("verify_implementation.py", stray)
        self.assertNotIn("verify_implementation.py", scratch)

    def test_brief_names_verify_py_is_not_scratch(self):
        """Test that verify.py (named in brief paths) is NOT dropped by scratch_files."""
        subprocess.run(["git", "checkout", "-b", "base"], cwd=self.repo_dir, capture_output=True)

        # Create base commit
        with open(os.path.join(self.repo_dir, "verify.py"), "w") as f:
            f.write("# named in brief paths\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=self.repo_dir, capture_output=True)

        # Create a new verify.py that modifies the old one
        subprocess.run(["git", "checkout", "-b", "head"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "rm", "--cached", "."], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "checkout", "--", "."], cwd=self.repo_dir, capture_output=True)

        with open(os.path.join(self.repo_dir, "verify.py"), "a") as f:
            f.write("\n# additional content\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True)

        # Run the check
        brief_text = "Paths: verify.py"
        stray = stray_files(self.repo_dir, "base", "head", brief_text)
        scratch = scratch_files(self.repo_dir, "base", "head", brief_text)

        # verify.py should NOT be in stray (it's in scope because it's named in brief)
        self.assertNotIn("verify.py", stray)
        self.assertNotIn("verify.py", scratch)


if __name__ == "__main__":
    unittest.main()
