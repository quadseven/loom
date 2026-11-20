"""A regression brief refuses when a follow-up ticket already fixes it."""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"


def _ticket(number, state, why, state_open=True):
    return {"number": number, "state": "open" if state_open else "closed",
            "body": "Difficulty: easy\n\n## Why\n%s\n\n## What\nx\n" % why,
            "labels": [{"name": "loom:ticket"}, {"name": state}]}


class RegressionFollowupDedupeTests(unittest.TestCase):
    def _brief(self, children):
        with unittest.mock.patch.object(brain, "_goal_children", return_value=children), \
                unittest.mock.patch.object(brain, "_regressed_ticket",
                                           side_effect=RuntimeError("reached")):
            return brain._brief_regression(
                None, REPO, 1, "Fix it", "Regresses #7\nmeasured evidence\n",
                None, None, "## What\nx\n", 7)

    def test_in_flight_followup_that_names_the_ticket_is_refused(self):
        followup = _ticket(9, "loom:briefed", "The fix from example-repo#7 is missing.")
        with self.assertRaisesRegex(ValueError, "already in flight as #9"):
            self._brief([followup])
        # Kept rules, asserted after the new case: a closed or shipped ticket,
        # or one that names another ticket or another repo, does not block.
        others = [_ticket(10, "loom:briefed", "from #7", state_open=False),
                  _ticket(11, "loom:shipped", "from #7"),
                  _ticket(12, "loom:queued", "from #70 and other-repo#7")]
        with self.assertRaisesRegex(RuntimeError, "reached"):
            self._brief(others)


if __name__ == "__main__":
    unittest.main()
