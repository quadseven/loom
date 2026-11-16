"""Test the expanded scope check scratch names rule."""

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


class ScopeCheckScratchNamesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self._tmp.name) / "repo"
        git(self._tmp.name, "init", "-q", "-b", "main", str(self.tree))
        self.write("README.md", "readme\n")
        self.write("bus/station.py", "x = 1\n")
        self.write("tests/test_station.py", "pass\n")
        self.base = self.commit("base")

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

    def test_a_new_top_level_test_py_file_no_reference_is_dropped(self):
        """Acceptance criterion 1: test.py that no tracked file references is dropped."""
        self.write("test.py", "print('test')\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertIn("test.py", found)

    def test_a_new_top_level_verify_implementation_py_no_reference_is_dropped(self):
        """Acceptance criterion 1: verify_implementation.py that no tracked file references is dropped."""
        self.write("verify_implementation.py", "print('verify')\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertIn("verify_implementation.py", found)

    def test_a_new_top_level_verify_py_prefix_no_reference_is_dropped(self):
        """Acceptance criterion 1: verify*.py that no tracked file references is dropped."""
        self.write("verify_script.py", "print('verify')\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertIn("verify_script.py", found)

    def test_a_new_top_level_check_py_prefix_no_reference_is_dropped(self):
        """New requirement: check_*.py that no tracked file references is dropped."""
        self.write("check_validation.py", "print('check')\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertIn("check_validation.py", found)

    def test_a_new_top_level_scratch_py_prefix_no_reference_is_dropped(self):
        """New requirement: scratch*.py that no tracked file references is dropped."""
        self.write("scratch_file.py", "print('scratch')\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertIn("scratch_file.py", found)

    def test_a_new_top_level_tmp_py_prefix_no_reference_is_dropped(self):
        """New requirement: tmp*.py that no tracked file references is dropped."""
        self.write("tmp_script.py", "print('tmp')\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertIn("tmp_script.py", found)

    def test_a_new_top_level_verify_implementation_py_with_reference_is_not_dropped(self):
        """Acceptance criterion 2: verify_implementation.py that a tracked file references is not dropped."""
        self.write("verify_implementation.py", "print('verify')\n")
        self.write("README.md", "See verify_implementation.py.\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertNotIn("verify_implementation.py", found)

    def test_a_new_top_level_verify_py_prefix_with_reference_is_not_dropped(self):
        """Acceptance criterion 2: verify*.py that a tracked file references is not dropped."""
        self.write("verify_script.py", "print('verify')\n")
        self.write("README.md", "Run verify_script.py.\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertNotIn("verify_script.py", found)

    def test_a_new_top_level_check_py_prefix_with_reference_is_not_dropped(self):
        """New requirement: check_*.py that a tracked file references is not dropped."""
        self.write("check_validation.py", "print('check')\n")
        self.write("README.md", "See check_validation.py.\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertNotIn("check_validation.py", found)

    def test_a_new_top_level_scratch_py_prefix_with_reference_is_not_dropped(self):
        """New requirement: scratch*.py that a tracked file references is not dropped."""
        self.write("scratch_file.py", "print('scratch')\n")
        self.write("README.md", "See scratch_file.py.\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertNotIn("scratch_file.py", found)

    def test_a_new_top_level_tmp_py_prefix_with_reference_is_not_dropped(self):
        """New requirement: tmp*.py that a tracked file references is not dropped."""
        self.write("tmp_script.py", "print('tmp')\n")
        self.write("README.md", "See tmp_script.py.\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertNotIn("tmp_script.py", found)

    def test_a_top_level_verify_py_file_brief_names_is_not_dropped(self):
        """Acceptance criterion 3: A top-level verify.py that the brief names is not dropped."""
        self.write("verify.py", "print('verify')\n")
        self.commit("work")
        brief = "Add `verify.py` to the workspace.\n"
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", brief)
        self.assertNotIn("verify.py", found)

    def test_a_top_level_verify_script_py_file_brief_names_is_not_dropped(self):
        """Acceptance criterion 3: verify*.py that the brief names is not dropped."""
        self.write("verify_script.py", "print('verify')\n")
        self.commit("work")
        brief = "Add `verify_script.py` to the workspace.\n"
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", brief)
        self.assertNotIn("verify_script.py", found)

    def test_a_top_level_check_py_file_brief_names_is_not_dropped(self):
        """New requirement: A top-level check.py that the brief names is not dropped."""
        self.write("check.py", "print('check')\n")
        self.commit("work")
        brief = "Add `check.py` to the workspace.\n"
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", brief)
        self.assertNotIn("check.py", found)

    def test_a_top_level_scratch_py_file_brief_names_is_not_dropped(self):
        """New requirement: A top-level scratch.py that the brief names is not dropped."""
        self.write("scratch.py", "print('scratch')\n")
        self.commit("work")
        brief = "Add `scratch.py` to the workspace.\n"
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", brief)
        self.assertNotIn("scratch.py", found)

    def test_a_top_level_tmp_py_file_brief_names_is_not_dropped(self):
        """New requirement: A top-level tmp.py that the brief names is not dropped."""
        self.write("tmp.py", "print('tmp')\n")
        self.commit("work")
        brief = "Add `tmp.py` to the workspace.\n"
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", brief)
        self.assertNotIn("tmp.py", found)

    def test_a_file_in_subdirectory_is_not_considered_top_level(self):
        """New requirement: Files under subdirectories are not considered top-level."""
        self.write("subdir/test.py", "print('test')\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertNotIn("subdir/test.py", found)


if __name__ == "__main__":
    unittest.main()