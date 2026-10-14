import json
import tempfile
import unittest
from pathlib import Path

from bus import usage


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_session_rate_limits_produce_five_hour_and_weekly_records(self):
        sessions = Path(self.home) / "sessions"
        sessions.mkdir()
        (sessions / "sample.jsonl").write_text(json.dumps({
            "type": "event_msg", "payload": {"rate_limits": {
                "primary": {"used_percent": 21, "window_minutes": 300, "resets_in_seconds": 1800},
                "secondary": {"used_percent": 67, "window_minutes": 10080, "resets_in_seconds": 7200},
            }}
        }) + "\n")
        rows = usage.codex_records(["codex", "codex-2"], sessions, 1000)
        self.assertEqual([row["window"] for row in rows], ["5-hour", "weekly"])
        self.assertEqual([row["used_pct"] for row in rows], [21, 67])
        self.assertEqual([row["resets_at"] for row in rows], [
            "1970-01-01T00:46:40+00:00", "1970-01-01T02:16:40+00:00"])

    def test_unexposed_provider_and_raising_adapter_are_reported(self):
        def broken(_home, _lanes, _env, _now):
            raise RuntimeError("details are not included")
        rows = usage.collect(self.home, now=1000, lane_names=["mystery", "zen"],
                             adapters={"zen": broken})
        by_provider = {row["provider"]: row for row in rows}
        self.assertIsNone(by_provider["mystery"]["used_pct"])
        self.assertIn("not exposed", by_provider["mystery"]["note"])
        self.assertEqual(by_provider["zen"]["note"], "adapter error: RuntimeError")

    def test_anthropic_record_uses_spend_fixture_and_budget(self):
        directory = Path(self.home) / "state" / "usage"
        directory.mkdir(parents=True)
        (directory / "spend.jsonl").write_text(json.dumps({
            "at": 900, "slot": "anthropic", "cost_usd": 2.5
        }) + "\n")
        row = usage.anthropic_records(self.home, ["anthropic", "anthropic-2"], {
            "LOOM_ANTHROPIC_BUDGET_USD": "10",
            "LOOM_ANTHROPIC_BUDGET_SINCE": "1970-01-01",
            "LOOM_ANTHROPIC_FLOOR_USD": "1",
        }, 1000)[0]
        self.assertEqual(row["used_pct"], 25)
        self.assertEqual(row["remaining"], 7.5)
        self.assertEqual(row["note"], "floor USD 1.00")


if __name__ == "__main__":
    unittest.main()
