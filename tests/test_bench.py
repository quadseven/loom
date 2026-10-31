"""Bench a model for everyone after repeated failure (loom#969): the decision
from the gateway's call records, the doubling cooldown, the one trial call,
the published list, and Loom's skipping of a benched pair."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import bench, factory, station  # noqa: E402
from dashboard import bench_store, tiers  # noqa: E402
from dashboard.server import _dispatch_get  # noqa: E402
from dashboard.tiers_sources import SourceState  # noqa: E402

T0 = 1_800_000_000
CFG = bench.Config(threshold=0.5, min_sample=10, cooldown=3600, max_cooldown=86400)


def row(target="free-kilo", model="kilo-coder", hits=0, ok=0, limit=0, **more):
    return dict({"target": target, "model": model, "hits": hits, "ok": ok, "limit": limit}, **more)


def run(rows, state, now, cfg=CFG):
    return bench.evaluate(rows, state, now, cfg)


def kinds(events):
    return [e["kind"] for e in events]


class Decision(unittest.TestCase):
    def test_failures_over_the_threshold_bench_the_pair_with_reason_and_return_time(self):
        state, events = run([row(hits=20, ok=8)], None, T0)
        self.assertEqual(kinds(events), ["bench"])
        entry = state["models"]["free-kilo|kilo-coder"]
        self.assertEqual((entry["status"], entry["until"], entry["level"]), ("benched", T0 + 3600, 1))
        self.assertIn("failed 12 of 20 calls (60%)", entry["reason"])
        shown = bench.snapshot(state, T0)["benched"]
        self.assertEqual([(b["provider"], b["model"], b["until"]) for b in shown],
                         [("free-kilo", "kilo-coder", T0 + 3600)])

    def test_a_small_sample_or_a_low_rate_does_not_bench(self):
        for rows in ([row(hits=9, ok=0)], [row(hits=20, ok=11)]):
            state, events = run(rows, None, T0)
            self.assertEqual(events, [])
            self.assertEqual(state["models"], {})

    def test_limit_refusals_never_count_toward_a_bench(self):
        # Every call a limit: nothing failed. Mostly limits with a few ok.
        for rows in ([row(hits=40, ok=0, limit=40)], [row(hits=40, ok=3, limit=37)],
                     [row(hits=40, ok=10, limit=30)]):
            _, events = run(rows, None, T0)
            self.assertEqual(events, [], rows)
        # Limits leave the denominator: 12 failed of 20 non-limit calls benches.
        _, events = run([row(hits=50, ok=8, limit=30)], None, T0)
        self.assertEqual(kinds(events), ["bench"])

    def test_client_errors_are_the_callers_not_the_models(self):
        _, events = run([row(hits=30, ok=0, client_error=30)], None, T0)
        self.assertEqual(events, [])

    def test_a_benched_pair_stays_benched_until_the_cooldown_ends(self):
        state, _ = run([row(hits=20, ok=0)], None, T0)
        state, events = run([row(hits=25, ok=0)], state, T0 + 3599)
        self.assertEqual(events, [])
        self.assertEqual(len(bench.snapshot(state, T0 + 3599)["benched"]), 1)

    def test_the_input_state_is_not_changed(self):
        state, _ = run([row(hits=20, ok=0)], None, T0)
        before = repr(state)
        run([row(hits=20, ok=0)], state, T0 + 4000)
        self.assertEqual(repr(state), before)


class Trial(unittest.TestCase):
    def benched(self, rows=None):
        return run(rows or [row(hits=20, ok=0)], None, T0)[0]

    def test_after_the_cooldown_the_pair_is_on_trial_and_not_benched(self):
        state = self.benched()
        state, events = run([row(hits=20, ok=0)], state, T0 + 3600)
        self.assertEqual(kinds(events), ["trial"])
        shown = bench.snapshot(state, T0 + 3600)
        self.assertEqual((shown["benched"], [t["model"] for t in shown["trial"]]), ([], ["kilo-coder"]))

    def test_a_successful_trial_call_returns_the_pair_and_old_failures_do_not_bench_it_again(self):
        state = self.benched()
        state, _ = run([row(hits=20, ok=0)], state, T0 + 3600)
        state, events = run([row(hits=21, ok=1)], state, T0 + 3700)
        self.assertEqual(kinds(events), ["return"])
        self.assertEqual(state["models"], {})
        # The window still holds 20 failures, but they were forgiven.
        state, events = run([row(hits=21, ok=1)], state, T0 + 3800)
        self.assertEqual(events, [])
        # Fresh failures after the return count again.
        state, events = run([row(hits=41, ok=1)], state, T0 + 3900)
        self.assertEqual(kinds(events), ["bench"])

    def test_a_failed_trial_call_benches_the_pair_for_twice_as_long(self):
        state = self.benched()
        state, _ = run([row(hits=20, ok=0)], state, T0 + 3600)
        state, events = run([row(hits=21, ok=0)], state, T0 + 3700)
        self.assertEqual(kinds(events), ["rebench"])
        entry = state["models"]["free-kilo|kilo-coder"]
        self.assertEqual((entry["status"], entry["level"], entry["cooldown"], entry["until"]),
                         ("benched", 2, 7200, T0 + 3700 + 7200))
        # And again: 4 hours, then capped at the maximum.
        state, _ = run([row(hits=21, ok=0)], state, T0 + 3700 + 7200)
        state, _ = run([row(hits=22, ok=0)], state, T0 + 3700 + 7300)
        self.assertEqual(state["models"]["free-kilo|kilo-coder"]["cooldown"], 14400)
        for n in range(4):
            until = state["models"]["free-kilo|kilo-coder"]["until"]
            state, _ = run([row(hits=22 + n, ok=0)], state, until)
            state, _ = run([row(hits=23 + n, ok=0)], state, until + 1)
        self.assertEqual(state["models"]["free-kilo|kilo-coder"]["cooldown"], 86400)

    def test_calls_made_during_the_cooldown_are_not_the_trials(self):
        state = self.benched()
        # A caller that ignored the bench failed 5 more calls in the cooldown.
        state, _ = run([row(hits=25, ok=0)], state, T0 + 1000)
        state, _ = run([row(hits=25, ok=0)], state, T0 + 3600)
        state, events = run([row(hits=26, ok=1)], state, T0 + 3700)
        self.assertEqual(kinds(events), ["return"])

    def test_a_trial_with_no_call_yet_waits(self):
        state = self.benched()
        state, _ = run([row(hits=20, ok=0)], state, T0 + 3600)
        state, events = run([row(hits=20, ok=0)], state, T0 + 9000)
        self.assertEqual(events, [])
        self.assertEqual(state["models"]["free-kilo|kilo-coder"]["status"], "trial")

    def test_a_limit_refusal_during_the_trial_decides_nothing(self):
        state = self.benched()
        state, _ = run([row(hits=20, ok=0)], state, T0 + 3600)
        state, events = run([row(hits=21, ok=0, limit=1)], state, T0 + 3700)
        self.assertEqual(events, [])
        self.assertEqual(state["models"]["free-kilo|kilo-coder"]["status"], "trial")

    def test_no_gateway_answer_moves_only_the_clock(self):
        state = self.benched()
        state, events = run(None, state, T0 + 3600)
        self.assertEqual(kinds(events), ["trial"])
        state, events = run(None, state, T0 + 3700)
        self.assertEqual(events, [])

    def test_a_pair_that_returned_long_ago_starts_at_the_first_cooldown_again(self):
        state = self.benched()
        state, _ = run([row(hits=20, ok=0)], state, T0 + 3600)
        state, _ = run([row(hits=21, ok=1)], state, T0 + 3700)
        later = T0 + 3700 + 86400 + 1
        state, events = run([row(hits=61, ok=1)], state, later)
        self.assertEqual(kinds(events), ["bench"])
        self.assertEqual(state["models"]["free-kilo|kilo-coder"]["level"], 1)


class Published(unittest.TestCase):
    def test_benched_pairs_skip_a_stale_or_ended_list(self):
        state, _ = run([row(hits=20, ok=0)], None, T0)
        listed = dict(bench.snapshot(state, T0), fetched_at=T0)
        self.assertEqual(bench.benched_pairs(listed, T0 + 60), [("free-kilo", "kilo-coder")])
        self.assertEqual(bench.benched_pairs(listed, T0 + bench.LIST_TTL + 1), [])
        # The cooldown ended, so the pair is on trial and not skipped.
        self.assertEqual(bench.benched_pairs(listed, T0 + 3600), [])
        self.assertEqual(bench.benched_pairs(None, T0), [])
        self.assertEqual(bench.benched_pairs({"benched": [{"provider": "x"}]}, T0), [])

    def test_a_pair_matches_the_lane_and_its_numbered_slots(self):
        pairs = [("spark", "coder-120b")]
        self.assertTrue(bench.is_benched(pairs, "spark", "coder-120b"))
        self.assertTrue(bench.is_benched(pairs, "spark-3", "coder-120b"))
        self.assertFalse(bench.is_benched(pairs, "spark-3", "other-model"))
        self.assertFalse(bench.is_benched(pairs, "codex", "coder-120b"))
        self.assertFalse(bench.is_benched(pairs, "spark", None))

    def test_digest_lines_report_each_bench_and_return(self):
        state, _ = run([row(hits=20, ok=0)], None, T0)
        state, _ = run([row(hits=20, ok=0)], state, T0 + 3600)
        state, _ = run([row(hits=21, ok=1)], state, T0 + 3700)
        lines = bench.digest_lines(bench.snapshot(state, T0 + 3700), T0 - 1, T0 + 3700)
        self.assertEqual(len(lines), 3)
        self.assertIn("benched (failed 20 of 20 calls (100%), threshold 50%)", lines[0])
        self.assertTrue(lines[1].endswith("on trial"))
        self.assertTrue(lines[2].endswith("back"))
        self.assertEqual(bench.digest_lines(bench.snapshot(state, T0 + 3700), T0 + 3700, T0 + 3700), [])


class Config(unittest.TestCase):
    def test_settings_from_the_environment_and_bad_values_keep_the_default(self):
        cfg = bench.Config.from_env({"LOOM_BENCH_THRESHOLD": "0.8", "LOOM_BENCH_MIN_SAMPLE": "30",
                                     "LOOM_BENCH_COOLDOWN_SECONDS": "60",
                                     "LOOM_BENCH_MAX_COOLDOWN_SECONDS": "oops"})
        self.assertEqual((cfg.threshold, cfg.min_sample, cfg.cooldown, cfg.max_cooldown), (0.8, 30, 60, 86400))
        cfg = bench.Config.from_env({"LOOM_BENCH_THRESHOLD": "7", "LOOM_BENCH_MIN_SAMPLE": "-1"})
        self.assertEqual((cfg.threshold, cfg.min_sample), (bench.THRESHOLD, bench.MIN_SAMPLE))


class Store(unittest.TestCase):
    def source(self, rows, stale=False):
        return {"gateway_24h": SourceState(name="g", data={"rows": rows}, last_good_at=T0, read_at=T0,
                                           stale=stale)}

    def test_the_store_decides_from_the_gateway_rows_and_keeps_state_across_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bench.json")
            store = bench_store.BenchStore(path, CFG)
            events = store.update(self.source([row(hits=20, ok=0)]), T0)
            self.assertEqual(kinds(events), ["bench"])
            again = bench_store.BenchStore(path, CFG)
            self.assertEqual([b["model"] for b in again.snapshot(T0 + 1)["benched"]], ["kilo-coder"])

    def test_a_stale_gateway_decides_nothing_new(self):
        store = bench_store.BenchStore("", CFG)
        self.assertEqual(store.update(self.source([row(hits=20, ok=0)], stale=True), T0), [])
        self.assertEqual(store.update({}, T0), [])
        self.assertEqual(store.snapshot(T0)["benched"], [])

    def test_a_state_of_the_wrong_shape_does_not_end_the_pass(self):
        store = bench_store.BenchStore("", CFG)
        store.state = {"models": {"free-kilo|kilo-coder": 7}, "events": []}
        with self.assertLogs("dashboard.bench_store", level="WARNING"):
            store.update(self.source([row(hits=20, ok=0)]), T0)
        self.assertEqual(store.snapshot(T0)["benched"], [])


class Endpoint(unittest.TestCase):
    def get(self, state, auth):
        handler = type("Handler", (), {})()
        handler.path = "/api/bench"
        handler.headers = Message()
        if auth:
            handler.headers["Authorization"] = auth
        result = []
        handler.send_json = lambda code, value: result.append((code, value))
        handler.send_text = lambda code, value: result.append((code, value))
        _dispatch_get(handler, state)
        return result[0]

    def test_get_api_bench_serves_the_list_to_the_read_tokens_only(self):
        class State:
            pass

        state = State()
        state.bench = bench_store.BenchStore("", CFG)
        now = 1_800_000_000
        with patch("time.time", return_value=now):
            state.bench.update({"gateway_24h": SourceState(name="g", data={"rows": [row(hits=20, ok=0)]},
                                                           last_good_at=now, read_at=now)}, now)
            with patch.dict(os.environ, {"LOOM_HANDOFF_TOKEN": "YOUR_HANDOFF_TOKEN_HERE",
                                         "LOOM_UPLOAD_TOKEN": "YOUR_UPLOAD_TOKEN_HERE"}):
                self.assertEqual(self.get(state, "")[0], 401)
                self.assertEqual(self.get(state, "Bearer wrong")[0], 401)
                code, listed = self.get(state, "Bearer YOUR_HANDOFF_TOKEN_HERE")
                self.assertEqual(self.get(state, "Bearer YOUR_UPLOAD_TOKEN_HERE")[0], 200)
        self.assertEqual(code, 200)
        self.assertEqual([(b["provider"], b["model"]) for b in listed["benched"]], [("free-kilo", "kilo-coder")])
        self.assertEqual(kinds(listed["events"]), ["bench"])


class StationReads(unittest.TestCase):
    """The station keeps the dashboard's bench list and hands it to the factory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = station.Settings(
            self.temp.name, ["example-org/app"], ["alice-example"], [],
            transcript_url="https://dashboard.example.com/api/transcripts",
            transcript_token_ssm="/loom/upload-token")
        self.fetched = []
        self.answer = {}

    def fetch(self, url, token):
        self.fetched.append((url, token))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def station(self, now=T0):
        return station.Station(object(), self.settings, now=now, read_secret=lambda name, region: "tok",
                               fetch=self.fetch)

    def listed(self, until=T0 + 3600):
        return {"generated_at": T0, "events": [], "trial": [],
                "benched": [{"provider": "codex", "model": "gpt-6-sol", "status": "benched",
                             "since": T0, "until": until, "level": 1, "reason": "failed 9 of 10 calls"}]}

    def test_the_list_is_read_from_the_dashboard_and_kept(self):
        self.answer = self.listed()
        st = self.station()
        st.refresh_bench()
        self.assertEqual(self.fetched, [("https://dashboard.example.com/api/bench", "tok")])
        self.assertEqual(st.benched_pairs(), [("codex", "gpt-6-sol")])

    def test_a_dashboard_that_does_not_answer_keeps_the_last_list_until_it_ages_out(self):
        self.answer = self.listed()
        self.station().refresh_bench()
        self.answer = OSError("the handoff read failed to connect")
        later = self.station(T0 + 600)
        later.refresh_bench()
        self.assertEqual(later.benched_pairs(), [("codex", "gpt-6-sol")])
        self.assertEqual(self.station(T0 + bench.LIST_TTL + 1).benched_pairs(), [])

    def test_a_list_the_dashboard_no_longer_holds_lets_the_model_back(self):
        self.answer = self.listed()
        self.station().refresh_bench()
        self.answer = dict(self.listed(), benched=[])
        st = self.station(T0 + 60)
        st.refresh_bench()
        self.assertEqual(st.benched_pairs(), [])

    def test_the_factory_gets_the_benched_pairs_and_the_lanes_whose_default_model_is_benched(self):
        self.answer = self.listed()
        st = self.station()
        st.refresh_bench()
        with patch.dict(os.environ, {"LOOM_CODEX_MODEL": "gpt-6-sol", "LOOM_CLAUDE_MODEL": "claude-opus-5-5"}):
            worker = st._factory()
        self.assertEqual(worker.benched, [("codex", "gpt-6-sol")])
        self.assertIn("codex", worker.down)
        self.assertIn("codex-2", worker.down)
        self.assertNotIn("claude", worker.down)
        self.assertFalse(worker._serves("codex-2", "gpt-6-sol"))
        self.assertTrue(worker._serves("codex-2", "gpt-6-luna"))

    def test_nothing_is_read_without_the_dashboard_settings(self):
        self.settings.transcript_url = ""
        self.station().refresh_bench()
        self.assertEqual(self.fetched, [])


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


