"""The operator-hold backstop does not read a quoted criterion as a step."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import operator_hold  # noqa: E402

CRITERION = ("- [ ] The cause is found and documented or the exact operator "
             "step is recorded")


class CriteriaLines(unittest.TestCase):
    def test_a_checkbox_criterion_is_not_a_step(self):
        body = ("## Operator steps before merge\nnone\n\n"
                "## Acceptance criteria\n%s\n" % CRITERION)
        self.assertEqual(operator_hold.operator_steps(body), [])

    def test_a_blockquoted_line_is_not_a_step(self):
        body = "> or recorded with the exact operator step.\n"
        self.assertEqual(operator_hold.operator_steps(body), [])

    def test_a_checked_box_with_other_bullet_mark_is_not_a_step(self):
        body = ("## Operator steps before merge\nnone\n\n"
                "## Acceptance criteria\n* [x] The exact operator step is recorded\n")
        self.assertEqual(operator_hold.operator_steps(body), [])

    def test_a_box_without_a_none_section_still_holds(self):
        body = "- [ ] Live cutover (not built: operator task - grow the disk)\n"
        self.assertEqual(len(operator_hold.operator_steps(body)), 1)

    def test_a_prose_sentence_still_holds(self):
        body = "The operator step before merge is to rotate the deploy key.\n"
        self.assertEqual(len(operator_hold.operator_steps(body)), 1)


if __name__ == "__main__":
    unittest.main()
