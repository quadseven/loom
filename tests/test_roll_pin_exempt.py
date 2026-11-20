"""Goal homes the operator exempted from the roll source-pin pass are left out
of the roll-pin-off breach (LOOM_ROLL_PIN_EXEMPT)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import invariants  # noqa: E402
from bus.invariants import Facts, Ref  # noqa: E402

T0 = 1_700_000_000.0
FIRST = Ref("example-org/alpha#1", "#1")  # leak-guard-allow: a ref shape is the input under test
SECOND = Ref("example-org/beta#2", "issue 2 of a private repo")  # leak-guard-allow


class RollPinExemptTests(unittest.TestCase):
    def test_exempt_goal_homes_are_left_out_of_the_breach(self):
        both = [FIRST, SECOND]
        exempt = (FIRST.key, SECOND.key)
        self.assertEqual(
            invariants.roll_pin_off(Facts(T0, roll_goals=both, roll_exempt=exempt)), [])
        (b,) = invariants.roll_pin_off(
            Facts(T0, roll_goals=both, roll_exempt=(FIRST.key,)))
        text = " ".join(b.evidence)
        self.assertIn("issue 2 of a private repo", text)
        self.assertNotIn("#1", text)
        self.assertIn("1 goal home ships", b.title)


if __name__ == "__main__":
    unittest.main()
