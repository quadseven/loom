"""The Tiers and allowances page (loom#968): the tier list, one row per
provider and model with its allowance state and hits, and what runs now, from
fixture sources. A source that fails shows its panel as stale with its last
good time; it never reads as zero."""
from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import factory  # noqa: E402
from bus import labels  # noqa: E402
from dashboard import tiers, tiers_sources  # noqa: E402
from dashboard.tiers_sources import SourceState  # noqa: E402
from dashboard.server import _dispatch_get  # noqa: E402
from dashboard.shell import VIEWS  # noqa: E402

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
T = NOW.timestamp()
GOOD_AT = T - 120  # Two minutes before NOW: 11:58 UTC.

# The #968 contract's example, plus a Spark row, a limit-only row (every hit a
# limit: no success rate) and a provider whose breaker is open.
STATS_24H = {
    "window": "24h", "generated_at": int(T),
    "ledger": {"enabled": True, "queued": 0, "written": 9, "dropped": 0, "write_errors": 0,
               "last_error": None, "last_error_at": None},
    "open": [{"target": "groq", "lane": "*", "caller": None, "reason": "rate_limited", "reopens_in_s": 600},
             {"target": "kilo", "lane": "*", "caller": "grug", "reason": "auth", "reopens_in_s": 3600},
             # A breaker on another model of the same target does not touch this row.
             {"target": "spark-a", "lane": "other-model", "caller": None, "reason": "error", "reopens_in_s": 60},
             {"target": "nvidia", "lane": "*", "caller": None, "reason": "benched", "benched": True,
              "reopens_in_s": None}],
    "rows": [
        {"kind": "remote", "target": "ollama-cloud", "model": "nemotron-3-ultra", "hits": 9, "ok": 9,
         "limit": 0, "auth": 0, "missing": 0, "error": 0, "client_error": 0, "success_rate": 1.0,
         "p50_ms": 2310, "p95_ms": 4100, "last_at": int(T) - 10},
        {"kind": "remote", "target": "groq", "model": "llama-5-70b", "hits": 4, "ok": 0, "limit": 4,
         "auth": 0, "missing": 0, "error": 0, "client_error": 0, "success_rate": None,
         "p50_ms": None, "p95_ms": None, "last_at": int(T) - 300},
        {"kind": "spark", "target": "spark-a", "model": "coder-120b", "hits": 20, "ok": 15, "limit": 0,
         "auth": 0, "missing": 0, "error": 5, "client_error": 0, "success_rate": 0.75,
         "p50_ms": 850, "p95_ms": 3000, "last_at": int(T) - 5},
        {"kind": "remote", "target": "kilo", "model": "kilo-coder", "hits": 2, "ok": 0, "limit": 0,
         "auth": 2, "missing": 0, "error": 0, "client_error": 0, "success_rate": 0.0,
         "p50_ms": None, "p95_ms": None, "last_at": int(T) - 900},
    ],
}
STATS_7D = dict(STATS_24H, window="7d", rows=[
    dict(STATS_24H["rows"][0], hits=40, ok=38, success_rate=0.95, p50_ms=2500),
    dict(STATS_24H["rows"][2], hits=100, ok=90, success_rate=0.9, p50_ms=900),
    # Seen this week, not today: its 24-hour figures are a real zero.
    {"kind": "remote", "target": "nvidia", "model": "nim-coder", "hits": 6, "ok": 6, "limit": 0,
     "success_rate": 1.0, "p50_ms": 4000},
])
RUNS = [
    {"lane": "codex", "model": "gpt-6-sol", "outcome": "done", "slots": ["codex", "codex-2"],
     "runs_24h": 3, "runs_7d": 10, "p50_24h_s": 900.0, "p50_7d_s": 1200.0},
    {"lane": "codex", "model": "gpt-6-sol", "outcome": "failed", "slots": ["codex"],
     "runs_24h": 1, "runs_7d": 5, "p50_24h_s": 300.0, "p50_7d_s": 400.0},
    {"lane": "anthropic", "model": "claude-opus-5-5", "outcome": "done", "slots": ["anthropic", "anthropic-2"],
     "runs_24h": 2, "runs_7d": 4, "p50_24h_s": 600.0, "p50_7d_s": 650.0},
    {"lane": "claude", "model": "claude-sonnet-5-5", "outcome": "done", "slots": ["claude"],
     "runs_24h": 1, "runs_7d": 1, "p50_24h_s": 60.0, "p50_7d_s": 60.0},
]
USAGE = [
    # codex: one fresh weekly meter, 70% used.
    {"provider": "codex", "lanes": ["codex", "codex-2"], "window": "weekly", "used_pct": 70.0,
     "resets_at": (NOW + timedelta(days=2)).isoformat(), "source": "fixture",
     "as_of": (NOW - timedelta(minutes=5)).isoformat(), "note": "", "holds": {}},
    # anthropic: every slot held out of credit until 15:00 (the runner holds
    # every slot of a shared credit at once).
    {"provider": "anthropic", "lanes": list(factory.ANTHROPIC), "window": "credit", "used_pct": None,
     "resets_at": None, "source": "fixture", "as_of": (NOW - timedelta(minutes=5)).isoformat(), "note": "",
     "holds": {slot: "limit: out of credit; lane waits until 2026-10-19T15:00:00Z" for slot in factory.ANTHROPIC}},
    # claude: held with no end time: benched with its reason.
    {"provider": "claude", "lanes": ["claude"], "window": "5-hour", "used_pct": None, "resets_at": None,
     "source": "fixture", "as_of": (NOW - timedelta(minutes=5)).isoformat(), "note": "",
     "holds": {"claude": "auth: token expired"}},
]
STATE = {"inflight": [{"id": 7, "ts": T - 42, "target": "spark-a", "path": "/v1/chat/completions",
                       "model": "coder-120b", "resolved_model": "coder-120b", "source": "grug",
                       "cancelling": False, "age_s": 42.0}]}
