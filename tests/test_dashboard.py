"""Dashboard HTTP and GitHub behavior using a local API stub."""
import json
import os
import http.client
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from bus.github import GitHub
from dashboard.github import TicketStore
from dashboard.server import make_server


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.api_calls = []

        class APIHandler(BaseHTTPRequestHandler):
            def do_GET(inner):
                self.api_calls.append(dict(path=inner.path, etag=inner.headers.get("If-None-Match")))
                headers = {"ETag": '"snapshot-1"', "X-RateLimit-Remaining": "10",
                           "X-RateLimit-Limit": "5000", "X-RateLimit-Reset": "2000000000"}
                if inner.headers.get("If-None-Match") == '"snapshot-1"':
                    inner.send_response(304)
                    for name, value in headers.items():
                        inner.send_header(name, value)
                    inner.end_headers()
                    return
                if inner.path.startswith("/repos/example-owner/failing/"):
                    inner.send_response(403)
                    inner.send_header("Content-Type", "application/json")
                    inner.end_headers()
                    inner.wfile.write(b'{"message":"stub read failure"}')
                    return
                payload = [{"number": 31, "title": "Sample ticket", "state": "open",
                            "body": "Difficulty: medium\nModel: model-x\nPart of #30\nRefs example-owner/sample#30",
                            "updated_at": "2026-10-14T12:00:00Z",
                            "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"},
                                       {"name": "lane:codex-2"}]}]
                body = json.dumps(payload).encode()
                inner.send_response(200)
                for name, value in headers.items():
                    inner.send_header(name, value)
                inner.send_header("Content-Length", str(len(body)))
                inner.end_headers()
                inner.wfile.write(body)

            def log_message(inner, fmt, *args):
                pass

        self.api = ThreadingHTTPServer(("127.0.0.1", 0), APIHandler)
        self.api_thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.api_thread.start()
        self.cache = tempfile.TemporaryDirectory()
        def local_transport(method, url, headers, body):
            parsed = urlsplit(url)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port)
            try:
                connection.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""),
                                   body=body, headers=headers)
                response = connection.getresponse()
                return response.status, dict(response.headers.items()), response.read()
            finally:
                connection.close()
        gh = GitHub("", api="http://127.0.0.1:%d" % self.api.server_port,
                    cache_dir=self.cache.name, transport=local_transport)
        self.store = TicketStore(["example-owner/sample", "example-owner/failing"], gh=gh, budget_floor=5)
        self.server = make_server(port=0, store=self.store)
        for _ in range(200):
            if self.store.ready:
                break
            threading.Event().wait(0.01)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.api.shutdown()
        self.api.server_close()
        self.cache.cleanup()

    def get_json(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return json.loads(response.read().decode())
        finally:
            connection.close()

    def test_backlog_bytes_order_start_here_and_health_inspectors(self):
        fixture = {
            "generated_at": "today", "judged_by": "Cave", "repos": [], "totals": {},
            "epics": [
                {"repo": "example-owner/sample", "number": 10, "title": "First", "tier": "Elder", "why": "why", "score": 4,
                 "state": "open", "next": {"number": 11, "title": "First next"}, "children": [
                     {"repo": "example-owner/sample", "number": 11, "state": "closed", "parent": 10},
                     {"repo": "example-owner/sample", "number": 12, "state": "closed"}]},
                {"repo": "example-owner/sample", "number": 20, "title": "[odd] title", "tier": "Cave", "children": []},
            ], "needs_operator": [], "ready_for_human": [], "queue_comments_read": []}
        raw = json.dumps(fixture, separators=(",", ":")).encode()
        with tempfile.NamedTemporaryFile() as source:
            source.write(raw)
            source.flush()
            prior = os.environ.get("LOOM_BACKLOG_SOURCE")
            os.environ["LOOM_BACKLOG_SOURCE"] = source.name
            try:
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
                connection.request("GET", "/backlog.json")
                response = connection.getresponse()
                self.assertEqual(response.read(), raw)
                self.assertEqual(response.getheader("Content-Type"), "application/json")
                connection.request("GET", "/backlog")
                body = connection.getresponse().read().decode()
                self.assertLess(body.index("First next"), body.index("[odd] title"))
                self.assertIn("First next", body[body.index("Start here"):])
                self.assertIn("Finished children, open epic", body)
                self.assertIn("Children without parent link", body)
                self.assertIn("Nonstandard titles", body)
                connection.close()
            finally:
                if prior is None:
                    os.environ.pop("LOOM_BACKLOG_SOURCE", None)
                else:
                    os.environ["LOOM_BACKLOG_SOURCE"] = prior

    def test_api_returns_tickets_and_failed_repo_reason(self):
        payload = self.get_json("/api/tickets")
        row = payload["tickets"]["example-owner/sample"][0]
        self.assertEqual(row["state"], "pr")
        self.assertEqual(row["goal"], "example-owner/sample#30")
        self.assertEqual(row["lane"], "codex-2")
        self.assertEqual(row["difficulty"], "medium")
        self.assertIsNone(row["attempts"])
        self.assertIsNone(row["rung"])
        self.assertEqual(payload["auth"], "none")
        self.assertIn("example-owner/failing", payload["errors"])
        # The whole reason: the request, GitHub's status and its own message.
        self.assertEqual(
            payload["errors"]["example-owner/failing"],
            "GET /repos/example-owner/failing/issues?labels=loom%3Aticket&state=all&per_page=100"
            " -> 403: stub read failure")

    def test_requests_serve_background_snapshot_without_refreshing(self):
        for _ in range(100):
            if self.store.ready:
                break
            threading.Event().wait(0.01)
        before = len(self.api_calls)
        self.get_json("/api/tickets")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("GET", "/")
            connection.getresponse().read()
        finally:
            connection.close()
        self.assertEqual(len(self.api_calls), before)

    def test_conditional_request_and_budget_floor_stale_response(self):
        self.store.refresh()
        self.store.budget_floor = 20
        before = len(self.api_calls)
        self.store.refresh()
        payload = self.get_json("/api/tickets")
        self.assertEqual(len(self.api_calls), before)
        self.assertTrue(payload["stale"])
        self.assertEqual(self.api_calls[0]["etag"], None)
        # A separate store remains above floor to exercise conditional GET.
        conditional = TicketStore(["example-owner/sample"],
                                  gh=GitHub("", api="http://127.0.0.1:%d" % self.api.server_port,
                                            cache_dir=self.cache.name,
                                            transport=self.store.gh.transport), budget_floor=0)
        conditional.refresh()
        conditional.refresh()
        self.assertEqual(self.api_calls[-1]["etag"], '"snapshot-1"')

    def test_part_of_only_is_goal_and_indentation_is_allowed(self):
        from dashboard.github import ticket_record
        row = ticket_record("example-owner/sample", {
            "body": "    Part of #30\nRefs #31", "labels": [],
        })
        self.assertEqual(row["goal"], "example-owner/sample#30")
        self.assertIsNone(ticket_record("example-owner/sample", {
            "body": "Refs #31", "labels": [],
        })["goal"])

    def test_routes_and_unknown_path(self):
        self.get_json("/api/tickets")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("GET", "/tickets?state=pr&goal=example-owner%2Fsample%2330")
            response = connection.getresponse()
            self.assertIn(b"Sample ticket", response.read())
            connection.request("GET", "/unknown")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
        finally:
            connection.close()


class DashboardSourceHygiene(unittest.TestCase):
    def test_dashboard_has_no_hostname_ip_or_home_path(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "dashboard"
        hostname = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|app|internal|local)\b", re.I)
        ip_address = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        home_path = re.compile(r"/(?:Users|home)/[^/\s]+")
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for pattern in (hostname, ip_address, home_path):
                self.assertIsNone(pattern.search(source), str(path))


class InsightViews(unittest.TestCase):
    def test_insight_routes_render_views(self):
        from dashboard.server import _dispatch_get

        class Handler:
            def send_page(self, body):
                return body

            def send_text(self, code, body):
                return code, body

        for path, heading, renderer in (("/brain", "Brain", "brain_page"),
                                        ("/hot-spots", "Hot spots", "hotspots_page")):
            with self.subTest(path=path), patch("dashboard.server.insights." + renderer, return_value="<p>Fixture view</p>"):
                handler = Handler()
                handler.path = path
                rendered = _dispatch_get(handler, object())
                self.assertIn(heading, rendered)
                self.assertIn("Fixture view", rendered)

    def test_brain_renders_fixture_step_and_preserves_handoff_markers(self):
        from dashboard.insights import brain_page
        from pathlib import Path
        import os
        old_home = os.environ.get("LOOM_HOME")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["LOOM_HOME"] = tmp
                state_dir = Path(tmp) / "state"
                state_dir.mkdir()
                (state_dir / "brain-step.json").write_text(json.dumps({
                    "current": "merge", "done": ["lock", "status"],
                    "next_move": "Merge the reviewed change.",
                    "ranking": [{"text": "A measured regression", "members_affected": 3, "hours_lost": 2}],
                    "markers": ["done example-org/project#42 --stage merge --holder runner"],
                }))
                class State:
                    data = {}
                    class GitHub:
                        pass
                    gh = GitHub()
                rendered = brain_page(State())
                self.assertIn('class="step current" aria-current="step"><span>merge</span>', rendered)
                self.assertIn("done example-org/project#42 --stage merge --holder runner", rendered)
                self.assertIn("3 members affected times 2 hours lost", rendered)
        finally:
            if old_home is None:
                os.environ.pop("LOOM_HOME", None)
            else:
                os.environ["LOOM_HOME"] = old_home

    def test_missing_step_record_is_not_assumed_to_be_step_one(self):
        from dashboard.insights import progress_html
        self.assertIn("not recorded", progress_html({}))
        self.assertNotIn('class="step current"', progress_html({}))

    def test_lane_failure_rate_flags_more_than_twenty_percent_in_text(self):
        from dashboard.insights import lane_failure_rates
        now = 1_800_000_000
        comments = [
            {"created_at": "2027-01-15T08:00:00Z", "body": "lane: codex outcome: failed"},
            {"created_at": "2027-01-15T09:00:00Z", "body": "lane: codex outcome: done"},
            {"created_at": "2027-01-15T10:00:00Z", "body": "lane: codex outcome: done"},
            {"created_at": "2027-01-15T11:00:00Z", "body": "lane: codex outcome: done"},
            {"created_at": "2027-01-15T12:00:00Z", "body": "lane: codex outcome: done"},
        ]
        rows = lane_failure_rates(comments, now)
        row = next(item for item in rows if item["lane"] == "codex")
        self.assertFalse(row["high"])
        comments.append({"created_at": "2027-01-15T13:00:00Z", "body": "lane: codex outcome: failed"})
        row = next(item for item in lane_failure_rates(comments, now) if item["lane"] == "codex")
        self.assertTrue(row["high"])
        self.assertGreater(row["rate"], 0.2)
        from dashboard.insights import lane_failure_html
        self.assertIn("high failure rate", lane_failure_html([row]))


class BacklogHelperTest(unittest.TestCase):
    def test_backlog_source_rejects_http_and_private_hosts(self):
        from dashboard.server import _backlog_bytes
        with patch.dict(os.environ, {"LOOM_BACKLOG_SOURCE": "http://example.com/backlog.json"}):
            self.assertIsNone(_backlog_bytes())
        with patch.dict(os.environ, {"LOOM_BACKLOG_SOURCE": "https://127.0.0.1/backlog.json"}), \
                patch("dashboard.server.build_opener") as open_url:
            self.assertIsNone(_backlog_bytes())
            open_url.assert_not_called()

    def test_health_inspectors_report_each_problem_kind(self):
        from dashboard.server import _health_inspectors
        document = {"epics": [
            {"repo": "example-owner/sample", "number": 1, "title": "Open epic", "state": "open",
             "children": [{"number": 2, "state": "closed"}]},
            {"repo": "example-owner/sample", "number": 3, "title": "[odd]", "children": []},
        ]}
        reports = dict(_health_inspectors(document))
        self.assertEqual(len(reports["Finished children, open epic"]), 1)
        self.assertEqual(len(reports["Children without parent link"]), 1)
        self.assertEqual(len(reports["Nonstandard titles"]), 1)

    def test_needs_you_deduplicates_orders_and_marks_updated_idle(self):
        from dashboard.server import _needs_you
        class State:
            operator_items = []
            human_counts = {"example-owner/sample": 2}
        payload = {"needs_operator": [{"repo": "example-owner/sample", "number": 3, "title": "dupe", "ask": "Exact ask", "waiting_since": "2026-01", "waiting_since_source": "updated", "unblocks": 8},
                                      {"repo": "example-owner/sample", "number": 4, "title": "next", "waiting_since": "2026-02", "unblocks": 7}],
                   "ready_for_human": [{"repo": "example-owner/sample", "number": 4, "title": "duplicate"}]}
        body = _needs_you(payload, State())
        self.assertEqual(body.count("example-owner/sample#3"), 1)
        self.assertLess(body.index("#3"), body.index("#4"))
        self.assertIn("idle", body)
        self.assertIn("Exact ask", body)
        self.assertIn("loom:human counts by repo", body)


if __name__ == "__main__":
    unittest.main()
