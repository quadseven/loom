"""Where the openai-mini lane may take work (loom#3188): easy tickets only,
never a review, and only models the ledger can price."""
import calendar
import tempfile
import unittest

from bus import factory, labels, openai_budget, review_sweep, station, usage


class OpenAIMiniLane(unittest.TestCase):
    def test_it_is_a_lane_with_a_kind(self):
        self.assertIn("openai-mini", labels.LANES)
        self.assertEqual(factory.lane_kind("openai-mini"), "never-paid")

    def test_it_takes_easy_tickets_only(self):
        tiers = {tier for tier, lanes in factory.TIERS.items() if "openai-mini" in lanes}
        self.assertEqual(tiers, {"easy"})
        self.assertFalse(any("openai-mini" in lanes for lanes, _ in factory.LADDER))
        self.assertNotIn("openai-mini", station.RETRY_ORDER)
        # A medium ticket with no medium lane up never falls back to it.
        self.assertIn("openai-mini", factory.lanes_of("easy", True))
        self.assertNotIn("openai-mini", factory.fallback_of("easy", True))
        self.assertIn("spark", factory.fallback_of("easy", True))

    def test_it_never_reviews_whatever_the_setting_names(self):
        config = review_sweep.Config.from_env({"LOOM_REVIEW_SWEEP": "on",
                                               "LOOM_REVIEW_LANES": "openai-mini,codex-2"})
        self.assertEqual(config.lanes, ("codex-2",))
        only = review_sweep.Config.from_env({"LOOM_REVIEW_SWEEP": "on",
                                             "LOOM_REVIEW_LANES": "openai-mini"})
        self.assertNotIn("openai-mini", only.lanes)
        self.assertNotIn("openai-mini", review_sweep.DEFAULT_LANES)

    def test_usage_shows_the_months_spend_against_the_cap(self):
        home = tempfile.mkdtemp()
        now = calendar.timegm((2026, 12, 15, 0, 0, 0))
        openai_budget.record(home, "lane", "gpt-6-luna", openai_budget.Usage(0, 0, 18_000_000), now)
        [row] = usage.collect(home, env={}, now=now, lane_names=["openai-mini"], host="h")
        self.assertEqual((row["window"], row["used_pct"], row["remaining"], row["unit"]),
                         ("monthly", 10.0, 81.0, "USD"))
        self.assertTrue(row["resets_at"].startswith("2027-01-01T00:00:00"))

    def test_the_station_routes_it_only_models_with_a_pinned_price(self):
        self.assertEqual(station.LANE_MODELS["openai-mini"],
                         ["openai/" + name for name in openai_budget.PRICES])
        self.assertEqual(openai_budget.LANE_MODEL, "openai/" + openai_budget.HELPER_MODEL)


if __name__ == "__main__":
    unittest.main()
