import json
import os
import tempfile
import unittest
from unittest import mock
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

    def _limit_line(self, used):
        return json.dumps({"type": "event_msg", "payload": {"rate_limits": {
            "primary": {"used_percent": used, "window_minutes": 300, "resets_in_seconds": 60}}}})

    def test_codex_reads_only_the_newest_reading_from_the_tail_of_the_newest_files(self):
        # 2026-10-14: every station pass parsed all 770 session files (4.4 GB)
        # for a reading that sits at the end of the newest one.
        sessions = Path(self.home) / "sessions"
        sessions.mkdir()
        old = sessions / "old.jsonl"
        old.write_text(self._limit_line(10) + "\n")
        os.utime(old, (1000, 1000))
        new = sessions / "new.jsonl"
        filler = ("x" * 1000 + "\n") * 2000  # 2 MB of lines before the reading
        new.write_text(self._limit_line(40) + "\n" + filler + self._limit_line(55) + "\n")
        os.utime(new, (2000, 2000))
        opened = []
        real_open = Path.open

        def counting_open(path, *args, **kwargs):
            opened.append(path.name)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", counting_open):
            rows = usage.codex_records(["codex"], sessions, 1000)
        self.assertEqual([row["used_pct"] for row in rows], [55])
        self.assertEqual(opened, ["new.jsonl"])  # the older file is never opened

    def test_codex_falls_back_to_an_older_file_when_the_newest_has_no_reading(self):
        sessions = Path(self.home) / "sessions"
        sessions.mkdir()
        older = sessions / "older.jsonl"
        older.write_text(self._limit_line(30) + "\n")
        os.utime(older, (1000, 1000))
        newest = sessions / "newest.jsonl"
        newest.write_text('{"type": "other"}\nnot json\n')
        os.utime(newest, (2000, 2000))
        self.assertEqual([row["used_pct"] for row in usage.codex_records(["codex"], sessions, 1000)], [30])

    def test_codex_reads_rate_limits_beside_token_info_as_the_current_cli_writes_them(self):
        # The current CLI's token_count event: payload.info holds token counts
        # and payload.rate_limits sits beside it. The reader looked only inside
        # info, missed every current line and reported a stale older reading.
        sessions = Path(self.home) / "sessions"
        sessions.mkdir()
        (sessions / "current.jsonl").write_text(json.dumps({
            "timestamp": "2026-10-14T13:35:00Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 5}},
                        "rate_limits": {"limit_id": "codex",
                                        "primary": {"used_percent": 63.0, "window_minutes": 300,
                                                    "resets_at": 1792005045},
                                        "secondary": {"used_percent": 86.0, "window_minutes": 10080,
                                                      "resets_at": 1792472636}}}}) + "\n")
        rows = usage.codex_records(["codex"], sessions, 1791990000)
        self.assertEqual([(row["window"], row["used_pct"]) for row in rows],
                         [("5-hour", 63.0), ("weekly", 86.0)])

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
