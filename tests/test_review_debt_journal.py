"""Review debt recognizes review entries inside journal comments."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import handoff, review_debt  # noqa: E402

AUTHOR = "trusted-example"
MARKER = handoff.JOURNAL_MARKER


def comment(body, login=AUTHOR):
    return {"body": body, "created_at": "2026-11-10T00:00:00Z",
            "user": {"login": login}}


class JournalReviews(unittest.TestCase):
    def test_listed_author_review_tick_counts_at_its_tick_time(self):
        body = "%s\nTick 2026-11-11T04:30:00Z, lane-machine\nLoom review, routine pass"
        found = review_debt.newest_review([comment(body % MARKER)], [AUTHOR])
        self.assertEqual(found, 1794371400.0)

    def test_non_review_journal_tick_does_not_count(self):
        body = "%s\nTick 2026-11-11T04:30:00Z, lane-machine\nBrain tick, routine pass"
        self.assertIsNone(review_debt.newest_review([comment(body % MARKER)], [AUTHOR]))

    def test_unlisted_author_journal_review_does_not_count(self):
        body = "%s\nTick 2026-11-11T04:30:00Z, lane-machine\nLoom review, routine pass"
        self.assertIsNone(review_debt.newest_review(
            [comment(body % MARKER, "unlisted-example")], [AUTHOR]))


if __name__ == "__main__":
    unittest.main()
