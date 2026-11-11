from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import watch  # noqa: E402


class CloseAuditSummaryTest(unittest.TestCase):
    """The close audit's per-pass summary line is status, not an error."""

    def test_close_audit_summary_with_zero_refused_is_not_kept(self):
        # The exact format from bus/close_audit.py:555, as the station logs it.
        line = "station: close audit: 6 closes read, 0 refused, 34 waiting; at most 80 reads a pass"
        self.assertFalse(watch.keep(line), line)

    def test_close_audit_summary_with_nonzero_refused_is_not_kept(self):
        # A nonzero refused count must also be excluded.
        line = "station: close audit: 6 closes read, 2 refused, 34 waiting; at most 80 reads a pass"
        self.assertFalse(watch.keep(line), line)

    def test_close_audit_summary_with_leading_timestamp_is_not_kept(self):
        # The station may prefix the line with a timestamp.
        line = "[2026-11-10T14:15:03Z] station: close audit: 6 closes read, 0 refused, 34 waiting; at most 80 reads a pass"
        self.assertFalse(watch.keep(line), line)

    def test_other_close_audit_lines_are_not_matched_by_expected_res(self):
        # Lines like "close audit: <repo> has more closes than one read shows" are not
        # matched by the new EXPECTED_RES pattern (they don't have the summary format).
        # They are not kept either because they lack error keywords, but the pattern
        # must not over-match.
        line = "station: close audit: example-owner/repo-a has more closes than one read shows"
        bare = watch._LEAD_RE.sub("", line).rstrip("\r\n")
        for pattern in watch.EXPECTED_RES:
            self.assertIsNone(pattern.fullmatch(bare), f"pattern {pattern.pattern} matched {line}")