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


class LanesAndImprovingTest(unittest.TestCase):
    def test_pull_request_footer_and_models_scoreboard(self):
        from dashboard import views
        from dashboard.server import _lane_view_data

        pr_body = (
            "## Summary\n"
            "This fixes the issue.\n\n"
            "---\n"
            "Loom ticket quadseven.loom-282-r2, lane codex, model gpt-6-luna.\n"
        )
        match = views.FOOTER.search(pr_body)
        self.assertIsNotNone(match)
        self.assertEqual(match.groups(), ("quadseven/loom", "282", "codex", "gpt-6-luna"))
        self.assertEqual(views.parse_footer(pr_body), ("quadseven/loom", "282", "codex", "gpt-6-luna"))

        prs = [
            {"body": pr_body, "state": "closed", "merged_at": "2026-10-14T12:00:00Z", "difficulty": "medium"},
            {"body": pr_body, "state": "open", "difficulty": "medium"},
        ]
        board = views.build_scoreboard(prs)
        self.assertIn("medium", board)
        self.assertIn("gpt-6-luna", board["medium"])
        self.assertEqual(board["medium"]["gpt-6-luna"]["lane"], "codex")
        self.assertEqual(board["medium"]["gpt-6-luna"]["merged"], 1)
        self.assertEqual(board["medium"]["gpt-6-luna"]["open"], 1)

        ticket = {"repo": "quadseven.loom", "number": 282, "difficulty": "medium", "state": "closed"}
        payload = {
            "pull_requests": [{"body": pr_body, "state": "closed", "merged_at": "2026-10-14T12:00:00Z"}],
            "lanes": [],
            "improving": {},
        }
        lane_data = _lane_view_data(payload, [ticket])
        self.assertIn("medium", lane_data["scoreboard"])
        self.assertEqual(lane_data["scoreboard"]["medium"]["gpt-6-luna"]["merged"], 1)
        html_out = views.render_lanes(lane_data)
        self.assertIn("gpt-6-luna", html_out)
        self.assertIn("codex", html_out)
        self.assertNotIn("Scoreboard is missing", html_out)

    def test_remote_heartbeat_after_lane_status_comment(self):
        from dashboard import views
        from dashboard.server import _lane_view_data

        status_comment = {
            "created_at": "2026-10-14T12:00:00Z",
            "body": (
                "<!-- loom:lanes v1 -->\n"
                "| lane | available | heartbeat |\n"
                "| codex | yes | 1792004788.123 |\n"
                "| agy | yes | 1792004788.123 |\n"
            ),
        }
        remote_heartbeat_comment = {
            "created_at": "2026-10-14T12:05:00Z",
            "body": "<!-- loom:remote-heartbeat v1 muse 1792004788.123 -->\n",
        }
        payload = {
            "lanes": [status_comment, remote_heartbeat_comment],
            "pull_requests": [],
            "improving": {},
        }
        lane_data = _lane_view_data(payload, [])
        lanes_by_name = {lane["lane"]: lane for lane in lane_data["lanes"]}
        self.assertIn("codex", lanes_by_name)
        self.assertIn("agy", lanes_by_name)
        self.assertIn("muse", lanes_by_name)
        self.assertTrue(lanes_by_name["muse"]["available"])

        html_out = views.render_lanes(lane_data)
        self.assertNotIn("Lane status is missing", html_out)
        self.assertIn("<strong>codex</strong>", html_out)
        self.assertIn("<strong>agy</strong>", html_out)
        self.assertIn("<strong>muse</strong>", html_out)

    def test_improving_filters_none_lines_and_renders_nine_factory_numbers(self):
        from dashboard import views

        journal_comment = {
            "id": 999123,
            "html_url": "/repos/example-owner/sample/issues/1#issuecomment-999123",
            "body": (
                "<!-- loom:journal v1 -->\n"
                "Friction: none.\n"
                "Blocker: none\n"
                "Waiting: none.\n"
                "Asks: none.\n"
                "Friction: Flaky test suite in worker runner\n"
                "Friction: None.\n"
            ),
        }
        tickets = [
            {
                "repo": "example-owner/sample",
                "number": 42,
                "title": "Fix flaky tests",
                "state": "open",
                "body": (
                    "## Why\n"
                    "Flaky test suite in worker runner causes retries.\n"
                ),
            }
        ]
        current_metrics = {
            "body": (
                "<!-- loom:metrics v1 -->\n"
                "2026-10-14:\n"
                "1. PASS Metric One: 42\n"
                "2. PASS Metric Two: 10 (target 12)\n"
                "3. PASS Metric Three: 85%\n"
                "4. FAIL Metric Four: 12\n"
                "5. PASS Metric Five: 100\n"
                "6. PASS Metric Six: 5\n"
                "7. PASS Metric Seven: 18\n"
                "8. PASS Metric Eight: 0\n"
                "9. PASS Metric Nine: 99\n"
            )
        }
        previous_metrics = {
            "body": (
                "<!-- loom:metrics v1 -->\n"
                "2026-10-13:\n"
                "1. PASS Metric One: 40\n"
                "2. FAIL Metric Two: 8 (target 12)\n"
                "3. PASS Metric Three: 80%\n"
                "4. FAIL Metric Four: 15\n"
                "5. PASS Metric Five: 95\n"
                "6. PASS Metric Six: 4\n"
                "7. PASS Metric Seven: 15\n"
                "8. PASS Metric Eight: 0\n"
                "9. PASS Metric Nine: 95\n"
            )
        }

        data = views.build_improvements([journal_comment], tickets, [current_metrics, previous_metrics])

        self.assertEqual(len(data["records"]), 1)
        card = data["records"][0]
        self.assertEqual(card["friction"], "Flaky test suite in worker runner")
        self.assertIsNotNone(card["ticket"])
        self.assertEqual(card["ticket"]["number"], 42)

        html_out = views.render_improving(data)
        self.assertIn('"Flaky test suite in worker runner"', html_out)
        self.assertNotIn('"none"', html_out.lower())
        self.assertIn("#42 Fix flaky tests", html_out)
        self.assertNotIn("ticket not found", html_out)

        self.assertIn('class="factory-grid"', html_out)
        self.assertEqual(html_out.count("factory-card"), 9)
        for num_name in ("Metric One", "Metric Two", "Metric Three", "Metric Four",
                         "Metric Five", "Metric Six", "Metric Seven", "Metric Eight", "Metric Nine"):
            self.assertIn(num_name, html_out)
        self.assertIn("was 40", html_out)
        self.assertIn("was 8", html_out)
        self.assertIn("was 80%", html_out)
        self.assertIn("better", html_out)


if __name__ == "__main__":
    unittest.main()
