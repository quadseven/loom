import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

    def test_codex_records_carry_when_codex_wrote_the_reading_and_the_window_length(self):
        # as_of is when the station collected the reading; a codex that last
        # ran hours ago wrote it then, and the Usage view must say so.
        sessions = Path(self.home) / "sessions"
        sessions.mkdir()
        (sessions / "current.jsonl").write_text(json.dumps({
            "timestamp": "2026-10-14T17:35:00.250Z", "type": "event_msg",
            "payload": {"rate_limits": {"primary": {"used_percent": 5.0, "window_minutes": 300,
                                                    "resets_at": 1792005045}}}}) + "\n")
        row = usage.codex_records(["codex"], sessions, 1792000000)[0]
        self.assertEqual(row["observed_at"], "2026-10-14T17:35:00.250000+00:00")
        self.assertEqual(row["window_minutes"], 300)
        self.assertEqual(row["as_of"], "2026-10-14T17:46:40+00:00")

    def test_codex_reading_without_a_timestamp_has_no_observed_time(self):
        sessions = Path(self.home) / "sessions"
        sessions.mkdir()
        (sessions / "old.jsonl").write_text(self._limit_line(12) + "\n")
        row = usage.codex_records(["codex"], sessions, 1000)[0]
        self.assertIsNone(row["observed_at"])
        self.assertEqual(row["window_minutes"], 300)

    def test_each_record_carries_why_its_held_lanes_are_held(self):
        state = Path(self.home) / "state" / "usage"
        state.mkdir(parents=True)
        (state / "zen.json").write_text(json.dumps(
            {"lane": "zen", "available": False, "reason": "limit: out of credit; unavailable until 2030-01-02T00:00:00Z"}))
        (state / "zai.json").write_text(json.dumps({"lane": "zai", "available": True, "reason": "ready"}))
        (state / "workersai.json").write_text("not json")
        rows = usage.collect(self.home, now=1000, lane_names=["zen", "zai", "workersai"])
        holds = {lane: reason for row in rows for lane, reason in row["holds"].items()}
        self.assertEqual(holds, {"zen": "limit: out of credit; unavailable until 2030-01-02T00:00:00Z"})

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

    def test_spark_hold_uses_absolute_end_time(self):
        directory = Path(self.home) / "state" / "usage"
        directory.mkdir(parents=True)
        now = 1_800_000_000
        end = now + 600
        (directory / "spark.limit").write_text("%s endpoint refused\n" % end)

        row = usage._spark_records(self.home, ["spark"], {}, now)[0]

        self.assertEqual(row["resets_at"], "2027-01-15T08:10:00+00:00")
        self.assertEqual(row["note"], "endpoint refused")

    def test_expired_spark_hold_is_reported_as_no_active_hold(self):
        directory = Path(self.home) / "state" / "usage"
        directory.mkdir(parents=True)
        now = 1_800_000_000
        (directory / "spark.limit").write_text("%s endpoint refused\n" % (now - 3600))

        row = usage._spark_records(self.home, ["spark"], {}, now)[0]

        self.assertIsNone(row["used_pct"])
        self.assertIsNone(row["resets_at"])
        self.assertEqual(row["note"], "no active hold")

    def test_spark_slots_read_their_own_limit_files(self):
        directory = Path(self.home) / "state" / "usage"
        directory.mkdir(parents=True)
        now = 1_800_000_000
        (directory / "spark.limit").write_text("%s reason one\n" % (now + 600))
        (directory / "spark-2.limit").write_text("%s reason two\n" % (now + 1200))

        rows = usage._spark_records(self.home, ["spark", "spark-2"], {}, now)

        self.assertEqual([row["lanes"] for row in rows], [["spark"], ["spark-2"]])
        self.assertEqual([row["source"] for row in rows], [
            "state/usage/spark.limit", "state/usage/spark-2.limit"])
        self.assertEqual([row["resets_at"] for row in rows], [
            "2027-01-15T08:10:00+00:00", "2027-01-15T08:20:00+00:00"])
        self.assertEqual([row["note"] for row in rows], ["reason one", "reason two"])


    # -- limit holds, host, credit settings and the command line (loom#813) --
    def _beat(self, lane, **fields):
        state = Path(self.home) / "state" / "usage"
        state.mkdir(parents=True, exist_ok=True)
        beat = {"lane": lane, "at": fields.pop("at", 1_800_000_000)}
        beat.update(fields)
        (state / (lane + ".json")).write_text(json.dumps(beat))

    def test_claude_and_agy_read_limited_until_from_their_heartbeat_then_keep_the_last_limit(self):
        now = 1_800_000_000
        self._beat("claude", available=False, retry_at=now + 3600,
                   reason="limit: provider limit reached; lane waits until 2027-01-15T09:00:00Z")
        self._beat("agy", available=True, reason="")
        rows = usage.collect(self.home, now=now, lane_names=["claude", "agy"], host="lanes")
        by = {row["provider"]: row for row in rows}
        self.assertEqual(by["claude"]["window"], "limit")
        self.assertEqual(by["claude"]["resets_at"], "2027-01-15T09:00:00+00:00")
        self.assertEqual(by["claude"]["note"], "limited: provider limit reached")
        self.assertEqual(by["claude"]["observed_at"], "2027-01-15T08:00:00+00:00")
        self.assertEqual(by["agy"]["note"], "no limit seen")
        self.assertIsNone(by["agy"]["resets_at"])
        # The hold lifts: the reading keeps the last limit and when it was seen.
        self._beat("claude", at=now + 4000, available=True, reason="")
        row = usage.collect(self.home, now=now + 4000, lane_names=["claude"], host="lanes")[0]
        self.assertEqual(row["note"], "last limit: provider limit reached")
        self.assertEqual(row["resets_at"], "2027-01-15T09:00:00+00:00")
        self.assertEqual(row["observed_at"], "2027-01-15T08:00:00+00:00")

    def test_a_hold_whose_retry_time_passed_is_not_a_current_limit(self):
        now = 1_800_000_000
        self._beat("agy", available=False, retry_at=now - 60, reason="limit: quota; lane waits until x")
        row = usage.collect(self.home, now=now, lane_names=["agy"], host="lanes")[0]
        self.assertEqual(row["note"], "no limit seen")

    def test_muse_says_it_reports_no_limit_and_how_old_its_heartbeat_is(self):
        station = Path(self.home) / "state" / "station"
        station.mkdir(parents=True)
        (station / "remote-status.json").write_text(json.dumps(
            {"muse": {"checked_at": 1_800_000_000, "heartbeat": 1_799_999_400, "claims": 0}}))
        row = usage.collect(self.home, now=1_800_000_000, lane_names=["muse"], host="lanes")[0]
        self.assertEqual((row["provider"], row["window"]), ("muse", "limit"))
        self.assertIn("reports no usage or limit to Loom", row["note"])
        self.assertEqual(row["observed_at"], "2027-01-15T07:50:00+00:00")

    def test_anthropic_without_a_budget_names_the_settings_and_the_spend(self):
        directory = Path(self.home) / "state" / "usage"
        directory.mkdir(parents=True)
        (directory / "spend.jsonl").write_text(json.dumps({"at": 900, "slot": "anthropic", "cost_usd": 2.5}) + "\n")
        row = usage.anthropic_records(self.home, ["anthropic"], {}, 1000)[0]
        self.assertIsNone(row["used_pct"])
        self.assertIn("budget is not configured", row["note"])
        self.assertIn("LOOM_ANTHROPIC_BUDGET_USD", row["note"])
        self.assertIn("LOOM_ANTHROPIC_BUDGET_SINCE", row["note"])
        self.assertIn("spent USD 2.50 recorded", row["note"])

    def test_anthropic_credit_reads_used_up_when_every_slot_reports_out_of_credit(self):
        now = 1_800_000_000
        for lane in ("anthropic", "anthropic-2"):
            self._beat(lane, available=False, retry_at=now + 3600,
                       reason="limit: out of credit; unavailable until 2027-01-15T09:00:00Z")
        row = usage.anthropic_records(self.home, ["anthropic", "anthropic-2"], {}, now)[0]
        self.assertEqual((row["used_pct"], row["remaining"]), (100.0, 0.0))
        self.assertEqual(row["source"], "lane heartbeats")
        self.assertIn("every anthropic slot reports out of credit", row["note"])
        # One slot still working: the credit is not known to be gone.
        self._beat("anthropic-2", available=True, reason="")
        row = usage.anthropic_records(self.home, ["anthropic", "anthropic-2"], {}, now)[0]
        self.assertIsNone(row["used_pct"])

    def test_anthropic_without_a_budget_is_used_up_when_the_provider_is_never_topped_up(self):
        row = usage.anthropic_records(self.home, ["anthropic", "anthropic-2"],
                                      {"LOOM_NO_TOPUP": "zen, anthropic"}, 1000)[0]
        self.assertEqual((row["used_pct"], row["remaining"]), (100.0, 0.0))
        self.assertEqual(row["source"], "LOOM_NO_TOPUP")
        self.assertIn("never topped up", row["note"])

    def test_codex_on_a_machine_without_session_logs_says_so(self):
        rows = usage.codex_records(["codex"], Path(self.home) / "no-sessions", 1000)
        self.assertIsNone(rows[0]["used_pct"])
        self.assertEqual(rows[0]["note"], "no codex session logs on this machine")

    def test_every_record_names_the_machine_that_collected_it(self):
        rows = usage.collect(self.home, now=1000, lane_names=["zen", "claude"], host="lane-box")
        self.assertEqual({row["host"] for row in rows}, {"lane-box"})

    def test_the_table_marks_an_old_or_reset_figure_stale(self):
        now = 1_800_000_000
        old = {"provider": "codex", "lanes": ["codex"], "window": "weekly", "used_pct": 40.0,
               "remaining": None, "resets_at": "2027-01-20T00:00:00+00:00",
               "observed_at": "2027-01-14T00:00:00+00:00", "as_of": "2027-01-15T08:00:00+00:00",
               "host": "lane-box", "note": None}
        reset = dict(old, observed_at="2027-01-15T07:30:00+00:00", resets_at="2027-01-15T07:50:00+00:00")
        fresh = dict(old, observed_at="2027-01-15T07:30:00+00:00")
        lines = usage.table([old, reset, fresh], now)
        self.assertIn("40.0% (stale: read 32h ago)", lines[1])
        self.assertIn("40.0% (stale: its window reset after this reading)", lines[2])
        self.assertIn("\t40.0%\t", lines[3])
        self.assertIn("lane-box", lines[3])

    def test_the_command_off_the_lane_machine_prints_no_figures_as_current(self):
        # A remote brain ran bus.usage on its own machine, read a folder
        # nothing refreshed there, and took it as the usage (loom#813).
        now = 1_800_000_000
        self._beat("codex", at=now - 86400, available=True, reason="")
        limits = Path(self.home) / "state" / "usage" / "limits.json"
        limits.write_text("[]\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = usage.main([], env={"LOOM_HOME": self.home}, now=now)
        self.assertEqual(code, 1)
        self.assertIn("this is not the lane machine", out.getvalue())
        self.assertIn("python3 -m bus.usage --live", out.getvalue())
        self.assertNotIn("provider\tlanes", out.getvalue())
        self.assertEqual(limits.read_text(), "[]\n")

    def test_the_command_on_the_lane_machine_writes_and_prints_the_readings(self):
        now = 1_800_000_000
        self._beat("claude", at=now - 30, available=True, reason="")
        out = io.StringIO()
        with redirect_stdout(out):
            code = usage.main([], env={"LOOM_HOME": self.home}, now=now)
        self.assertEqual(code, 0)
        self.assertIn("provider\tlanes", out.getvalue())
        self.assertTrue((Path(self.home) / "state" / "usage" / "limits.json").exists())

    def test_live_reads_the_dashboard_snapshot_with_the_read_token(self):
        now = 1_800_000_000
        seen = []
        packet = {"received_at": "2027-01-15T07:55:00+00:00", "readings": [
            {"provider": "codex", "lanes": ["codex"], "window": "weekly", "used_pct": 40.0,
             "remaining": None, "resets_at": "2027-01-20T00:00:00+00:00",
             "observed_at": "2027-01-15T07:50:00+00:00", "as_of": "2027-01-15T07:55:00+00:00",
             "host": "lane-box", "note": None}]}
        env = {"LOOM_TRANSCRIPT_URL": "https://dashboard.example.com/api/transcripts",
               "LOOM_HANDOFF_TOKEN": "YOUR_HANDOFF_TOKEN_HERE"}
        out = io.StringIO()
        with mock.patch.object(usage, "fetch_live", lambda url, token: seen.append((url, token)) or packet), \
                redirect_stdout(out):
            code = usage.main(["--live"], env=env, now=now)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("https://dashboard.example.com/api/usage", "YOUR_HANDOFF_TOKEN_HERE")])
        self.assertIn("uploaded at 2027-01-15T07:55:00+00:00 (5m ago)", out.getvalue())
        self.assertIn("\t40.0%\t", out.getvalue())

    def test_live_without_the_dashboard_settings_names_them(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = usage.main(["--live"], env={}, now=1000)
        self.assertEqual(code, 1)
        self.assertIn("LOOM_TRANSCRIPT_URL", err.getvalue())

    def test_live_with_an_empty_dashboard_says_no_snapshot_yet(self):
        env = {"LOOM_TRANSCRIPT_URL": "https://dashboard.example.com/api/transcripts",
               "LOOM_HANDOFF_TOKEN": "YOUR_HANDOFF_TOKEN_HERE"}
        out = io.StringIO()
        with mock.patch.object(usage, "fetch_live", lambda url, token: {"readings": [], "received_at": None}), \
                redirect_stdout(out):
            code = usage.main(["--live"], env=env, now=1000)
        self.assertEqual(code, 1)
        self.assertIn("holds no usage snapshot yet", out.getvalue())


if __name__ == "__main__":
    unittest.main()
