"""claude-2 is a second Claude subscription on its own slot: the station
claims for it, gives it work where claude takes work, and maps it back to
the claude runner."""
import unittest

from bus import factory, labels, station


class ClaudeSlotsTest(unittest.TestCase):
    def test_claude_2_is_a_lane_of_the_claude_runner(self):
        self.assertIn("claude-2", labels.LANES)
        self.assertEqual(labels.base_lane("claude-2"), "claude")
        self.assertIn("lane:claude-2", labels.LABELS)

    def test_claude_2_takes_work_where_claude_does(self):
        # A slot missing from LANE_ORDER or a rung beats available and idles.
        slots = [x for x in labels.LANES if labels.base_lane(x) == "claude"]
        self.assertEqual(slots, ["claude", "claude-2"])
        self.assertEqual(factory.CLAUDE, ("claude", "claude-2"))
        for slot in slots:
            self.assertIn(slot, factory.LANE_ORDER)
            for model in ("claude-sonnet-5-5", "claude-opus-5-5"):
                rung = next(lanes for lanes, m in factory.LADDER if m == model)
                self.assertIn(slot, rung, model)
        order = factory.LANE_ORDER
        self.assertEqual(order.index("claude-2"), order.index("claude") + 1)

    def test_claude_2_runs_the_claude_models_and_never_a_retry(self):
        self.assertTrue(station.lane_takes_model("claude-2", "claude-opus-5-5"))
        self.assertFalse(station.lane_takes_model("claude-2", "gpt-6-luna"))
        # claude is never chosen to retry: it spends a subscription.
        self.assertNotIn("claude-2", station.RETRY_ORDER)
        self.assertIn("claude-2", station.model_lanes("claude-opus-5-5"))


if __name__ == "__main__":
    unittest.main()