GRUG = {"inflight": [{"persona": "elder", "repo": "example-org/app", "pr": 7, "model": "model-g",
                      "started_at": "2026-10-19T11:59:00Z"},
                     {"persona": "chief", "repo": "example-org/app", "pr": 8, "model": "model-c",
                      "started_at": "2026-10-19T11:59:30Z"}],
        "tiers": [{"model": "model-g", "backend": "gateway"}, "model-h"]}
STATION = [{"repo": "example-org/app", "number": 12, "title": "Add the export button", "lane": "spark-2",
            "model": "coder-120b", "age": "14m", "html_url": "https://github.com/example-org/app/issues/12"}]


def setUpModule():
    patcher = patch.dict(os.environ, {"LOOM_DASHBOARD_TZ": "UTC"})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def good(data, at=GOOD_AT, name="source"):
    return SourceState(name=name, data=data, last_good_at=at, read_at=at)


def stale(data, error, at=GOOD_AT, name="source"):
    return SourceState(name=name, data=data, last_good_at=at, read_at=T, stale=True, error=error)


def unset(missing, name="source"):
    return SourceState.unconfigured(name, missing)


def sources(**changes):
    out = {"gateway_24h": good(STATS_24H, name="Gateway stats, 24 hours"),
           "gateway_7d": good(STATS_7D, name="Gateway stats, 7 days"),
           "gateway_state": good(STATE, name="Gateway in-flight calls"),
           "grug": good(GRUG, name="Grug stats"),
           "loom_runs": good(RUNS, name="Loom run outcomes"),
           "usage": good(USAGE, name="Usage readings"),
           "station": good(STATION, name="Station tickets")}
    out.update(changes)
    return out


def page(**changes):
    return tiers.render(tiers.build(sources(**changes), NOW))


def panel(body, key):
    """The markup of one panel by its section id."""
    start = body.index('id="tiers-%s"' % key)
    return body[start:body.index("</section>", start)]


def row(rows, provider, model, source=None):
    found = [r for r in rows if r["provider"] == provider and r["model"] == model
             and (source is None or r["source"] == source)]
    if len(found) != 1:
        raise AssertionError("%d rows for %s %s" % (len(found), provider, model))
    return found[0]


