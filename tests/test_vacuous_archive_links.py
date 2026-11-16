"""The base-test gate copes with symbolic links in the base commit."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bus import vacuous

TEST_SOURCE = ("import app, unittest\n"
               "class TestNew(unittest.TestCase):\n"
               " def test_fails_on_base(self): self.assertEqual(app.VALUE, 2)\n"
               " def test_passes_on_base(self): self.assertEqual(app.VALUE, 1)\n")


def _commit_all(root, message):
    subprocess.run(["git", "-C", root, "add", "."], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", root, "-c", "user.name=Test", "-c",
                    "user.email=test@example.com", "commit", "-qm", message],
                   check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"],
                                   text=True).strip()


def _build_repo(root, link_target):
    """Commit a base holding a symlink to link_target, then a changed test."""
    repo = Path(root)
    subprocess.run(["git", "-C", root, "init", "-q"], check=True,
                   capture_output=True)
    (repo / "app.py").write_text("VALUE = 1\n")
    (repo / "data").mkdir()
    (repo / "data" / "file.txt").write_text("content\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "link").symlink_to(link_target)
    base = _commit_all(root, "base")
    (repo / "app.py").write_text("VALUE = 2\n")
    (repo / "tests" / "test_new.py").write_text(TEST_SOURCE)
    return base, _commit_all(root, "head")


class ArchiveLinksTests(unittest.TestCase):
    def test_internal_symlink_in_base_does_not_crash_the_gate(self):
        with tempfile.TemporaryDirectory() as root:
            base, head = _build_repo(root, "data/file.txt")
            result = vacuous.git_base_test_results(root, base, head, timeout=20)
            self.assertEqual(result.passed_on_base,
                             ["tests.test_new.TestNew.test_passes_on_base"])
            self.assertEqual(result.failed_on_base,
                             ["tests.test_new.TestNew.test_fails_on_base"])

    def test_internal_symlink_is_recreated_as_a_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            base, _ = _build_repo(root, "data/file.txt")
            checkout = os.path.join(root, "checkout")
            os.mkdir(checkout)
            vacuous._archive(checkout, root, base)
            link = os.path.join(checkout, "link")
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.readlink(link), "data/file.txt")

    def test_symlink_leaving_the_checkout_is_skipped_without_raising(self):
        with tempfile.TemporaryDirectory() as root:
            base, head = _build_repo(root, "../../outside")
            checkout = os.path.join(root, "checkout")
            os.mkdir(checkout)
            vacuous._archive(checkout, root, base)
            self.assertFalse(os.path.lexists(os.path.join(checkout, "link")))
            result = vacuous.git_base_test_results(root, base, head, timeout=20)
            self.assertEqual(result.passed_on_base,
                             ["tests.test_new.TestNew.test_passes_on_base"])

    def test_nested_symlink_keeps_its_own_relative_text(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(["git", "-C", root, "init", "-q"], check=True,
                           capture_output=True)
            (repo / "data").mkdir()
            (repo / "data" / "file.txt").write_text("content\n")
            (repo / "pkg").mkdir()
            (repo / "pkg" / "link").symlink_to("../data/file.txt")
            base = _commit_all(root, "base")
            checkout = os.path.join(root, "checkout")
            os.mkdir(checkout)
            vacuous._archive(checkout, root, base)
            link = os.path.join(checkout, "pkg", "link")
            self.assertEqual(os.readlink(link), "../data/file.txt")
            with open(link) as handle:
                self.assertEqual(handle.read(), "content\n")

    def test_symlink_into_a_sibling_with_a_shared_prefix_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(["git", "-C", root, "init", "-q"], check=True,
                           capture_output=True)
            (repo / "link").symlink_to("../checkout-other/file.txt")
            base = _commit_all(root, "base")
            checkout = os.path.join(root, "checkout")
            os.mkdir(checkout)
            vacuous._archive(checkout, root, base)
            self.assertFalse(os.path.lexists(os.path.join(checkout, "link")))


if __name__ == "__main__":
    unittest.main()