class Page(unittest.TestCase):
    def render(self, listed):
        gateway = SourceState(name="g", data={"generated_at": int(NOW.timestamp()), "rows": [
            dict(row(hits=20, ok=0), kind="remote")]}, last_good_at=NOW.timestamp(), read_at=NOW.timestamp())
        sources = {"gateway_24h": gateway, "gateway_7d": gateway}
        return tiers.render(tiers.build(sources, NOW, bench=listed))

    def gateway_section(self, body):
        gateway = body[body.index('id="tiers-gateway"'):]
        return gateway[:gateway.index("</section>")]

    def listed(self):
        now = NOW.timestamp()
        state, _ = run([row(hits=20, ok=0)], None, now)
        return bench.snapshot(state, now)

    def test_a_benched_model_shows_on_the_page_with_its_reason_and_return_time(self):
        body = self.render(self.listed())
        section = body[body.index('id="tiers-bench"'):]
        section = section[:section.index("</section>")]
        self.assertIn("kilo-coder", section)
        self.assertIn("failed 20 of 20 calls (100%), threshold 50%", section)
        self.assertIn("back 2026-10-19 13:00 UTC", section)
        self.assertIn("benched", section)
        # The gateway row reads benched too, with the same reason.
        gateway = self.gateway_section(body)
        self.assertIn("tiers-benched", gateway)
        self.assertIn("benched for everyone: failed 20 of 20 calls", gateway)

    def test_no_bench_list_or_an_empty_one_says_so(self):
        self.assertIn("not kept by this dashboard", self.render(None))
        empty = self.render(bench.snapshot(None, NOW.timestamp()))
        self.assertIn("No model is benched.", empty)
        self.assertNotIn("benched for everyone", self.gateway_section(empty))

    def test_a_pair_on_trial_is_listed_and_not_benched(self):
        now = NOW.timestamp()
        state, _ = run([row(hits=20, ok=0)], None, now - 3600)
        state, _ = run([row(hits=20, ok=0)], state, now)
        body = self.render(bench.snapshot(state, now))
        self.assertIn("on trial", body)
        self.assertNotIn("benched for everyone", self.gateway_section(body))

    def test_loom_rows_of_the_lane_read_benched(self):
        listed = {"benched": [{"provider": "codex", "model": "gpt-6-sol", "reason": "failed 9 of 10 calls",
                               "until": NOW.timestamp() + 600, "level": 1}], "trial": [], "events": []}
        gateway = SourceState(name="g", data={"generated_at": int(NOW.timestamp()), "rows": []},
                              last_good_at=NOW.timestamp(), read_at=NOW.timestamp())
        runs = SourceState(name="r", data=[{"lane": "codex", "model": "gpt-6-sol", "outcome": "done",
                                            "slots": ["codex", "codex-2"], "runs_24h": 1, "runs_7d": 1}],
                           last_good_at=NOW.timestamp(), read_at=NOW.timestamp())
        data = tiers.build({"gateway_24h": gateway, "gateway_7d": gateway, "loom_runs": runs}, NOW, bench=listed)
        codex = [r for r in data["loom_rows"] if r["provider"] == "codex" and r["model"] == "gpt-6-sol"]
        self.assertEqual(codex[0]["allowance"]["state"], "benched")
        other = [r for r in data["loom_rows"] if r["provider"] == "claude"]
        self.assertNotEqual(other[0]["allowance"]["state"], "benched")


if __name__ == "__main__":
    unittest.main()
