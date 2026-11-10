"""Tests for the draft pull request blocker in ready_verdict (loom#2931)."""
import unittest

from bus import brain
from bus import merge_verdict


BOT = "grug-tribe"
BOT_AUTHOR = {"__typename": "Bot", "login": BOT}


def node(is_draft=False, checks="SUCCESS", reviews=(), threads=()):
    """A pull request's ready facts with optional isDraft."""
    return {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                              "statusCheckRollup": {"state": checks}}}]},
            "reviews": {"nodes": list(reviews)},
            "reviewThreads": {"totalCount": len(threads), "nodes": list(threads)},
            "isDraft": is_draft}


class DraftBlockerTests(unittest.TestCase):
    def test_ready_fields_includes_isDraft(self):
        """READY_FIELDS query text asks for isDraft."""
        self.assertIn("isDraft", brain.READY_FIELDS)

    def test_ready_verdict_blocks_on_draft(self):
        """ready_verdict for a node with isDraft: true returns a blocked verdict naming draft."""
        verdict = merge_verdict.ready_verdict(node(is_draft=True), BOT)
        self.assertFalse(verdict.ready)
        self.assertIn("draft", verdict.blockers)
        self.assertEqual(verdict.actors, ("lane",))

    def test_ready_verdict_ready_when_not_draft(self):
        """ready_verdict for a node with isDraft: false returns READY when otherwise clean."""
        verdict = merge_verdict.ready_verdict(node(is_draft=False), BOT)
        self.assertTrue(verdict.ready)
        self.assertEqual(verdict.text, "READY")

    def test_ready_verdict_draft_blocker_keeps_rule_order(self):
        """draft blocker appears in the correct rule order among other blockers."""
        verdict = merge_verdict.ready_verdict(
            node(is_draft=True, checks="FAILURE", reviews=[{"author": BOT_AUTHOR, "state": "CHANGES_REQUESTED", "submittedAt": "2030-01-01T08:06:00Z"}]),
            BOT)
        self.assertEqual(verdict.blockers, ("checks failure", "draft", "grug changes_requested"))

    def test_ready_verdict_does_not_change_input(self):
        """ready_verdict does not mutate its input node."""
        facts = node(is_draft=True)
        before = repr(facts)
        merge_verdict.ready_verdict(facts, BOT)
        self.assertEqual(repr(facts), before)


if __name__ == "__main__":
    unittest.main()