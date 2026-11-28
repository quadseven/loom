"""Daily allowance readings retain their real amount and provider unit."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bus import daily_allowance, meter, usage


START = datetime(2026, 11, 27, tzinfo=timezone.utc).timestamp()
NOON = START + 12 * 3600
RESET = "2026-11-28T00:00:00+00:00"
TARGET = {"LOOM_DAILY_PACE_TARGET_HOUR": "18", "LOOM_DAILY_PACE_TARGET_SHARE": "90"}


def reading(provider="openrouter", observed=NOON, **changes):
    row = {"provider": provider, "window": "daily", "used": 120, "limit": 1000,
           "unit": "requests" if provider == "openrouter" else "neurons",
           "resets_at": RESET,
           "observed_at": datetime.fromtimestamp(observed, timezone.utc).isoformat()}
    row.update(changes)
    return row


class DailyMeterTest(unittest.TestCase):
    def test_usage_snapshot_preserves_daily_counts_only(self):
        with TemporaryDirectory() as home:
            path = Path(home) / "state" / "usage" / "openrouter.meter.json"
            meter.write(str(path), {"at": NOON, "windows": [
                {"window": "daily", "used": 0, "limit": 1000, "unit": "requests",
                 "used_pct": 0, "resets_at": RESET},
                {"window": "credit", "used": 2, "limit": 10, "unit": "USD",
                 "used_pct": 20},
            ]})
            records = usage.collect(home, now=NOON, lane_names=["openrouter"], host="test")
            daily, credit = records

        self.assertEqual((daily["used"], daily["limit"], daily["unit"]), (0, 1000, "requests"))
        self.assertNotIn("used", credit)
        self.assertNotIn("limit", credit)


class DailyAllowanceTest(unittest.TestCase):
    def test_two_provider_units_and_real_zero_survive_projection(self):
        actual = daily_allowance.rows([
            reading(used=0),
            reading("workersai", used=7400.5, limit=10000),
            reading(window="credit", used=9, limit=10, unit="USD"),
        ], NOON, {})
        self.assertEqual([(row["provider"], row["used"], row["limit"], row["unit"])
                          for row in actual],
                         [("openrouter", 0, 1000, "requests"),
                          ("workersai", 7400.5, 10000, "neurons")])
        self.assertEqual(actual[0]["resets_at"], RESET)
        self.assertEqual(actual[0]["observed_at"], "2026-11-27T12:00:00+00:00")
        self.assertIsNone(actual[0]["expected_pct"])
        self.assertIsNone(actual[0]["verdict"])

    def test_target_share_advances_linearly_and_stops_at_the_target_hour(self):
        for hour, expected in ((12, 60), (18, 90), (21, 90)):
            now = START + hour * 3600
            with self.subTest(hour=hour):
                [actual, _] = daily_allowance.rows([reading(observed=now, used=850)], now, TARGET)
                self.assertEqual(actual["expected_pct"], expected)
                self.assertEqual(actual["verdict"], "ahead" if hour == 12 else "on")

    def test_a_same_tick_reading_remains_current_after_iso_rounding(self):
        now = NOON + 0.000_000_8
        [actual, _] = daily_allowance.rows([reading(observed=now, used=0)], now, TARGET)
        self.assertEqual(actual["used"], 0)

    def test_verdict_respects_the_existing_slack_setting(self):
        rows = [reading(used=500), reading("workersai", used=9500, limit=10000)]
        defaults = daily_allowance.rows(rows, NOON, TARGET)
        tight = daily_allowance.rows(rows, NOON, dict(TARGET, LOOM_PACE_SLACK="5"))
        self.assertEqual([row["verdict"] for row in defaults], ["on", "ahead"])
        self.assertEqual([row["verdict"] for row in tight], ["behind", "ahead"])

    def test_missing_or_invalid_target_does_not_hide_the_counts(self):
        for env in ({}, {"LOOM_DAILY_PACE_TARGET_HOUR": "18"},
                    dict(TARGET, LOOM_DAILY_PACE_TARGET_HOUR="0"),
                    dict(TARGET, LOOM_DAILY_PACE_TARGET_HOUR="25"),
                    dict(TARGET, LOOM_DAILY_PACE_TARGET_SHARE="NaN"),
                    dict(TARGET, LOOM_DAILY_PACE_TARGET_SHARE="101")):
            with self.subTest(env=env):
                [actual, _] = daily_allowance.rows([reading(used=0)], NOON, env)
                self.assertEqual(actual["used"], 0)
                self.assertIsNone(actual["expected_pct"])
                self.assertIsNone(actual["verdict"])

    def test_missing_stale_reset_future_and_invalid_counts_show_no_reading(self):
        bad = (
            [],
            [reading(window="credit")],
            [reading(observed=NOON - 7201)],
            [reading(observed=NOON + 1)],
            [reading(observed_at=None, as_of="2026-11-27T12:00:00+00:00")],
            [reading(resets_at="2026-11-27T00:00:00+00:00")],
            [reading(resets_at="2026-11-28T01:00:00+00:00")],
            [reading(resets_at=None)],
            [reading(used=None)],
            [reading(used=-1)],
            [reading(used=True)],
            [reading(limit=float("inf"))],
            [reading(limit=float("nan"))],
            [reading(unit="neurons")],
        )
        for records in bad:
            with self.subTest(records=records):
                [actual, _] = daily_allowance.rows(records, NOON, TARGET)
                self.assertEqual(actual, {"provider": "openrouter", "unit": "requests",
                                          "used": None, "limit": None, "observed_at": None,
                                          "resets_at": None, "verdict": None, "expected_pct": None})

    def test_midnight_requires_a_reading_from_the_new_day(self):
        [actual, _] = daily_allowance.rows([reading(observed=START - 1)], START, TARGET)
        self.assertIsNone(actual["used"])
        [actual, _] = daily_allowance.rows([reading(observed=START, used=0)], START, TARGET)
        self.assertEqual((actual["used"], actual["expected_pct"], actual["verdict"]),
                         (0, 0, "on"))

    def test_zero_limit_has_no_pace_and_an_overage_keeps_its_real_amount(self):
        [empty, _] = daily_allowance.rows([reading(used=0, limit=0)], NOON, TARGET)
        self.assertEqual((empty["used"], empty["limit"], empty["verdict"]), (0, 0, None))
        [overage, _] = daily_allowance.rows([reading(used=1100)], NOON, TARGET)
        self.assertEqual((overage["used"], overage["limit"], overage["verdict"]), (1100, 1000, "ahead"))
