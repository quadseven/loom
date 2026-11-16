"""Tests for _archive function handling symlinks."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bus import vacuous


class ArchiveLinksTests(unittest.TestCase):
    def test_symlink_with_target_inside_repo_is_recreated(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)

            def git(*args):
                return subprocess.run(["git", "-C", root, *args], check=True,
                                      capture_output=True).stdout

            git("init", "-q")
            (repo / "app.py").write_text("VALUE = 1\\n")
            (repo / "data").mkdir()
            (repo / "data" / "file.txt").write_text("content\\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
            base = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()

            (repo / "link_to_file").symlink_to("data/file.txt")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "head")
            head = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()

            checkout = os.path.join(root, "checkout")
            os.mkdir(checkout)
            vacuous._archive(checkout, root, head)

            link_path = os.path.join(checkout, "link_to_file")
            self.assertTrue(os.path.exists(link_path))
            self.assertTrue(os.path.islink(link_path))
            self.assertEqual(os.readlink(link_path), "data/file.txt")

    def test_symlink_outside_checkout_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)

            def git(*args):
                return subprocess.run(["git", "-C", root, *args], check=True,
                                      capture_output=True).stdout

            git("init", "-q")
            (repo / "app.py").write_text("VALUE = 1\\n")
            (repo / "data").mkdir()
            (repo / "data" / "file.txt").write_text("content\\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
            base = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()

            (repo / "link_to_file").symlink_to("../../outside/file.txt")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "head")
            head = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()

            checkout = os.path.join(root, "checkout")
            os.mkdir(checkout)
            vacuous._archive(checkout, root, head)

            link_path = os.path.join(checkout, "link_to_file")
            self.assertFalse(os.path.exists(link_path))