"""Eight codex slots run gpt-6-luna side by side (operator, 2026-10-25: use
the Codex weekly allowance). Each slot is a lane the station claims for and
maps back to the codex runner."""
import unittest

from bus import labels


class CodexSlotsTest(unittest.TestCase):
    def test_codex_three_to_eight_are_lanes_of_the_codex_runner(self):
        for n in range(2, 9):
            slot = "codex-%d" % n
            self.assertIn(slot, labels.LANES)
            self.assertEqual(labels.base_lane(slot), "codex")


if __name__ == "__main__":
    unittest.main()
