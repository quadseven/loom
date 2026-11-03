import unittest

from bus.hold_boxes import hold_boxes


class HoldBoxesPastMergeTests(unittest.TestCase):
    def test_past_merge_of_covering_pull_request_is_not_a_hold(self):
        box = (
            "A loom:pr ticket whose covering pull request merged before the "
            "reconcile window moves to loom:merged"
        )
        self.assertEqual(hold_boxes("- [ ] " + box), [])

    def test_before_merge_is_a_hold(self):
        self.assertEqual(hold_boxes("- [ ] Run the migration before merge"),
                         ["Run the migration before merge"])

    def test_before_merging_this_pull_request_is_a_hold(self):
        box = "Check the dashboard before merging this pull request"
        self.assertEqual(hold_boxes("- [ ] " + box), [box])


if __name__ == "__main__":
    unittest.main()
