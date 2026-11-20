"""A Loom review written through `bus journal` counts as a review (bus/review_debt.py)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import review_debt as rd  # noqa: E402
from bus.handoff import JOURNAL_MARKER  # noqa: E402

AUTHORS = ["listed-example"]


def comment(body, login="listed-example"):
    return {"created_at": "2026-11-11T04:30:00Z", "body": body, "user": {"login": login}}


class JournalReviewTests(unittest.TestCase):
    def test_a_review_written_through_bus_journal_counts(self):
        body = "%s\nTick 2026-11-11T04:30:00Z, lane-machine\nLoom review, duty 7." % JOURNAL_MARKER
        self.assertTrue(rd.is_review(comment(body), AUTHORS))
        # Main already refuses these two; kept in this one method so no test
        # passes on main.
        other = "%s\nTick 2026-11-11T04:30:00Z, lane-machine\nNotes only." % JOURNAL_MARKER
        self.assertFalse(rd.is_review(comment(other), AUTHORS))
        self.assertFalse(rd.is_review(comment(body, login="unlisted-example"), AUTHORS))


if __name__ == "__main__":
    unittest.main()
