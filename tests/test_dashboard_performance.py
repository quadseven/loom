"""Tests for dashboard latency optimizations: gzip, static caching, non-blocking
station_state sync, snapshot precomputation, and view caching."""
from __future__ import annotations

import gzip
import http.client
import json
import sys
import threading
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import server, station_state, views  # noqa: E402
from tests.sandbox_caps import LOCALHOST, NO_LOCALHOST  # noqa: E402


class FakeState:
    def __init__(self, version=1):
        self.stale = False
        self.ready = True
        self.repos = ["example-owner/sample"]
        self.operator_items = []
        self.open_issues = {}
        self.open_issues_read_at = 0
        self.open_issues_error = ""
        self.waits = {}
        self.waits_error = ""
        self.data = {"example-owner/sample": [
            {"repo": "example-owner/sample", "number": 1, "title": "Fast page", "state": "running",
             "open": True, "lane": "codex", "model": "gpt-5", "labels": ["loom:ticket", "loom:running", "lane:codex"],
             "updated_at": "2030-01-08T10:00:00Z", "created_at": "2030-01-08T09:00:00Z", "body": ""}
        ]}
        self._version = version
        self.response_calls = 0

    def response(self):
        self.response_calls += 1
        return {
            "tickets": self.data, "errors": {}, "repeated_failures": [],
            "stale": self.stale, "auth": "token",
            "ticket_refreshed_at": 1894096800, "next_refresh_at": 1894096860,
            "stale_reason": "", "improving": {"journals": [], "metrics_comments": []},
            "improving_built": None, "lanes": [], "pull_requests": [],
            "footer_models": {}, "footer_runs": {}, "newest_lane_models": {},
            "fix_round_refs": set(), "all_tickets": self.data["example-owner/sample"],
            "models_resolved": True, "_version": self._version,
            "latest_runs": {}, "state_entered": {}, "wait_rows": [],
            "wait_cause_rows": [], "wait_bottleneck": None, "floor_context": {},
            "goal_context": {}, "goal_context_error": "", "operator_items": [],
            "dashboard_errors": [], "brain_step": None, "brain_step_error": "",
            "issue_comments": [], "review_comments": [], "hot_spots_error": "",
            "source_read_at": {}, "sources_carried": [], "operator_claims": {}, "usage": [],
        }

    def start_background(self):
        pass

    def stop_background(self):
        pass


@unittest.skipUnless(LOCALHOST, NO_LOCALHOST)
class CompressionAndStaticCacheTest(unittest.TestCase):
    def setUp(self):
        self.state = FakeState()
        self.httpd = server.make_server(host="127.0.0.1", port=0, store=self.state)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_html_compresses_when_client_accepts_gzip(self):
        status, headers, raw = self._get("/", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")
        html = gzip.decompress(raw).decode("utf-8")
        self.assertIn("<!doctype html>", html)
        self.assertLess(len(raw), len(html.encode("utf-8")))

    def test_static_asset_returns_etag_cache_control_and_304(self):
        status, headers, raw = self._get("/static/loom.js", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        etag = headers.get("ETag")
        self.assertTrue(etag)
        self.assertIn("max-age=", headers.get("Cache-Control", ""))
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        body = gzip.decompress(raw).decode("utf-8")
        self.assertIn("loomPoll", body)

        status304, _, body304 = self._get("/static/loom.js", {"If-None-Match": etag})
        self.assertEqual(status304, 304)
        self.assertEqual(body304, b"")

    def test_api_tickets_excludes_internal_snapshot_keys(self):
        status, _, raw = self._get("/api/tickets")
        self.assertEqual(status, 200)
        payload = json.loads(raw.decode("utf-8"))
        for key in server.INTERNAL_SNAPSHOT_KEYS:
            self.assertNotIn(key, payload)
        self.assertIn("source_counts", payload)


class StationStateSyncAndLiveFloorTest(unittest.TestCase):
    def test_static_requests_never_trigger_station_state_sync(self):
        state = FakeState()
        sync_calls = []
        handler_type = server._handler_for(state)
        handler = handler_type.__new__(handler_type)
        handler.path = "/static/loom.js"
        handler.headers = Message()
        handler.send_bytes = lambda *args, **kwargs: None
        with patch.object(station_state, "sync", side_effect=lambda *a, **k: sync_calls.append(1)):
            handler.do_GET()
        self.assertEqual(sync_calls, [])

    def test_background_sync_prevents_blocking_do_get(self):
        state = FakeState()
        state.station_state = station_state.Store(station_state.MemoryBackend())
        stop = threading.Event()
        thread = station_state.start_background_sync(state, stop)
        self.addCleanup(stop.set)
        self.assertIsNotNone(thread)
        self.assertTrue(getattr(state, "_station_state_bg", False))
        sync_calls = []
        handler_type = server._handler_for(state)
        handler = handler_type.__new__(handler_type)
        handler.path = "/api/floor/live"
        handler.headers = Message()
        handler.send_json = lambda *args, **kwargs: None
        with patch.object(station_state, "sync", side_effect=lambda *a, **k: sync_calls.append(1)):
            handler.do_GET()
        self.assertEqual(sync_calls, [])

    def test_floor_live_skips_scoreboard_computation(self):
        state = FakeState(version=None)
        with patch.object(views, "build_scoreboard", side_effect=AssertionError("should not run")):
            result = server._floor_live(state)
        self.assertIn("lanes", result)
        self.assertIn("moving", result)

    def test_cached_view_reuses_result_until_version_changes(self):
        state = FakeState(version=1)
        calls = []
        first = server._cached_view(state, 1, ("k",), lambda: (calls.append(1), "v1")[1])
        second = server._cached_view(state, 1, ("k",), lambda: (calls.append(2), "v2")[1])
        third = server._cached_view(state, 2, ("k",), lambda: (calls.append(3), "v3")[1])
        self.assertEqual((first, second, third), ("v1", "v1", "v3"))
        self.assertEqual(calls, [1, 3])

    def test_postgres_backend_reuses_connection_and_reconnects_on_error(self):
        connects = []

        class FlakyCursor:
            def __init__(self, fail):
                self.fail = fail

            def execute(self, sql, params=()):
                if self.fail:
                    raise OSError("connection dropped")

            def fetchall(self):
                return [("chain", '{"repos": []}', 5.0)]

        class FlakyConn:
            def __init__(self, fail_second=False):
                self.fail_second = fail_second
                self.calls = 0

            def cursor(self):
                self.calls += 1
                return FlakyCursor(self.fail_second and self.calls > 1)

            def commit(self):
                pass

            def close(self):
                pass

        conns = [FlakyConn(fail_second=True), FlakyConn(fail_second=False)]

        def fake_connect(_dbapi):
            connects.append(1)
            return conns.pop(0)

        backend = station_state.PostgresBackend(object())
        with patch.object(station_state.transcripts, "connect", fake_connect):
            rows1 = backend.all()
            rows2 = backend.all()
        self.assertEqual(len(connects), 2)
        self.assertEqual(rows1["chain"]["at"], 5.0)
        self.assertEqual(rows2["chain"]["at"], 5.0)


if __name__ == "__main__":
    unittest.main()