class TierList(unittest.TestCase):
    def test_every_tier_is_listed_in_order_then_the_ladder_each_entry_with_lane_model_and_kind(self):
        env = {"LOOM_SPARK_MODEL": "coder-120b", "LOOM_CODEX_MODEL": "gpt-6-luna"}
        listed = tiers.tier_list(env)
        self.assertEqual([t["name"] for t in listed], list(factory.TIERS) + ["ladder"])
        for tier in listed[:-1]:
            self.assertEqual([e["lane"] for e in tier["entries"]], list(factory.TIERS[tier["name"]]))
            for entry in tier["entries"]:
                self.assertEqual(entry["kind"], factory.lane_kind(entry["lane"]))
        hard = {e["lane"]: e for e in listed[2]["entries"]}
        self.assertEqual(hard["codex"]["model"], factory.HARD_MODELS["codex"])
        easy = {e["lane"]: e for e in listed[0]["entries"]}
        medium = {e["lane"]: e for e in listed[1]["entries"]}
        # A lane's own setting, or its provider's for a numbered slot.
        self.assertEqual(easy["spark-2"]["model"], "coder-120b")
        self.assertEqual(medium["codex-2"]["model"], "gpt-6-luna")
        self.assertIsNone(easy["zen"]["model"])
        ladder = listed[-1]["entries"]
        self.assertEqual([(e["rung"], e["lane"], e["model"]) for e in ladder],
                         [(n, lane, model) for n, (lanes, model) in enumerate(factory.LADDER, 1) for lane in lanes])

    def test_the_page_lists_the_tiers_in_order_with_each_kind_and_grugs_order(self):
        body = page()
        listed = panel(body, "tiers")
        positions = [listed.index('id="tier-%s"' % name) for name in list(factory.TIERS) + ["ladder", "grug"]]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("own hardware", listed)
        self.assertIn("never paid", listed)
        grug = listed[listed.index('id="tier-grug"'):]
        self.assertLess(grug.index("model-g"), grug.index("model-h"))

    def test_grugs_order_waits_for_its_endpoint(self):
        body = page(grug=unset("not configured yet: LOOM_GRUG_STATS_URL is not set"))
        self.assertIn("waiting for Grug's endpoint", panel(body, "tiers"))
        body = page(grug=good({"inflight": []}))
        self.assertIn("waiting for Grug's endpoint", panel(body, "tiers"))


def line(body, key):
    match = re.search(r'<tr class="tiers-row[^"]*" data-row="%s">.*?</tr>' % re.escape(key), body, re.S)
    if not match:
        raise AssertionError("no row %s" % key)
    return match.group(0)


