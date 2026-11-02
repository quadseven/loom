"""bus/brain_trees.py: the brain's roll trees in $LOOM_HOME/brain/work are
retired once their work landed, kept and named otherwise, and at most KEEP
idle trees with unpushed commits are kept (loom#2438). Real git repos in a
temp folder; the remote is a local bare repo."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from bus import brain_trees
from tests import sandbox_caps

DAY = 86400


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def age(path, seconds):
    """Set every mtime in the tree to seconds ago."""
    when = time.time() - seconds
    for top, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(top, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when))


class BrainTreesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.tmp)
        seed = self.tmp / "seed"
        git("clone", "-q", str(self.remote), str(seed), cwd=self.tmp)
        (seed / "README").write_text("seed\n")
        git("add", ".", cwd=seed)
        git("commit", "-qm", "seed", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.seed = seed
        self.home = self.tmp / "loom"
        self.work = self.home / "brain" / "work"
        self.work.mkdir(parents=True)
        self.lines = []

    def tree(self, name, commit=None, hours=48.0):
        """A clone in brain/work, with a commit of file name -> text when
        commit is given, last changed hours ago."""
        path = self.work / name
        # --no-local: hard-linked objects would share one mtime across trees.
        git("clone", "-q", "--no-local", str(self.remote), str(path), cwd=self.tmp)
        if commit:
            (path / commit[0]).write_text(commit[1])
            git("add", ".", cwd=path)
            git("commit", "-qm", "roll " + name, cwd=path)
        age(path, hours * 3600)
        return path

    def land(self, name, text):
        """Put file name -> text on the remote's main in a commit of its own,
        as a squash merge does."""
        git("pull", "-q", "origin", "main", cwd=self.seed)
        (self.seed / name).write_text(text)
        git("add", ".", cwd=self.seed)
        git("commit", "-qm", "squash " + name, cwd=self.seed)
        git("push", "-q", "origin", "HEAD:main", cwd=self.seed)

    def prune(self, keep=brain_trees.KEEP, cwds=None):
        pruner = brain_trees.Pruner(
            work=str(self.work), retired=str(self.home / "brain" / "retired"), now=time.time(),
            keep=keep, say=self.lines.append, cwds=[] if cwds is None else cwds)
        self.assertEqual(pruner.run(), 0)
        return "\n".join(self.lines)

    def test_a_tree_whose_head_is_on_main_is_removed(self):
        self.tree("roll-merged")
        out = self.prune()
        self.assertFalse((self.work / "roll-merged").exists())
        self.assertIn("remove roll-merged reason=merged", out)

    def test_a_tree_whose_commit_landed_as_a_squash_merge_is_removed(self):
        self.tree("roll-squashed", commit=("pin.txt", "v2\n"))
        self.land("pin.txt", "v2\n")
        out = self.prune()
        self.assertFalse((self.work / "roll-squashed").exists(), out)
        self.assertIn("remove roll-squashed reason=squash-merged", out)

    def test_a_tree_with_a_commit_on_no_remote_is_kept_and_named(self):
        path = self.tree("roll-unpushed", commit=("pin.txt", "v3\n"))
        out = self.prune()
        self.assertTrue(path.exists())
        self.assertRegex(out, r"keep roll-unpushed reason=unpushed-commits=1 head=[0-9a-f]{12}")
        self.assertIn("keeps total=1 unpushed-commits=1", out)

    def test_a_recent_or_dirty_tree_is_kept_with_its_reason(self):
        self.tree("roll-fresh", hours=0)
        dirty = self.tree("roll-dirty")
        (dirty / "README").write_text("changed\n")
        out = self.prune()
        self.assertTrue((self.work / "roll-fresh").exists())
        self.assertTrue(dirty.exists())
        self.assertRegex(out, r"keep roll-fresh reason=active-idle-0\.\dh")
        self.assertIn("keep roll-dirty reason=dirty", out)
        self.assertIn("active-idle=1", out)
        self.assertIn("dirty=1", out)

    def test_a_tree_some_process_works_in_is_kept(self):
        path = self.tree("roll-in-use")
        out = self.prune(cwds=[os.path.realpath(path / "sub")])
        self.assertTrue(path.exists())
        self.assertIn("keep roll-in-use reason=in-use-cwd", out)

    def test_unknown_process_cwds_keep_every_tree(self):
        path = self.tree("roll-merged")
        pruner = brain_trees.Pruner(work=str(self.work), retired=str(self.home / "brain" / "retired"),
                                    now=time.time(), say=self.lines.append, cwds=None)
        pruner.run()
        self.assertTrue(path.exists())
        self.assertIn("keep roll-merged reason=in-use-cwd", self.lines)

    def test_unpushed_trees_beyond_the_keep_count_are_bundled_and_removed_newest_kept(self):
        for i, hours in enumerate((100, 80, 60, 40)):
            self.tree("roll-%d" % i, commit=("pin%d.txt" % i, "v%d\n" % i), hours=hours)
        out = self.prune(keep=2)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["roll-2", "roll-3"])
        bundles = sorted((self.home / "brain" / "retired").iterdir())
        self.assertEqual([b.name.split("-")[1] for b in bundles], ["0", "1"])
        for bundle in bundles:
            # The bundle holds the retired commit: it can be fetched back.
            self.assertIn("HEAD", git("bundle", "list-heads", str(bundle), cwd=self.tmp))
            tree = "-".join(bundle.name.split("-")[:2])
            self.assertIn("remove %s reason=over-keep-2 unpushed-commits=1" % tree, out)
            self.assertIn("bundle=%s" % bundle, out)
        self.assertIn("keep roll-3 reason=unpushed-commits=1", out)

    @unittest.skipUnless(sandbox_caps.PROCESS_TABLE, sandbox_caps.NO_PROCESS_TABLE)
    def test_an_in_use_tree_beyond_the_keep_count_is_kept(self):
        for i, hours in enumerate((100, 80)):
            self.tree("roll-%d" % i, commit=("pin%d.txt" % i, "v%d\n" % i), hours=hours)
        busy = self.work / "roll-0"
        proc = subprocess.Popen(["sleep", "30"], cwd=busy)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        cwds = brain_trees.process_cwds()
        if cwds is None:
            self.skipTest("lsof cannot list process working directories here")
        out = self.prune(keep=0, cwds=cwds)
        self.assertTrue(busy.exists(), out)
        self.assertIn("keep roll-0 reason=in-use-cwd", out)
        self.assertFalse((self.work / "roll-1").exists(), out)

    def test_a_pushed_tree_goes_once_stale(self):
        path = self.tree("roll-pushed", commit=("pin.txt", "v4\n"), hours=24 * 4)
        git("push", "-q", "origin", "HEAD:refs/heads/side", cwd=path)
        age(path, 4 * DAY)
        out = self.prune()
        self.assertFalse(path.exists(), out)
        self.assertIn("remove roll-pushed reason=stale-4d-pushed", out)

    def test_a_main_checkout_holding_linked_worktrees_is_kept_and_its_linked_tree_judged(self):
        main = self.tree("infra")
        git("worktree", "add", "-q", "--detach", str(self.work / "roll-linked"), "origin/main", cwd=main)
        age(main, 48 * 3600)
        age(self.work / "roll-linked", 48 * 3600)
        out = self.prune()
        self.assertIn("keep infra reason=holds-linked-worktrees", out)
        self.assertIn("remove roll-linked reason=merged", out)
        self.assertFalse((self.work / "roll-linked").exists())
        self.assertNotIn("roll-linked", git("worktree", "list", cwd=main))

    def test_a_link_or_a_plain_folder_is_kept_untouched(self):
        (self.work / "notes").mkdir()
        (self.work / "link").symlink_to(self.tmp)
        out = self.prune()
        self.assertIn("keep notes reason=not-a-git-tree", out)
        self.assertIn("keep link reason=path-is-symlink", out)
        self.assertTrue((self.work / "notes").exists())
        self.assertTrue(self.tmp.exists())

    def test_a_missing_work_folder_is_no_error(self):
        self.work.rmdir()
        out = self.prune()
        self.assertIn("keeps total=0", out)

    def test_old_bundles_go(self):
        retired = self.home / "brain" / "retired"
        retired.mkdir(parents=True)
        old, new = retired / "roll-a-0.bundle", retired / "roll-b-0.bundle"
        old.write_text("x")
        new.write_text("y")
        age_s = (brain_trees.RETIRED_DAYS + 1) * DAY
        os.utime(old, (time.time() - age_s, time.time() - age_s))
        self.prune()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_the_command_line_prunes_under_loom_home(self):
        self.tree("roll-merged")
        root = Path(brain_trees.__file__).resolve().parent.parent
        done = subprocess.run(["python3", "-B", "-m", "bus.brain_trees", "prune", "--home", str(self.home)],
                              cwd=root, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("brain-trees: remove roll-merged reason=merged", done.stdout)
        self.assertIn("brain-trees: keeps total=0", done.stdout)


if __name__ == "__main__":
    unittest.main()
