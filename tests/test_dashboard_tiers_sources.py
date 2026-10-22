"""The Tiers and allowances page's background reads (loom#968): each source
keeps its last good answer and the time it was read, and a failed read is
stale, never zero."""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import tiers_sources  # noqa: E402
from tests import sandbox_caps  # noqa: E402
from tests.local_http import LocalHTTPServer  # noqa: E402

STATS = {"window": "24h", "generated_at": 1792421800,
         "ledger": {"enabled": True, "queued": 0, "written": 9, "dropped": 0,
                    "write_errors": 0, "last_error": None, "last_error_at": None},
         "open": [], "rows": [{"kind": "remote", "target": "ollama-cloud", "model": "nemotron-3-ultra",
                               "hits": 9, "ok": 9, "limit": 0, "success_rate": 1.0, "p50_ms": 2310}]}
STATE = {"ts": 1792421800, "inflight": [{"id": 4, "ts": 1792421790, "target": "spark-a", "path": "/v1/chat",
                                         "model": "coder", "resolved_model": "coder", "source": "loom",
                                         "cancelling": False, "age_s": 10.0}]}
GRUG = {"inflight": [{"persona": "elder", "repo": "example-org/app", "pr": 7, "model": "model-g",
                      "started_at": "2026-10-19T11:59:00Z"}],
        "tiers": ["model-g", "model-h"]}


class Upstream:
    """A local HTTP server standing in for the gateway and Grug: each path
    answers what `routes` holds now, so a test can stop one source."""

    def __init__(self):
        self.routes = {}
        self.seen = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                upstream.seen.append((self.path, self.headers.get("Authorization")))
                code, body, delay = upstream.routes.get(self.path, (404, {"error": "no route"}, 0))
                if delay:
                    time.sleep(delay)
                raw = json.dumps(body).encode()
                self.send_response(code)
                if code in (301, 302):
                    self.send_header("Location", "/ui/state")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

            def log_message(self, fmt, *args):
                pass

        self.server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeRuns:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows or [], error

    def model_outcomes(self, now=None):
        if self.error:
            raise self.error
        return self.rows


