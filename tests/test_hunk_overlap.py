"""Hunk-overlap spans for behind pull requests (loom#2622)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import hunk_overlap


class HunkSpans(unittest.TestCase):
    def test_a_hunk_span_covers_its_old_lines_context_included(self):
        self.assertEqual(hunk_overlap.hunk_spans("@@ -10,3 +10,4 @@\n a\n-b\n+c\n d"),
                         [(10, 12)])

    def test_a_hunk_with_no_count_covers_one_line(self):
        self.assertEqual(hunk_overlap.hunk_spans("@@ -7 +7 @@\n-a\n+b"), [(7, 7)])

    def test_every_hunk_in_a_patch_is_read(self):
        patch = "@@ -1,2 +1,2 @@\n-a\n+b\n@@ -40,5 +40,5 @@\n c"
        self.assertEqual(hunk_overlap.hunk_spans(patch), [(1, 2), (40, 44)])

    def test_spans_overlap_only_when_they_share_a_line(self):
        self.assertTrue(hunk_overlap.spans_overlap([(10, 12)], [(12, 20)]))
        self.assertFalse(hunk_overlap.spans_overlap([(10, 12)], [(13, 20)]))


class FilesOverlap(unittest.TestCase):
    def entry(self, name, patch, **extra):
        out = {"filename": name, "patch": patch}
        out.update(extra)
        return out

    def test_overlapping_hunks_in_the_same_file_overlap(self):
        pr = [self.entry("bus/a.py", "@@ -10,3 +10,3 @@\n-x\n+y")]
        base = [self.entry("bus/a.py", "@@ -11,2 +11,2 @@\n-x\n+y")]
        self.assertTrue(hunk_overlap.files_overlap(pr, base))

    def test_disjoint_hunks_in_the_same_file_do_not_overlap(self):
        pr = [self.entry("bus/a.py", "@@ -10,3 +10,3 @@\n-x\n+y")]
        base = [self.entry("bus/a.py", "@@ -40,3 +40,3 @@\n-x\n+y")]
        self.assertFalse(hunk_overlap.files_overlap(pr, base))

    def test_different_files_do_not_overlap(self):
        pr = [self.entry("bus/a.py", "@@ -1 +1 @@\n-x\n+y")]
        base = [self.entry("bus/b.py", "@@ -1 +1 @@\n-x\n+y")]
        self.assertFalse(hunk_overlap.files_overlap(pr, base))

    def test_a_missing_patch_on_a_shared_path_overlaps(self):
        pr = [self.entry("bus/a.py", "@@ -1 +1 @@\n-x\n+y")]
        base = [{"filename": "bus/a.py"}]
        self.assertTrue(hunk_overlap.files_overlap(pr, base))

    def test_a_rename_of_a_shared_path_overlaps(self):
        pr = [self.entry("bus/a.py", "@@ -1 +1 @@\n-x\n+y")]
        base = [self.entry("bus/b.py", "@@ -1 +1 @@\n-x\n+y",
                           status="renamed", previous_filename="bus/a.py")]
        self.assertTrue(hunk_overlap.files_overlap(pr, base))

    def test_a_compare_at_the_file_cap_overlaps(self):
        base = [self.entry("bus/f%d.py" % n, "@@ -1 +1 @@\n-x\n+y")
                for n in range(hunk_overlap.COMPARE_FILE_CAP)]
        self.assertTrue(hunk_overlap.files_overlap([], base))


if __name__ == "__main__":
    unittest.main()
