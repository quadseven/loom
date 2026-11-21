"""A remote-lane pull request's added test module runs on its base."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import factory, vacuous  # noqa: E402
import test_bus_factory as tbf  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

PASSING = ("import unittest\n\nclass T(unittest.TestCase):\n"
           "    def test_it(self):\n        self.assertTrue(True)\n")
FAILING = PASSING.replace("assertTrue(True)", "assertTrue(False)")


def git(tree, *args):
    subprocess.run(["git", "-C", tree, "-c", "user.name=t", "-c", "user.email=t@example.com",
                    *args], check=True, capture_output=True)


class RemoteLaneBaseRunTests(tbf.Base):
    def tree(self, source):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        tree = scratch.name
        git(tree, "init", "-q")
        os.makedirs(os.path.join(tree, "tests"))
        with open(os.path.join(tree, "bus.py"), "w") as stream:
            stream.write("X = 1\n")
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "base")
        base = subprocess.check_output(["git", "-C", tree, "rev-parse", "HEAD"],
                                       text=True).strip()
        with open(os.path.join(tree, "tests", "test_new_mod.py"), "w") as stream:
            stream.write(source)
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "head")
        head = subprocess.check_output(["git", "-C", tree, "rev-parse", "HEAD"],
                                       text=True).strip()
        return tree, base, head

    def run_pull(self, source, lane):
        tree, base, head = self.tree(source)
        self.gh.ticket(5, "pr", lane=lane)
        self.gh.pull(50, 5, files=("bus/a.py", "tests/test_new_mod.py"))
        self.gh.pulls[50]["base"]["sha"] = base
        self.gh.pulls[50]["head"]["sha"] = head
        self.gh.runs[head] = self.gh.runs.pop("sha50")
        self.gh.details[50]["headRefOid"] = head
        clones = []

        def clone(repo, scratch):
            clones.append(repo)
            return tree

        with mock.patch.object(factory.Factory, "base_clone", staticmethod(clone)), \
                mock.patch.object(factory.Factory, "_behind", return_value=False), \
                mock.patch.object(vacuous, "added_test_modules",
                                  return_value=["tests/test_new_mod.py"]):
            f = self.go()
        return f, clones

    def test_remote_lane_base_run_holds_a_passing_added_module(self):
        # Inside one method: the passing, failing and local-lane cases.
        f, clones = self.run_pull(PASSING, "muse")
        self.assertEqual(self.merged, [])
        self.assertIn("passes on base: tests/test_new_mod.py", f.notes)
        self.setUp()
        f, clones = self.run_pull(FAILING, "muse")
        self.assertNotIn("passes on base", getattr(f, "notes", ""))
        self.setUp()
        f, clones = self.run_pull(PASSING, "codex")
        self.assertEqual(clones, [])
        self.assertNotIn("passes on base", getattr(f, "notes", ""))


if __name__ == "__main__":
    unittest.main()
