"""Tests for draft pull request handling in merge verdict and status."""
import unittest

from bus import brain, merge_verdict

BOT = "grug-tribe"
BOT_AUTHOR = {"__typename": "Bot", "login": BOT}
PERSON = {"__typename": "User", "login": "reviewer-one"}


def node(checks="SUCCESS", reviews=(), threads=(), is_draft=False):
    """A pull request's ready facts: head committed at 12:45 on 2030-01-01."""
    return {
        "commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                          "statusCheckRollup": {"state": checks}}}]},
        "reviews": {"nodes": list(reviews)},
        "reviewThreads": {"totalCount": len(threads), "nodes": list(threads)},
        "isDraft": is_draft,
    }


def review(author, state, at="2030-01-01T08:06:00Z"):
    return {"author": author, "state": state, "submittedAt": at}


def thread(resolved, author=BOT_AUTHOR, at="2029-01-01T00:00:00Z"):
    return {"isResolved": resolved,
            "root": {"nodes": [{"author": author, "createdAt": at}]}}


class ReadyFieldsDraftTests(unittest.TestCase):
    def test_ready_fields_includes_isDraft(self):
        self.assertIn("isDraft", brain.READY_FIELDS)


class ReadyVerdictDraftTests(unittest.TestCase):
    def test_draft_pr_is_blocked_by_lane(self):
        facts = node(is_draft=True)
        verdict = merge_verdict.ready_verdict(facts, BOT)
        self.assertFalse(verdict.ready)
        self.assertIn("draft", verdict.blockers)
        self.assertEqual(verdict.actors, (merge_verdict.LANE,))

    def test_draft_pr_blocked_even_with_clean_checks_and_reviews(self):
        facts = node(checks="SUCCESS", reviews=[review(BOT_AUTHOR, "APPROVED")],
                     threads=[thread(True)], is_draft=True)
        verdict = merge_verdict.ready_verdict(facts, BOT)
        self.assertFalse(verdict.ready)
        self.assertIn("draft", verdict.blockers)

    def test_draft_blocker_appears_first_in_rule_order(self):
        facts = node(checks="FAILURE", is_draft=True)
        verdict = merge_verdict.ready_verdict(facts, BOT)
        self.assertEqual(verdict.blockers[0], "draft")

    def test_non_draft_pr_not_blocked_by_draft(self):
        facts = node(is_draft=False)
        verdict = merge_verdict.ready_verdict(facts, BOT)
        self.assertTrue(verdict.ready)
        self.assertNotIn("draft", verdict.blockers)


if __name__ == "__main__":
    unittest.main()