"""The due date of an awaiting-live-check ticket and the overdue section."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import live_check_due as due  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
LABELLED = datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)


def row(number, days_past):
    return (NOW - timedelta(days=days_past), {"number": number, "title": "t%d" % number})


class DueDate(unittest.TestCase):
    def test_a_body_line_wins_over_the_label_time(self):
        body = "Why\n\nLive check due: 2026-12-24\n"
        self.assertEqual(due.due_date(body, LABELLED),
                         datetime(2026, 12, 24, tzinfo=timezone.utc))

    def test_no_line_means_the_label_time_plus_seven_days(self):
        self.assertEqual(due.due_date("no line", LABELLED), LABELLED + timedelta(days=7))
        self.assertEqual(due.due_date(None, LABELLED), LABELLED + timedelta(days=7))


class Section(unittest.TestCase):
    def test_counts_only_tickets_past_due_and_lists_five_oldest_first(self):
        rows = [row(n, n) for n in range(1, 9)] + [row(50, -3)]
        lines = due.section_lines(rows, NOW)
        self.assertEqual(lines[0], "### Live checks overdue")
        self.assertEqual(lines[1], "- 8 overdue")
        shown = [x.split()[1] for x in lines[2:]]
        self.assertEqual(shown, ["#8", "#7", "#6", "#5", "#4"])


if __name__ == "__main__":
    unittest.main()