class ModelRows(unittest.TestCase):
    def rows(self, **changes):
        s = sources(**changes)
        return tiers.model_rows(s["gateway_24h"], s["gateway_7d"], s["loom_runs"], s["usage"], NOW)

    def test_each_gateway_row_has_its_allowance_hits_success_and_p50(self):
        rows = self.rows()
        nemotron = row(rows, "ollama-cloud", "nemotron-3-ultra")
        self.assertEqual(nemotron["kind"], "free")
        self.assertEqual(nemotron["allowance"]["state"], "available")
        self.assertEqual((nemotron["w24"]["hits"], nemotron["w24"]["success"]), (9, 1.0))
        self.assertEqual(nemotron["w24"]["p50_ms"], 2310)
        self.assertEqual((nemotron["w7d"]["hits"], nemotron["w7d"]["success"]), (40, 0.95))
        spark = row(rows, "spark-a", "coder-120b")
        self.assertEqual(spark["kind"], "own-hardware")
        self.assertEqual(spark["w24"]["success"], 0.75)
        # Its target's breaker on another model does not cool this one.
        self.assertEqual(spark["allowance"]["state"], "available")

    def test_a_limit_only_row_has_no_success_rate_and_cools_until_its_breaker_reopens(self):
        groq = row(self.rows(), "groq", "llama-5-70b")
        self.assertEqual(groq["w24"]["hits"], 4)
        self.assertIsNone(groq["w24"]["success"])
        self.assertEqual(groq["allowance"]["state"], "cooling")
        self.assertEqual(groq["allowance"]["until"], NOW + timedelta(seconds=600))
        text = line(page(), "groq llama-5-70b")
        self.assertIn(">n/a<", text)
        self.assertNotIn("0%", text)
        self.assertIn("cooling until 2026-10-19 12:10 UTC", text)
        self.assertIn("10m breaker window", text)
        self.assertIn("rate_limited", text)

    def test_an_open_breaker_for_another_reason_also_cools_with_its_time_reason_and_caller(self):
        kilo = row(self.rows(), "kilo", "kilo-coder")
        self.assertEqual(kilo["allowance"]["state"], "cooling")
        self.assertEqual(kilo["allowance"]["until"], NOW + timedelta(hours=1))
        self.assertIn("auth", kilo["allowance"]["text"])
        self.assertIn("caller grug", kilo["allowance"]["text"])
        self.assertIn("1h 0m breaker window", kilo["allowance"]["text"])

    def test_benched_is_only_what_the_gateway_reports_benched(self):
        nim = row(self.rows(), "nvidia", "nim-coder")
        self.assertEqual(nim["allowance"]["state"], "benched")
        self.assertIn("benched", nim["allowance"]["text"])

    def test_stale_gateway_stats_claim_no_allowance(self):
        rows = self.rows(gateway_24h=stale(STATS_24H, "answered 503: ledger off"),
                         gateway_7d=stale(STATS_7D, "answered 503: ledger off"))
        for target, model in (("ollama-cloud", "nemotron-3-ultra"), ("groq", "llama-5-70b")):
            allowance = row(rows, target, model)["allowance"]
            self.assertEqual(allowance["state"], "unknown")
            self.assertEqual(allowance["text"], "unknown (stale since 2026-10-19 11:58 UTC)")
        # One fresh window is enough for a current claim.
        rows = self.rows(gateway_7d=stale(STATS_7D, "answered 503: ledger off"))
        self.assertEqual(row(rows, "groq", "llama-5-70b")["allowance"]["state"], "cooling")

    def test_a_row_seen_this_week_only_has_a_real_zero_today(self):
        nim = row(self.rows(), "nvidia", "nim-coder")
        self.assertEqual(nim["w24"]["hits"], 0)
        self.assertEqual(nim["w7d"]["hits"], 6)

    def test_a_missing_window_is_unknown_not_zero(self):
        rows = self.rows(gateway_7d=stale(None, "answered 503: ledger off", at=None))
        nemotron = row(rows, "ollama-cloud", "nemotron-3-ultra")
        self.assertIsNone(nemotron["w7d"])
        self.assertEqual(nemotron["w24"]["hits"], 9)
        body = page(gateway_7d=stale(None, "answered 503: ledger off", at=None))
        self.assertIn("unknown", line(body, "ollama-cloud nemotron-3-ultra"))

    def test_loom_rows_count_runs_per_lane_and_model(self):
        codex = row(self.rows(), "codex", "gpt-6-sol")
        self.assertEqual((codex["unit"], codex["kind"]), ("runs", "subscription"))
        self.assertEqual((codex["w24"]["hits"], codex["w24"]["ok"], codex["w24"]["failed"]), (4, 3, 1))
        # The median of the runs that finished, as the gateway's is of ok calls.
        self.assertEqual(codex["w24"]["p50_ms"], 900000)

    def test_a_loom_failure_may_be_a_limit_so_its_success_rate_is_unknown(self):
        rows = self.rows()
        codex = row(rows, "codex", "gpt-6-sol")
        self.assertIsNone(codex["w24"]["success"])
        self.assertTrue(codex["w24"]["limits_unseparated"])
        text = line(page(), "loom codex gpt-6-sol")
        self.assertIn("unknown (limits not separated)", text)
        self.assertNotIn("75%", text)
        # With no failed run nothing can be a hidden limit: every run finished.
        self.assertEqual(row(rows, "anthropic", "claude-opus-5-5")["w24"]["success"], 1.0)

    def test_every_lane_in_the_tiers_has_a_loom_row_even_with_no_runs(self):
        rows = self.rows(loom_runs=good([RUNS[0]]))
        lanes = {r["provider"] for r in rows if r["source"] == "loom"}
        wanted = {labels.base_lane(lane) for lanes in factory.TIERS.values() for lane in lanes}
        self.assertEqual(wanted - lanes, set())
        anthropic = [r for r in rows if r["source"] == "loom" and r["provider"] == "anthropic"]
        self.assertEqual(len(anthropic), 1)
        # Out of credit and no runs this week: the row that matters most.
        self.assertEqual(anthropic[0]["allowance"]["state"], "cooling")
        self.assertEqual(anthropic[0]["w24"]["hits"], 0)
        self.assertTrue(anthropic[0]["no_runs"])
        self.assertIn("anthropic-8", anthropic[0]["slots"])
        self.assertIn("no runs in 7 days", line(tiers.render(tiers.build(sources(loom_runs=good([RUNS[0]])), NOW)),
                                                 "loom anthropic"))

    def test_with_no_run_outcomes_every_tier_lane_still_has_a_row_with_unknown_cells(self):
        rows = self.rows(loom_runs=stale(None, "database is down", at=None))
        zen = row(rows, "zen", "", source="loom")
        self.assertIsNone(zen["w24"])
        self.assertIsNone(zen["w7d"])
        self.assertEqual(zen["allowance"]["state"], "available")

    def test_loom_allowance_from_the_usage_readings_with_the_window(self):
        rows = self.rows()
        codex = row(rows, "codex", "gpt-6-sol")
        self.assertEqual(codex["allowance"]["state"], "metered")
        self.assertIn("30% left", codex["allowance"]["text"])
        self.assertIn("weekly", codex["allowance"]["text"].lower())
        anthropic = row(rows, "anthropic", "claude-opus-5-5")
        self.assertEqual(anthropic["kind"], "never-paid")
        self.assertEqual(anthropic["allowance"]["state"], "cooling")
        self.assertEqual(anthropic["allowance"]["until"], datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(anthropic["allowance"]["window"], "Credit")
        self.assertIn("cooling until 2026-10-19 15:00 UTC (in 3h 0m), credit window: out of credit",
                      anthropic["allowance"]["text"])
        claude = row(rows, "claude", "claude-sonnet-5-5")
        self.assertEqual(claude["allowance"]["state"], "benched")
        self.assertIn("token expired", claude["allowance"]["text"])

    def test_no_usage_reading_is_an_unknown_allowance(self):
        rows = self.rows(usage=stale(None, "no usage snapshot since the dashboard started", at=None))
        self.assertEqual(row(rows, "codex", "gpt-6-sol")["allowance"]["state"], "unknown")

    def test_a_stale_usage_snapshot_claims_no_allowance(self):
        rows = self.rows(usage=stale(USAGE, "no station upload in 3h"))
        allowance = row(rows, "anthropic", "claude-opus-5-5")["allowance"]
        self.assertEqual(allowance["state"], "unknown")
        self.assertEqual(allowance["text"], "unknown (stale since 2026-10-19 11:58 UTC)")

    def test_gateway_and_loom_rows_are_two_titled_tables(self):
        body = page()
        gateway, loom = panel(body, "gateway"), panel(body, "loom")
        self.assertIn("Gateway calls by target and model", gateway)
        self.assertIn("Loom runs by lane and model", loom)
        self.assertIn('data-row="spark-a coder-120b"', gateway)
        self.assertNotIn('data-row="loom ', gateway)
        self.assertIn('data-row="loom spark"', loom)
        self.assertNotIn('data-row="spark-a', loom)


class StalePanels(unittest.TestCase):
    """Stopping any one source shows that panel as stale with its last good
    time, and the rest of the page still renders (one test per source)."""

    def assertRestRenders(self, body, *, except_panel):
        for key in ("tiers", "gateway", "loom", "live"):
            if key != except_panel:
                self.assertIn('id="tiers-%s"' % key, body)
        self.assertIn("Add the export button", panel(body, "live"))

    def test_gateway_stats_503_shows_the_models_panel_stale_with_its_last_good_rows(self):
        body = page(gateway_24h=stale(STATS_24H, "answered 503: ledger database unavailable",
                                      name="Gateway stats, 24 hours"))
        models = panel(body, "gateway")
        self.assertIn("Stale", models)
        self.assertIn("Gateway stats, 24 hours", models)
        self.assertIn("ledger database unavailable", models)
        self.assertIn("last good read 2026-10-19 11:58 UTC", models)
        self.assertIn('data-row="ollama-cloud nemotron-3-ultra"', models)
        self.assertRestRenders(body, except_panel="gateway")

    def test_gateway_state_timeout_shows_the_gateway_live_panel_stale(self):
        body = page(gateway_state=stale(None, "timed out after 5s", at=None, name="Gateway in-flight calls"))
        live = panel(body, "live")
        gateway = live[live.index('id="live-gateway"'):live.index('id="live-grug"')]
        self.assertIn("Stale", gateway)
        self.assertIn("timed out after 5s", gateway)
        self.assertIn("never read", gateway)
        self.assertNotIn("No calls in flight", gateway)
        self.assertIn("Add the export button", live)
        self.assertIn("elder", live)

    def test_grug_unset_says_not_configured_yet_not_zero(self):
        body = page(grug=unset("not configured yet: LOOM_GRUG_STATS_URL is not set", name="Grug stats"))
        live = panel(body, "live")
        grug = live[live.index('id="live-grug"'):]
        self.assertIn("not configured yet", grug)
        self.assertNotIn("No reviews in flight", grug)
        self.assertRestRenders(body, except_panel=None)

    def test_grug_erroring_shows_its_live_panel_stale_with_the_last_good_reviews(self):
        body = page(grug=stale(GRUG, "answered 500: boom", name="Grug stats"))
        live = panel(body, "live")
        grug = live[live.index('id="live-grug"'):]
        self.assertIn("Stale", grug)
        self.assertIn("answered 500: boom", grug)
        self.assertIn("last good read 2026-10-19 11:58 UTC", grug)
        self.assertIn("example-org/app#7", grug)

    def test_transcripts_unavailable_shows_loom_runs_stale_and_the_gateway_rows_still_render(self):
        body = page(loom_runs=stale(None, "database is down", at=None, name="Loom run outcomes"))
        loom = panel(body, "loom")
        self.assertIn("Loom run outcomes", loom)
        self.assertIn("database is down", loom)
        self.assertNotIn('data-row="codex gpt-6-sol"', loom)
        self.assertIn("unknown", line(body, "loom codex"))
        self.assertIn('data-row="ollama-cloud nemotron-3-ultra"', panel(body, "gateway"))
        self.assertRestRenders(body, except_panel="loom")

    def test_station_tickets_stale_shows_the_loom_live_panel_stale(self):
        body = page(station=stale(STATION, "GitHub rate limit", name="Station tickets"))
        live = panel(body, "live")
        loom = live[live.index('id="live-loom"'):live.index('id="live-gateway"')]
        self.assertIn("Stale", loom)
        self.assertIn("GitHub rate limit", loom)
        self.assertIn("Add the export button", loom)

    def test_the_gateway_unset_says_which_setting_is_missing(self):
        missing = unset("LOOM_GATEWAY_URL is not set")
        body = page(gateway_24h=missing, gateway_7d=missing, gateway_state=missing)
        self.assertIn("LOOM_GATEWAY_URL is not set", panel(body, "gateway"))
        self.assertIn("LOOM_GATEWAY_URL is not set", panel(body, "live"))


class LiveNow(unittest.TestCase):
    def test_live_now_shows_a_loom_run_a_gateway_call_and_a_grug_review(self):
        s = sources()
        live = tiers.live_now(s["station"], s["gateway_state"], s["grug"], NOW)
        self.assertEqual(live["loom"]["source"].name, "Station tickets")
        self.assertEqual(live["loom"]["items"][0]["lane"], "spark-2")
        self.assertEqual(live["gateway"]["items"][0]["target"], "spark-a")
        self.assertEqual(live["grug"]["items"][0]["persona"], "elder")
        body = panel(page(), "live")
        self.assertIn("example-org/app#12", body)
        self.assertIn("spark-2", body)
        self.assertIn("coder-120b", body)
        self.assertIn("spark-a", body)
        self.assertIn("42s", body)
        self.assertIn("elder", body)
        self.assertIn("example-org/app#7", body)
        self.assertIn("model-g", body)

    def test_grug_live_shows_elder_reviews_only(self):
        s = sources()
        live = tiers.live_now(s["station"], s["gateway_state"], s["grug"], NOW)
        self.assertEqual([item["persona"] for item in live["grug"]["items"]], ["elder"])
        body = panel(page(), "live")
        self.assertNotIn("model-c", body)
        self.assertNotIn("example-org/app#8", body)

    def test_grug_entries_are_read_defensively(self):
        grug = good({"inflight": [{"persona": "Elder", "pr": "x", "model": "<b>m</b>"}, "junk", None,
                                  {"repo": "example-org/app", "pr": 3, "started_at": "not a time"}]})
        live = tiers.live_now(good([]), good({"inflight": []}), grug, NOW)
        self.assertEqual(len(live["grug"]["items"]), 2)
        body = tiers.render(tiers.build(sources(grug=grug), NOW))
        self.assertIn("&lt;b&gt;m&lt;/b&gt;", body)
        self.assertNotIn("<b>m</b>", body)

    def test_nothing_in_flight_is_said_plainly(self):
        body = panel(page(gateway_state=good({"inflight": []}), station=good([]), grug=good({"inflight": []})), "live")
        self.assertIn("No calls in flight", body)
        self.assertIn("No reviews in flight", body)
        self.assertIn("No Loom run", body)


class Route(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"LOOM_DASHBOARD_TZ": "UTC"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tiers_is_in_the_nav_next_to_usage(self):
        keys = [key for key, _, _ in VIEWS]
        self.assertEqual(keys[keys.index("usage") + 1], "tiers")
        self.assertIn(("tiers", "Tiers and allowances", "/tiers"), VIEWS)

    def test_the_route_renders_from_the_background_snapshot_and_reads_nothing(self):
        class TierSources:
            calls = 0

            def snapshot(self):
                return {key: value for key, value in sources().items() if key not in ("usage", "station")}

            def refresh(self, now=None):
                raise AssertionError("a page request must not read a source")

        class State:
            stale = False
            tier_sources = TierSources()
            usage = None

            def response(self):
                return {"tickets": {"example-org/app": [
                    {"repo": "example-org/app", "number": 12, "title": "Add the export button", "state": "running",
                     "open": True, "lane": "spark-2", "model": None, "updated_at": "2026-10-19T11:50:00Z",
                     "html_url": "https://github.com/example-org/app/issues/12", "labels": []},
                    {"repo": "example-org/app", "number": 13, "title": "Merged one", "state": "merged",
                     "open": True, "lane": "codex", "model": None}]},
                    "stale": False, "pull_requests": [], "issue_comments": [], "latest_runs": {},
                    "state_entered": {}, "ticket_refreshed_at": T}

        class Handler:
            path = "/tiers"

            def send_page(self, body, code=200):
                self.result = (code, body)

        handler = Handler()
        _dispatch_get(handler, State())
        code, body = handler.result
        self.assertEqual(code, 200)
        self.assertIn("<title>Tiers and allowances - Loom</title>", body)
        self.assertIn('href="/tiers" aria-current="page"', body)
        live = panel(body, "live")
        self.assertIn("example-org/app#12", live)
        self.assertNotIn("Merged one", live)
        self.assertIn("spark-a", live)
        # No usage snapshot has arrived: allowance is unknown, not available.
        loom = panel(body, "loom")
        self.assertIn("no usage snapshot", loom)
        self.assertIn("unknown", line(body, "loom anthropic claude-opus-5-5"))

    def test_a_state_without_background_sources_still_renders(self):
        class State:
            stale = False

            def response(self):
                return {"tickets": {}, "stale": False}

        class Handler:
            path = "/tiers"

            def send_page(self, body, code=200):
                self.result = (code, body)

        handler = Handler()
        with patch.dict(os.environ, {"LOOM_GATEWAY_URL": ""}):
            _dispatch_get(handler, State())
        self.assertEqual(handler.result[0], 200)
        self.assertIn("LOOM_GATEWAY_URL is not set", handler.result[1])


class OneRefresh(unittest.TestCase):
    """Live now shows a Loom run, a gateway call and a Grug review within one
    refresh pass of the background sources."""

    def test_one_refresh_fills_live_now(self):
        try:
            from test_dashboard_tiers_sources import STATE as UP_STATE, GRUG as UP_GRUG, STATS, Upstream
        except ImportError:
            from tests.test_dashboard_tiers_sources import STATE as UP_STATE, GRUG as UP_GRUG, STATS, Upstream
        upstream = Upstream()
        self.addCleanup(upstream.close)
        upstream.routes = {"/ui/stats?window=24h": (200, STATS, 0), "/ui/stats?window=7d": (200, STATS, 0),
                           "/ui/state": (200, UP_STATE, 0), "/grug/stats": (200, UP_GRUG, 0)}
        env = {"LOOM_GATEWAY_URL": upstream.base, "LOOM_GRUG_STATS_URL": upstream.base + "/grug/stats"}
        background = tiers_sources.TierSources(env=env, runs_store=lambda: None, timeout=0.5)
        before = tiers.render(tiers.build(dict(background.snapshot(now=T), station=good(STATION)), NOW))
        self.assertNotIn("spark-a", panel(before, "live"))
        background.refresh(now=T)
        after = panel(tiers.render(tiers.build(dict(background.snapshot(now=T), station=good(STATION)), NOW)),
                      "live")
        self.assertIn("example-org/app#12", after)
        self.assertIn("spark-a", after)
        self.assertIn("elder", after)


if __name__ == "__main__":
    unittest.main()
