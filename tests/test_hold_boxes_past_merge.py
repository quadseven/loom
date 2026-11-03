"""Tests for hold_boxes past-tense merge false positive (loom#2547).

The _WEAK_HOLD pattern must not treat "before ... merged" (past tense
describing another PR's merge) as a hold instruction. It should only
match "before merge/merging/it merges/this pull request merges/the pull
request merges" (instructions about THIS PR's own merge).
"""
import unittest
from bus import hold_boxes


class TestHoldBoxesPastMerge(unittest.TestCase):
    """Tests for the past-tense merge false positive fix."""

    def test_past_tense_merged_before_does_not_hold(self):
        """A box mentioning a past merge before something else must not hold."""
        box = "A loom:pr ticket whose covering pull request merged before the reconcile window moves to loom:merged"
        self.assertEqual(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "",
            "past-tense 'merged before' should not be a hold",
        )

    def test_before_merge_still_holds(self):
        """'before merge' (instruction about this PR) must still hold."""
        box = "Run the migration before merge"
        self.assertTrue(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "'before merge' should be a hold",
        )

    def test_before_merging_this_pull_request_still_holds(self):
        """'before merging this pull request' must still hold."""
        box = "Check the dashboard before merging this pull request"
        self.assertTrue(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "'before merging this pull request' should be a hold",
        )

    def test_before_merging_still_holds(self):
        """'before merging' (instruction about this PR) must still hold."""
        box = "Verify the build before merging"
        self.assertTrue(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "'before merging' should be a hold",
        )

    def test_before_it_merges_still_holds(self):
        """'before it merges' must still hold."""
        box = "Wait for CI before it merges"
        self.assertTrue(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "'before it merges' should be a hold",
        )

    def test_before_the_pull_request_merges_still_holds(self):
        """'before the pull request merges' must still hold."""
        box = "Confirm tests pass before the pull request merges"
        self.assertTrue(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "'before the pull request merges' should be a hold",
        )

    def test_before_this_pull_request_merges_still_holds(self):
        """'before this pull request merges' must still hold."""
        box = "Run smoke tests before this pull request merges"
        self.assertTrue(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "'before this pull request merges' should be a hold",
        )

    def test_before_authorized_session_decides_to_merge_still_holds(self):
        """'before the authorized session decides to merge' must still hold."""
        box = "Wait for review before the authorized session decides to merge"
        self.assertTrue(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "'before the authorized session decides to merge' should be a hold",
        )

    def test_merged_long_ago_before_does_not_hold(self):
        """'merged long ago before' must not hold (past tense)."""
        box = "The previous PR merged long ago before the window opened"
        self.assertEqual(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "",
            "past-tense 'merged ... before' should not be a hold",
        )

    def test_ticket_merged_before_deploy_does_not_hold(self):
        """'ticket merged before deploy' must not hold (past tense)."""
        box = "The linked ticket merged before deploy started"
        self.assertEqual(
            hold_boxes.hold_box_reason("- [ ] %s\n" % box),
            "",
            "past-tense 'merged before deploy' should not be a hold",
        )


if __name__ == "__main__":
    unittest.main()