class SourceTests(unittest.TestCase):
    def test_a_failed_read_keeps_the_last_good_data_and_its_time(self):
        answers = [{"rows": []}]

        def fetch():
            if isinstance(answers[0], Exception):
                raise answers[0]
            return answers[0]

        source = tiers_sources.Source("gateway stats (24h)", fetch)
        before = source.envelope(now=100.0)
        self.assertTrue(before.stale)
        self.assertIsNone(before.last_good_at)
        self.assertIn("not read yet", before.error)
        source.refresh(now=100.0)
        good = source.envelope(now=110.0)
        self.assertEqual((good.stale, good.data, good.last_good_at, good.read_at, good.error),
                         (False, {"rows": []}, 100.0, 100.0, ""))
        answers[0] = OSError("connection refused")
        source.refresh(now=200.0)
        failed = source.envelope(now=210.0)
        self.assertTrue(failed.stale)
        self.assertEqual(failed.data, {"rows": []})
        self.assertEqual(failed.last_good_at, 100.0)
        self.assertEqual(failed.read_at, 200.0)
        self.assertIn("connection refused", failed.error)

    def test_a_good_answer_turns_stale_when_reads_stop(self):
        source = tiers_sources.Source("gateway state", lambda: {"inflight": []})
        source.refresh(now=100.0)
        old = source.envelope(now=100.0 + tiers_sources.STALE_AFTER + 1)
        self.assertTrue(old.stale)
        self.assertIn("no read since", old.error)

    def test_a_secret_in_an_error_never_reaches_the_envelope(self):
        def fetch():
            raise OSError("upstream echoed Bearer YOUR_GRUG_TOKEN_HERE back")
        source = tiers_sources.Source("Grug stats", fetch, secrets=("YOUR_GRUG_TOKEN_HERE",))
        with self.assertLogs(tiers_sources.LOG, "WARNING") as logged:
            source.refresh(now=100.0)
        envelope = source.envelope(now=100.0)
        self.assertNotIn("YOUR_GRUG_TOKEN_HERE", envelope.error)
        self.assertIn("[redacted]", envelope.error)
        self.assertNotIn("YOUR_GRUG_TOKEN_HERE", "".join(logged.output))

    def test_only_http_and_https_urls_are_read(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(tiers_sources.SourceError, "http"):
                    tiers_sources.fetch_json(url)
        sources = tiers_sources.TierSources(env={"LOOM_GATEWAY_URL": "file:///etc",
                                                 "LOOM_GRUG_STATS_URL": "file:///etc/passwd"},
                                            runs_store=lambda: FakeRuns())
        snap = sources.snapshot(now=1.0)
        self.assertFalse(snap["gateway_state"].configured)
        self.assertIn("not an http(s) URL", snap["gateway_state"].missing)
        self.assertFalse(snap["grug"].configured)

    def test_an_unconfigured_source_is_not_stale_and_says_what_to_set(self):
        source = tiers_sources.Source("Grug stats", None, missing="LOOM_GRUG_STATS_URL is not set")
        source.refresh(now=100.0)
        envelope = source.envelope(now=100.0)
        self.assertEqual((envelope.configured, envelope.stale, envelope.data), (False, False, None))
        self.assertEqual(envelope.missing, "LOOM_GRUG_STATS_URL is not set")


@unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
class FetchTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)
        self.upstream.routes = {
            "/ui/stats?window=24h": (200, STATS, 0),
            "/ui/stats?window=7d": (200, dict(STATS, window="7d"), 0),
            "/ui/state": (200, STATE, 0),
            "/grug/stats": (200, GRUG, 0),
        }
        self.env = {"LOOM_GATEWAY_URL": self.upstream.base + "/",
                    "LOOM_GRUG_STATS_URL": self.upstream.base + "/grug/stats",
                    "LOOM_GRUG_STATS_TOKEN": "YOUR_GRUG_TOKEN_HERE"}

    def sources(self, runs=None, env=None):
        return tiers_sources.TierSources(env=self.env if env is None else env,
                                         runs_store=lambda: runs or FakeRuns(), timeout=0.5)

    def test_one_refresh_reads_every_source(self):
        sources = self.sources(FakeRuns([{"lane": "codex", "model": "model-x", "outcome": "done"}]))
        sources.refresh(now=1000.0)
        snap = sources.snapshot(now=1000.0)
        self.assertEqual(snap["gateway_24h"].data["rows"][0]["model"], "nemotron-3-ultra")
        self.assertEqual(snap["gateway_7d"].data["window"], "7d")
        self.assertEqual(snap["gateway_state"].data["inflight"][0]["target"], "spark-a")
        self.assertEqual(snap["grug"].data["inflight"][0]["persona"], "elder")
        self.assertEqual(snap["loom_runs"].data[0]["lane"], "codex")
        self.assertFalse(any(envelope.stale for envelope in snap.values()))
        # Grug's token goes as a bearer token to Grug only, never to the gateway.
        auth = dict(self.upstream.seen)
        self.assertEqual(auth["/grug/stats"], "Bearer YOUR_GRUG_TOKEN_HERE")
        self.assertIsNone(auth["/ui/state"])

    def test_a_503_from_the_stats_ledger_is_stale_with_its_error_and_the_last_good_rows(self):
        sources = self.sources()
        sources.refresh(now=1000.0)
        self.upstream.routes["/ui/stats?window=24h"] = (
            503, dict(STATS, rows=[], error="ledger database unavailable"), 0)
        sources.refresh(now=1060.0)
        envelope = sources.snapshot(now=1060.0)["gateway_24h"]
        self.assertTrue(envelope.stale)
        self.assertIn("503", envelope.error)
        self.assertIn("ledger database unavailable", envelope.error)
        self.assertEqual(envelope.last_good_at, 1000.0)
        self.assertEqual(envelope.data["rows"][0]["hits"], 9)

    def test_a_gateway_state_timeout_is_stale(self):
        self.upstream.routes["/ui/state"] = (200, STATE, 1)
        sources = self.sources()
        sources.refresh(now=1000.0)
        snap = sources.snapshot(now=1000.0)
        self.assertTrue(snap["gateway_state"].stale)
        self.assertIsNone(snap["gateway_state"].last_good_at)
        self.assertIn("timed out", snap["gateway_state"].error)
        self.assertFalse(snap["gateway_24h"].stale)

    def test_an_unknown_window_and_an_odd_answer_are_errors_not_data(self):
        self.upstream.routes["/ui/stats?window=7d"] = (400, {"error": "unknown window"}, 0)
        self.upstream.routes["/ui/stats?window=24h"] = (200, ["not", "an", "object"], 0)
        sources = self.sources()
        sources.refresh(now=1000.0)
        snap = sources.snapshot(now=1000.0)
        self.assertIn("400", snap["gateway_7d"].error)
        self.assertIn("unexpected answer", snap["gateway_24h"].error)
        self.assertIsNone(snap["gateway_24h"].data)

    def test_a_redirect_is_refused(self):
        self.upstream.routes["/grug/stats"] = (302, {}, 0)
        sources = self.sources()
        sources.refresh(now=1000.0)
        envelope = sources.snapshot(now=1000.0)["grug"]
        self.assertTrue(envelope.stale)
        self.assertIn("redirect", envelope.error)
        self.assertNotIn(("/ui/state", "Bearer YOUR_GRUG_TOKEN_HERE"), self.upstream.seen)

    def test_grug_unset_is_not_configured_and_grug_erroring_is_stale(self):
        env = {"LOOM_GATEWAY_URL": self.upstream.base}
        snap = self.sources(env=env)
        snap.refresh(now=1000.0)
        grug = snap.snapshot(now=1000.0)["grug"]
        self.assertFalse(grug.configured)
        self.assertIn("LOOM_GRUG_STATS_URL", grug.missing)
        self.upstream.routes["/grug/stats"] = (500, {"error": "boom"}, 0)
        failing = self.sources()
        failing.refresh(now=1000.0)
        grug = failing.snapshot(now=1000.0)["grug"]
        self.assertTrue(grug.configured)
        self.assertTrue(grug.stale)
        self.assertIn("500", grug.error)

    def test_the_gateway_unset_is_not_configured(self):
        sources = self.sources(env={})
        sources.refresh(now=1000.0)
        snap = sources.snapshot(now=1000.0)
        for name in ("gateway_24h", "gateway_7d", "gateway_state"):
            self.assertFalse(snap[name].configured)
            self.assertIn("LOOM_GATEWAY_URL", snap[name].missing)
        self.assertEqual(self.upstream.seen, [])

    def test_transcripts_unavailable_is_stale_with_the_last_good_runs(self):
        runs = FakeRuns([{"lane": "codex", "model": "model-x", "outcome": "done"}])
        sources = self.sources(runs)
        sources.refresh(now=1000.0)
        runs.error = OSError("database is down")
        sources.refresh(now=1060.0)
        envelope = sources.snapshot(now=1060.0)["loom_runs"]
        self.assertTrue(envelope.stale)
        self.assertEqual(envelope.last_good_at, 1000.0)
        self.assertEqual(envelope.data[0]["model"], "model-x")
        self.assertIn("database is down", envelope.error)

    def test_an_unconfigured_transcript_store_is_stale_not_empty(self):
        def missing():
            raise RuntimeError("Transcript database is not configured")
        sources = tiers_sources.TierSources(env={}, runs_store=missing, timeout=0.5)
        sources.refresh(now=1000.0)
        envelope = sources.snapshot(now=1000.0)["loom_runs"]
        self.assertTrue(envelope.stale)
        self.assertIsNone(envelope.data)
        self.assertIn("not configured", envelope.error)


if __name__ == "__main__":
    unittest.main()
