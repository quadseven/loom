"""Tests for dashboard latency optimizations: gzip, static caching, non-blocking
station_state sync, snapshot precomputation, and view caching."""
from __future__ import annotations

import gzip
import http.client
import json
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import brain_view, fleet, server, station_state, views  # noqa: E402
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
            def __init__(self, fail_after=None):
                self.fail_after = fail_after
                self.calls = 0

            def cursor(self):
                self.calls += 1
                return FlakyCursor(self.fail_after is not None and self.calls > self.fail_after)

            def commit(self):
                pass

            def close(self):
                pass

        conns = [FlakyConn(fail_after=2), FlakyConn()]

        def fake_connect(_dbapi):
            connects.append(1)
            return conns.pop(0)

        backend = station_state.PostgresBackend(object())
        with patch.object(station_state.transcripts, "connect", fake_connect):
            rows1 = backend.all()
            rows2 = backend.all()
            self.assertEqual(len(connects), 1)
            rows3 = backend.all()
        self.assertEqual(len(connects), 2)
        self.assertEqual(rows3["chain"]["at"], 5.0)
        self.assertEqual(rows1["chain"]["at"], 5.0)
        self.assertEqual(rows2["chain"]["at"], 5.0)


class Wave4OptimizationParityTest(unittest.TestCase):
    def test_build_improvements_index_matches_linear_scan(self):
        repo = "example-owner/sample"
        ref_direct = "%s#%d" % (repo, 10)
        ref_body = "%s#%d" % (repo, 60)
        tickets = [
            {"repo": repo, "number": 10, "title": "Direct first", "state": "shipped", "body": "## Why\nOther."},
            {"repo": repo, "number": 10, "title": "Direct duplicate", "state": "queued", "body": ""},
            {"repo": repo, "number": 20, "title": "Noticed mismatch", "state": "queued",
             "body": "Noticed in https://example.com/issues/1#issuecomment-200: unrelated slow build step\n"},
            {"repo": repo, "number": 21, "title": "Noticed match", "state": "merged",
             "body": "Noticed in https://example.com/issues/1#issuecomment-200: runner cache miss during compile\n"},
            {"repo": repo, "number": 22, "title": "Noticed short", "state": "briefed",
             "body": "Noticed in https://example.com/issues/1#issuecomment-201\n"},
            {"repo": repo, "number": 23, "title": "Refs id", "state": "pr",
             "body": "Refs journal comment 202\n"},
            {"repo": repo, "number": 30, "title": "Why overlap first", "state": "shipped",
             "body": "## Why\nThe checkout step timed out repeatedly when cloning large fixtures.\n## Out of scope\nNone"},
            {"repo": repo, "number": 31, "title": "Why overlap second", "state": "queued",
             "body": "## Why\nThe checkout step timed out repeatedly when cloning large fixtures.\n"},
            {"repo": repo, "number": 60, "title": "Body ref", "state": "running", "body": ""},
            {"repo": repo, "number": 70, "title": "Journal issue ref", "state": "queued",
             "refs": [(repo, "99")], "body": ""},
        ]
        journals = [
            {"id": 100, "html_url": "https://example.com/issues/1#issuecomment-100",
             "created_at": "2030-01-10T10:00:00Z", "repo": repo, "issue_number": 1,
             "body": "<!-- loom:journal v1 -->\n- Friction: see %s for retry delay.\n" % ref_direct},
            {"id": 200, "html_url": "https://example.com/issues/1#issuecomment-200",
             "created_at": "2030-01-10T09:00:00Z", "repo": repo, "issue_number": 1,
             "body": "<!-- loom:journal v1 -->\n- Friction: runner cache miss during compile.\n"},
            {"id": 201, "html_url": "https://example.com/issues/1#issuecomment-201",
             "created_at": "2030-01-10T08:00:00Z", "repo": repo, "issue_number": 1,
             "body": "<!-- loom:journal v1 -->\n- Friction: short noticed link friction line.\n"},
            {"id": 202, "html_url": "https://example.com/issues/1#issuecomment-202",
             "created_at": "2030-01-10T07:00:00Z", "repo": repo, "issue_number": 1,
             "body": "<!-- loom:journal v1 -->\n- Friction: refs id matched friction line.\n"},
            {"id": 300, "html_url": "https://example.com/issues/1#issuecomment-300",
             "created_at": "2030-01-10T06:00:00Z", "repo": repo, "issue_number": 1,
             "body": "<!-- loom:journal v1 -->\n- Friction: checkout step timed out repeatedly when cloning large fixtures.\n"},
            {"id": 400, "html_url": "https://example.com/issues/1#issuecomment-400",
             "created_at": "2030-01-10T05:00:00Z", "repo": repo, "issue_number": 1,
             "body": "<!-- loom:journal v1 -->\n- Done: linked %s.\n- Friction: unmatched friction falls back to body ref.\n" % ref_body},
            {"id": 500, "html_url": "https://example.com/issues/99#issuecomment-500",
             "created_at": "2030-01-10T04:00:00Z", "repo": repo, "issue_number": 99,
             "body": "<!-- loom:journal v1 -->\n- Friction: unmatched friction falls back to issue ref.\n"},
        ]
        built = views.build_improvements(journals, tickets, [])
        # Compare every friction match against the original linear _ticket_for_friction.
        for record in built["records"]:
            journal = record["journal"]
            expected = views._ticket_for_friction(journal, record["friction"], str(journal["body"]), tickets)
            self.assertEqual(record["ticket"], expected)
        matched_numbers = [r["ticket"]["number"] for r in built["records"]]
        self.assertEqual(matched_numbers, [10, 21, 22, 23, 30, 60, 70])

    def test_journal_url_prefix_keeps_the_legacy_digit_boundary(self):
        url = "https://example.com/issues/1#issuecomment-123"
        journal = {"html_url": url}
        for suffix, matches in (("", True), (" appendix", True), ("extra", True), ("4", False)):
            with self.subTest(suffix=suffix):
                ticket = {"body": url + suffix}
                expected = ticket if matches else None
                self.assertIs(views._ticket_refs_journal([ticket], journal), expected)
                index = views._TicketIndex([{"body": "Unrelated ticket"}, ticket])
                with patch.object(index, "_ticket_matches_journal", wraps=index._ticket_matches_journal) as matcher:
                    self.assertIs(index.ticket_refs_journal(journal, ""), expected)
                self.assertEqual([call.args[0] for call in matcher.call_args_list], [1] if matches else [])

    def test_journal_url_embedded_in_another_url_matches(self):
        url = "https://example.com/issues/1#issuecomment-123"
        journal = {"html_url": url}
        ticket = {"body": "https://example.com/redirect?target=" + url}
        self.assertIs(views._ticket_refs_journal([ticket], journal), ticket)
        self.assertIs(views._TicketIndex([ticket]).ticket_refs_journal(journal, ""), ticket)

    def test_journal_id_matches_without_a_url(self):
        journal = {"id": 123}
        ticket = {"body": "See issuecomment-123."}
        tickets = [{"body": "See issuecomment-1234."}, ticket]
        self.assertIs(views._ticket_refs_journal(tickets, journal), ticket)
        self.assertIs(views._TicketIndex(tickets).ticket_refs_journal(journal, ""), ticket)

    def test_noticed_quote_stays_with_its_journal_across_unlinked_lines(self):
        url = "https://example.com/issues/1#issuecomment-123"
        journal = {"html_url": url, "id": 123}
        friction = "runner cache miss during compile"
        wrong = {"body": f"Noticed in {url}: slow dashboard redraw after refresh\n"
                         "Noticed in: no linked comment on this line\n"
                         "Noticed in https://example.com/issues/1#issuecomment-567: " + friction}
        right = {"body": f"Noticed in {url}: {friction}"}
        tickets = [wrong, right]
        self.assertIs(views._ticket_refs_journal(tickets, journal, friction), right)
        self.assertIs(views._TicketIndex(tickets).ticket_refs_journal(journal, friction), right)

    def test_summarize_prs_matches_scoreboard_and_lane_view_uses_precomputed(self):
        repo = "example-owner/sample"
        tickets = [
            {"repo": repo, "number": 1, "difficulty": "easy"},
            {"repo": repo, "number": 2, "difficulty": "hard"},
            {"repo": repo, "number": 3, "difficulty": None},
        ]
        prs = [
            {"number": 101, "state": "closed", "merged_at": "2030-01-09T12:00:00Z",
             "created_at": "2030-01-09T10:00:00Z", "updated_at": "2030-01-09T12:00:00Z",
             "html_url": "https://example.com/pr/101",
             "body": "Loom ticket example-owner.sample-1, lane codex, model gpt-5.\n"},
            {"number": 102, "state": "open", "merged_at": None,
             "created_at": "2030-01-09T11:00:00Z", "updated_at": "2030-01-09T11:00:00Z",
             "html_url": "https://example.com/pr/102",
             "body": "Loom ticket example-owner.sample-2, lane anthropic, model claude-sonnet.\n"},
            {"number": 103, "state": "closed", "merged_at": None, "difficulty": "medium",
             "created_at": "2030-01-09T09:00:00Z", "updated_at": "2030-01-09T09:00:00Z",
             "html_url": "https://example.com/pr/103",
             "body": "Loom ticket example-owner.sample-3, lane codex, model gpt-5.\n"},
        ]
        models, runs, by_lane, scoreboard = views.summarize_prs(prs, tickets)
        self.assertEqual(models, views.footer_models(prs))
        self.assertEqual(runs, views.footer_runs(prs))
        self.assertEqual(by_lane, server._newest_lane_models(prs))
        self.assertEqual(scoreboard, views.build_scoreboard(server._scoreboard_rows(prs, tickets)))

        payload = {"pull_requests": prs, "lanes": [], "improving": {"metrics_comments": []},
                   "newest_lane_models": by_lane, "scoreboard": scoreboard}
        with patch.object(views, "build_scoreboard", side_effect=AssertionError("should use precomputed")):
            lane_data = server._lane_view_data(payload, tickets)
        self.assertEqual(lane_data["scoreboard"], scoreboard)

    def test_journal_holders_and_fleet_journal_rows_skip_redundant_scans(self):
        now = datetime(2030, 1, 10, 12, 0, tzinfo=timezone.utc)
        start = now - timedelta(hours=24)
        old_body = "<!-- loom:journal v1 -->\nTick 2030-01-05T10:00:00Z, muse\n- Done: old work.\n"
        new_body = "<!-- loom:journal v1 -->\nTick 2030-01-10T11:00:00Z, muse\n- Step 2: failed check.\n"
        journals = [
            {"id": 1, "created_at": "2030-01-05T10:00:00Z", "repo": "example-owner/sample",
             "issue_number": 30, "body": old_body},
            {"id": 2, "created_at": "2030-01-05T10:00:00Z", "repo": "example-owner/sample",
             "issue_number": 31, "body": old_body},
            {"id": 3, "created_at": "2030-01-10T11:00:00Z", "repo": "example-owner/sample",
             "issue_number": 30, "body": new_body},
            {"id": 4, "created_at": "2030-01-10T11:00:00Z", "repo": "example-owner/sample",
             "issue_number": 31, "body": new_body},
        ]
        holders = brain_view._journal_holders(journals)
        self.assertEqual(set(holders.keys()), {"muse"})
        self.assertEqual(holders["muse"]["outcome"], "failed")
        self.assertEqual(holders["muse"]["at"], "2030-01-10T11:00:00Z")

        rows, homes = fleet._journal_rows(journals, start, now)
        muse_row = next(r for r in rows if r["key"] == "brain:muse")
        self.assertEqual(len(muse_row["_all"]), 2)
        in_window = [e for e in muse_row["_all"] if start <= e["at"] <= now]
        self.assertEqual(len(in_window), 1)
        self.assertEqual(in_window[0]["kind"], "tick-failed")
        self.assertIn("Step 2: failed check", in_window[0]["gist"])
        self.assertEqual(homes, {("example-owner/sample", 30)})


if __name__ == "__main__":
    unittest.main()
