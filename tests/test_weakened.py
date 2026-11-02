"""bus/weakened.py: a lane pull request that weakens its own tests is
knowable from the diff alone, before any review round.

The review bot's weakened-test send-backs rose from none to 18 in a week
(loom#2162): assertions deleted, tests skipped, equality checks loosened
to truthiness. Each cost a full review round-trip to discover. The scan
reads the same file entries the station already reads for a pull request
and names the lines; what it cannot read (a patch GitHub withheld) it
leaves to the review, and a change that moves or rewords an assertion
without weakening it is not a finding.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import weakened  # noqa: E402


def entry(path, patch, **kw):
    found = {"filename": path, "patch": patch}
    found.update(kw)
    return found


class WeakenedTestScanTest(unittest.TestCase):
    def test_a_deleted_assertion_is_named_with_its_line(self):
        patch = ("@@ -10,4 +10,3 @@ def test_total(self):\n"
                 "         kept = 1\n"
                 "-        self.assertEqual(total([1, 2]), 3)\n"
                 "         self.assertTrue(done)\n")
        found = weakened.find([entry("tests/test_cart.py", patch)])
        self.assertEqual(1, len(found))
        self.assertIn("tests/test_cart.py:11", found[0])
        self.assertIn("deleted", found[0])
        self.assertIn("self.assertEqual(total([1, 2]), 3)", found[0])

    def test_an_assertion_moved_within_the_file_is_not_deleted(self):
        patch = ("@@ -3,3 +3,2 @@\n"
                 "-        self.assertEqual(total([1, 2]), 3)\n"
                 "         pass\n"
                 "@@ -20,2 +19,3 @@\n"
                 "         def helper(self):\n"
                 "+            self.assertEqual(total([1, 2]), 3)\n")
        self.assertEqual([], weakened.find([entry("tests/test_cart.py", patch)]))

    def test_an_added_skip_is_named(self):
        patch = ("@@ -5,3 +5,4 @@\n"
                 "         def test_total(self):\n"
                 "+        @pytest.mark.skip(reason=\"slow\")\n"
                 "         def test_total(self):\n")
        found = weakened.find([entry("tests/test_cart.py", patch)])
        self.assertEqual(1, len(found))
        self.assertIn("tests/test_cart.py:6", found[0])
        self.assertIn("skipped", found[0])

    def test_a_skip_that_was_already_there_is_not_new(self):
        patch = ("@@ -5,4 +5,4 @@\n"
                 "-        @pytest.mark.skip(reason=\"slow\")\n"
                 "+        @pytest.mark.skip(reason=\"slow\")\n"
                 "         def test_total(self):\n")
        self.assertEqual([], weakened.find([entry("tests/test_cart.py", patch)]))

    def test_a_loosened_assertion_is_named_once_not_as_deleted(self):
        patch = ("@@ -8,3 +8,3 @@\n"
                 "         def test_total(self):\n"
                 "-        self.assertEqual(total([1, 2]), 3)\n"
                 "+        self.assertTrue(total([1, 2]))\n")
        found = weakened.find([entry("tests/test_cart.py", patch)])
        self.assertEqual(1, len(found))
        self.assertIn("loosened", found[0])
        self.assertIn("assertEqual", found[0])
        self.assertIn("assertTrue", found[0])

    def test_a_changed_expectation_is_not_a_weakening(self):
        patch = ("@@ -8,3 +8,3 @@\n"
                 "         def test_total(self):\n"
                 "-        self.assertEqual(total([1, 2]), 3)\n"
                 "+        self.assertEqual(total([1, 2]), 4)\n")
        self.assertEqual([], weakened.find([entry("tests/test_cart.py", patch)]))

    def test_a_bare_assert_loosened_to_a_truthiness_check(self):
        patch = ("@@ -4,2 +4,2 @@\n"
                 "-    assert result == 7\n"
                 "+    assert result\n")
        found = weakened.find([entry("pkg/test_run.py", patch)])
        self.assertEqual(1, len(found))
        self.assertIn("loosened", found[0])

    def test_assertions_outside_test_files_are_not_scanned(self):
        patch = ("@@ -8,3 +8,2 @@\n"
                 "-        self.assertEqual(total([1, 2]), 3)\n"
                 "         pass\n")
        self.assertEqual([], weakened.find([entry("bus/cart.py", patch)]))

    def test_javascript_assertions_and_skips_are_scanned(self):
        patch = ("@@ -2,4 +2,3 @@\n"
                 "-  expect(total([1, 2])).toBe(3);\n"
                 "   expect(done).toBe(true);\n")
        found = weakened.find([entry("src/cart.test.ts", patch)])
        self.assertEqual(1, len(found))
        self.assertIn("deleted", found[0])
        patch = ("@@ -2,2 +2,3 @@\n"
                 "+  it.skip(\"totals\", () => {});\n"
                 "   it(\"kept\", () => {});\n")
        found = weakened.find([entry("src/cart.test.ts", patch)])
        self.assertEqual(1, len(found))
        self.assertIn("skipped", found[0])

    def test_an_added_assertion_is_not_a_weakening(self):
        patch = ("@@ -2,2 +2,3 @@\n"
                 "+        self.assertEqual(total([1, 2]), 3)\n"
                 "         pass\n")
        self.assertEqual([], weakened.find([entry("tests/test_cart.py", patch)]))

    def test_a_removed_non_assertion_line_is_not_a_weakening(self):
        patch = ("@@ -2,3 +2,2 @@\n"
                 "-        # arrange the cart\n"
                 "         pass\n")
        self.assertEqual([], weakened.find([entry("tests/test_cart.py", patch)]))

    def test_an_entry_without_a_patch_is_left_to_the_review(self):
        self.assertEqual([], weakened.find([{"filename": "tests/test_cart.py"}]))
        self.assertEqual([], weakened.find([entry("tests/test_cart.py", "")]))


if __name__ == "__main__":
    unittest.main()
