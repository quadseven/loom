from __future__ import annotations

import unittest

from bus import hold_boxes


class PastTenseMergeTests(unittest.TestCase):
    def test_a_past_tense_merged_after_before_is_not_a_hold(self):
        body = ("- [ ] A loom:pr ticket whose covering pull request merged before "
                "the reconcile window moves to loom:merged\n")
        self.assertEqual(hold_boxes.hold_box_reason(body), "")


if __name__ == "__main__":
    unittest.main()
