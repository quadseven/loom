"""Eight codex slots run gpt-6-luna side by side (operator, 2026-10-25: use
the Codex weekly allowance). Each slot is a lane the station claims for and
maps back to the codex runner."""
import unittest

from bus import factory, labels


class CodexSlotsTest(unittest.TestCase):
    def test_codex_three_to_eight_are_lanes_of_the_codex_runner(self):
        for n in range(2, 9):
            slot = "codex-%d" % n
            self.assertIn(slot, labels.LANES)
            self.assertEqual(labels.base_lane(slot), "codex")

    def test_every_codex_slot_takes_work_where_codex_does(self):
        # The six slots were added as lanes only, so no queued ticket of any
        # tier ever reached them: 35 medium and hard tickets waited "no free
        # lane in tier" while codex-3 to codex-8 beat available and idle.
        slots = [x for x in labels.LANES if labels.base_lane(x) == "codex"]
        self.assertEqual(len(slots), 8)
        for tier, lanes in factory.TIERS.items():
            if "codex" in lanes:
                self.assertEqual([x for x in slots if x not in lanes], [], tier)
        rung = next(lanes for lanes, model in factory.LADDER if model == "gpt-6-luna")
        self.assertEqual([x for x in slots if x not in rung], [])

    def test_every_slot_takes_work_where_its_lane_does(self):
        # The same rule for every numbered slot: a slot of a tiered lane that
        # is in no tier sits idle with a live heartbeat.
        for tier, lanes in factory.TIERS.items():
            for slot in labels.LANES:
                base = labels.base_lane(slot)
                if base != slot and base in lanes:
                    self.assertIn(slot, lanes, tier)


if __name__ == "__main__":
    unittest.main()
