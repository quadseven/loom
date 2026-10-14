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

    def test_brain_and_hot_spots_routes_render_with_missing_sources(self):
        for path, expected in (("/brain", "not recorded"), ("/hot-spots", "Wait times: missing")):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read().decode()
            finally:
                connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn(expected, body)

    def test_floor_backlog_bottleneck_and_ticket_links(self):
        import os
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as fixture:
            json.dump({"tickets": [{"id": "sample#1"}, {"id": "sample#2"}]}, fixture)
            fixture.flush()
            prior = os.environ.get("LOOM_BACKLOG_SOURCE")
            os.environ["LOOM_BACKLOG_SOURCE"] = fixture.name
            self.store.waits = {"queued": [1, 2, 3], "pr": [5, 6, 8]}
            try:
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
                try:
                    connection.request("GET", "/")
                    body = connection.getresponse().read().decode()
                finally:
                    connection.close()
            finally:
                if prior is None:
                    os.environ.pop("LOOM_BACKLOG_SOURCE", None)
                else:
                    os.environ["LOOM_BACKLOG_SOURCE"] = prior
        self.assertIn("<strong>2</strong>", body)
        self.assertIn("bottleneck", body)
        self.assertIn('href="/t/example-owner/sample/31"', body)

    def test_ticket_state_filter_and_empty_clear_filters(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("GET", "/tickets?state=pr")
            body = connection.getresponse().read().decode()
            self.assertIn("Sample ticket", body)
            self.assertNotIn("queued", body)
            connection.request("GET", "/tickets?state=verified")
            body = connection.getresponse().read().decode()
            self.assertIn("Clear filters", body)
            self.assertIn("No tickets match these filters", body)
        finally:
            connection.close()

    def test_missing_backlog_source_is_not_reported_as_zero(self):
        import os
        prior = os.environ.pop("LOOM_BACKLOG_SOURCE", None)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
            try:
                connection.request("GET", "/")
                body = connection.getresponse().read().decode()
            finally:
                connection.close()
        finally:
            if prior is not None:
                os.environ["LOOM_BACKLOG_SOURCE"] = prior
        self.assertIn("not looked", body)
        self.assertIn("LOOM_BACKLOG_SOURCE is not set", body)


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


class DashboardViewTest(unittest.TestCase):
    class State:
        stale = False
        context = {}
        waits = {"queued": [1, 2, 3], "pr": [5, 6, 8]}
        data = {"example-owner/sample": [
            {"repo": "example-owner/sample", "number": 31, "title": "Sample ticket",
             "state": "pr", "goal": "example-owner/sample#30", "lane": "codex",
             "model": "model-x", "attempts": 1},
            {"repo": "example-owner/sample", "number": 32, "title": "Other ticket",
             "state": "briefed", "goal": "example-owner/sample#30", "lane": None,
             "model": None, "attempts": 1},
        ]}

        def response(self):
            return {"tickets": self.data}

    def test_floor_bottleneck_and_links(self):
        from dashboard.server import _floor
        import os
        prior = os.environ.get("LOOM_BACKLOG_SOURCE")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as fixture:
            json.dump({"tickets": [{"id": "sample#1"}, {"id": "sample#2"}]}, fixture)
            fixture.flush()
            os.environ["LOOM_BACKLOG_SOURCE"] = fixture.name
            try:
                body = _floor(self.State())
            finally:
                if prior is None:
                    os.environ.pop("LOOM_BACKLOG_SOURCE", None)
                else:
                    os.environ["LOOM_BACKLOG_SOURCE"] = prior
        self.assertIn('<strong>2</strong><span>backlog</span>', body)
        self.assertIn("bottleneck", body)
        self.assertIn('href="/t/example-owner/sample/31"', body)
        self.assertIn('href="/t/example-owner/sample/32"', body)

    def test_ticket_filters_and_empty_state(self):
        from dashboard.server import _tickets
        from urllib.parse import urlsplit
        pr = _tickets(self.State(), urlsplit("/tickets?state=pr"))
        self.assertIn("Sample ticket", pr)
        self.assertNotIn("Other ticket", pr)
        empty = _tickets(self.State(), urlsplit("/tickets?state=verified"))
        self.assertIn("Clear filters", empty)
        self.assertIn("No tickets match", empty)

    def test_ticket_goal_filter_lists_only_that_goal(self):
        from dashboard.server import _tickets
        from urllib.parse import urlsplit
        state = self.State()
        state.data = {"example-owner/sample": self.State.data["example-owner/sample"] + [
            {"repo": "example-owner/sample", "number": 41, "title": "Elsewhere ticket",
             "state": "pr", "goal": "example-owner/sample#40", "lane": "codex",
             "model": "model-x", "attempts": 1}]}
        body = _tickets(state, urlsplit("/tickets?goal=example-owner%2Fsample%2340"))
        self.assertIn("Elsewhere ticket", body)
        self.assertNotIn("Sample ticket", body)
        self.assertNotIn("Other ticket", body)
        body = _tickets(state, urlsplit("/tickets?goal=example-owner%2Fsample%2330"))
        self.assertIn("Sample ticket", body)
        self.assertIn("Other ticket", body)
        self.assertNotIn("Elsewhere ticket", body)

    def test_ticket_filters_without_params(self):
        from dashboard.server import _tickets
        from urllib.parse import urlsplit
        body = _tickets(self.State(), urlsplit("/tickets"))
        self.assertIn("Sample ticket", body)
        self.assertIn("Other ticket", body)
        self.assertIn('href="/tickets?state=pr"', body)
        self.assertIn('href="/tickets?state=briefed"', body)
        self.assertNotIn("Clear filters", body)

    def test_missing_backlog_reason(self):
        from dashboard.server import _floor
        import os
        prior = os.environ.pop("LOOM_BACKLOG_SOURCE", None)
        try:
            body = _floor(self.State())
        finally:
            if prior is not None:
                os.environ["LOOM_BACKLOG_SOURCE"] = prior
        self.assertIn("not looked", body)
        self.assertIn("LOOM_BACKLOG_SOURCE is not set", body)


class BacklogHelperTest(unittest.TestCase):
    def test_https_backlog_opener_verifies_certificates(self):
        import ssl
        from dashboard.server import _RejectRedirects, _backlog_bytes
        with patch.dict(os.environ, {"LOOM_BACKLOG_SOURCE": "https://example.com/backlog.json"}), \
                patch("dashboard.server.socket.getaddrinfo", return_value=[(None, None, None, None, ("192.0.2.1", 443))]), \
                patch("dashboard.server.ipaddress.ip_address", return_value=unittest.mock.Mock(is_global=True)), \
                patch("dashboard.server.build_opener") as build_opener:
            _backlog_bytes()
        handlers = build_opener.call_args.args
        self.assertIn(_RejectRedirects, handlers)
        https = next(handler for handler in handlers if hasattr(handler, "_context"))
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)

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
