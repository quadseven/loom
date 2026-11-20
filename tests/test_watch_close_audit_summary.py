from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import watch  # noqa: E402

SUMMARY = ("station: close audit: %d closes read, %d refused, %d waiting; "
           "at most 80 reads a pass")


class CloseAuditSummaryTest(unittest.TestCase):
    def test_zero_refused_summary_is_not_kept(self):
        self.assertFalse(watch.keep(SUMMARY % (6, 0, 34)))

    def test_nonzero_refused_summary_is_not_kept(self):
        self.assertFalse(watch.keep(SUMMARY % (6, 2, 34)))

    def test_stamped_summary_is_not_kept(self):
        self.assertFalse(watch.keep("[2026-11-10T14:15:03Z] " + SUMMARY % (6, 0, 34)))

    def test_other_close_audit_lines_stay_kept(self):
        line = ("station: close audit: example-owner/repo has more closes "
                "than one read shows; refused")
        self.assertTrue(watch.keep(line))


if __name__ == "__main__":
    unittest.main()
