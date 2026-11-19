"""bus/brain_trees.py process_cwds: when lsof is missing or fails, the
/proc/<pid>/cwd links list process working directories, so the brain-tree
prune still works on a Linux runner with no lsof installed."""

import os
import tempfile
import unittest
from pathlib import Path

from bus import brain_trees


class ProcessCwdsProcFallbackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.missing_lsof = str(self.tmp / "no-lsof-here")

    def fake_proc(self, entries):
        """A fake proc directory: entries maps a pid name to the cwd target
        its cwd link names, or None for an entry whose link cannot be read."""
        proc = self.tmp / "proc"
        proc.mkdir()
        for pid, target in entries.items():
            pid_dir = proc / pid
            pid_dir.mkdir()
            if target is not None:
                (pid_dir / "cwd").symlink_to(target)
        return str(proc)

    def test_proc_fallback_lists_both_cwds_when_lsof_is_missing(self):
        first = self.tmp / "first"
        second = self.tmp / "second"
        first.mkdir()
        second.mkdir()
        proc = self.fake_proc({"101": first, "202": second})
        cwds = brain_trees.process_cwds(lsof=self.missing_lsof, proc_dir=proc)
        self.assertIsNotNone(cwds)
        self.assertEqual(sorted(cwds),
                         sorted([os.path.realpath(first), os.path.realpath(second)]))

    def test_proc_fallback_skips_unreadable_and_none_when_no_source_lists(self):
        readable = self.tmp / "readable"
        readable.mkdir()
        proc = self.fake_proc({"101": readable, "202": None})
        cwds = brain_trees.process_cwds(lsof=self.missing_lsof, proc_dir=proc)
        self.assertEqual(cwds, [os.path.realpath(readable)])
        missing_proc = str(self.tmp / "no-proc-here")
        self.assertIsNone(brain_trees.process_cwds(lsof=self.missing_lsof,
                                                   proc_dir=missing_proc))


if __name__ == "__main__":
    unittest.main()
