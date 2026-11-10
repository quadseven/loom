from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import watch  # noqa: E402


class CloseAuditSummaryTest(unittest.TestCase):
    def test_close_audit_summary_line_with_zero_refused_is_not_kept(self):
        line = ("station: close audit: 6 closes read, 0 refused, 34 waiting; "
                "at most 80 reads a pass")
        self.assertFalse(watch.keep(line), line)

    def test_close_audit_summary_line_with_nonzero_refused_is_not_kept(self):
        line = ("station: close audit: 6 closes read, 2 refused, 34 waiting; "
                "at most 80 reads a pass")
        self.assertFalse(watch.keep(line), line)

    def test_close_audit_summary_line_with_timestamp_prefix_is_not_kept(self):
        line = ("[2026-11-10T14:15:03Z] station: close audit: 6 closes read, "
                "0 refused, 34 waiting; at most 80 reads a pass")
        self.assertFalse(watch.keep(line), line)

    def test_other_close_audit_lines_are_not_kept_by_default(self):
        line = "station: close audit: example-owner/repo-a has more closes than one read shows"
        self.assertFalse(watch.keep(line), line)

    def test_close_audit_summary_different_numbers_are_not_kept(self):
        for reads, refused, waiting, max_reads in (
            (1, 0, 0, 80),
            (100, 5, 200, 120),
            (0, 0, 0, 80),
        ):
            line = (f"station: close audit: {reads} closes read, {refused} refused, "
                    f"{waiting} waiting; at most {max_reads} reads a pass")
            with self.subTest(line=line):
                self.assertFalse(watch.keep(line), line)