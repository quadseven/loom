"""Dashboard HTTP and GitHub behavior using a local API stub."""
import json
import os
import http.client
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from bus.github import GitHub
from dashboard.github import TicketStore
from dashboard import views
from dashboard.server import make_server

from tests import sandbox_caps


@unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
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

    def test_backlog_bytes_order_next_child_and_health_inspectors(self):
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
                self.assertIn("First next", body[body.index('<table class="epic-table">'):])
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

    def test_stale_banner_names_age_reason_and_next_refresh(self):
        self.store._snapshot.update({"stale": True, "ticket_refreshed_at": time.time() - 2400,
                                     "stale_reason": "GitHub's request budget ran low",
                                     "next_refresh_at": time.time() + 600})
        _, _, body = self.raw_get("/")
        page = body.decode()
        self.assertRegex(page, r"Ticket data is 40 minutes old: GitHub's request budget ran low")
        self.assertIn("refreshes at", page)
        self.assertIn("ET", page)

    def test_no_page_uses_internal_budget_wording(self):
        for path in ("/", "/results", "/brain", "/hot-spots"):
            _, _, body = self.raw_get(path)
            page = body.decode().lower()
            self.assertNotIn("optional view sources", page)
            self.assertNotIn("below its floor", page)

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

    def test_a_budget_below_the_floor_is_forgotten_once_github_resets_the_window(self):
        # A pass below the floor reads nothing, so nothing updated the number:
        # the page said "stale ticket data" for a day with 4,265 requests left.
        self.store.refresh()
        self.store._last_remaining = 3
        self.store.budget_floor = 20
        self.store._reset_at = time.time() + 600
        self.assertTrue(self.store._below_floor())
        before = len(self.api_calls)
        self.store.refresh()
        self.assertEqual(len(self.api_calls), before)
        self.assertTrue(self.get_json("/api/tickets")["stale"])
        self.store._reset_at = time.time() - 1
        self.store.budget_floor = 5
        self.assertFalse(self.store._below_floor())
        self.store.refresh()
        self.assertGreater(len(self.api_calls), before)
        self.assertFalse(self.get_json("/api/tickets")["stale"])

    def test_below_the_floor_without_a_reset_time_the_next_refresh_is_unknown(self):
        # A pass below the floor reads nothing, so "now plus the interval" is
        # not when the data refreshes; without GitHub's reset time it is unknown.
        self.store.refresh()
        self.store._last_remaining, self.store.budget_floor, self.store._reset_at = 3, 20, 0
        self.store.refresh()
        payload = self.get_json("/api/tickets")
        self.assertTrue(payload["stale"])
        self.assertIsNone(payload["next_refresh_at"])
        self.store._reset_at = int(time.time()) + 600
        self.store.refresh()
        self.assertEqual(self.get_json("/api/tickets")["next_refresh_at"], self.store._reset_at)

    def raw_get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def test_the_dashboard_has_a_favicon_in_every_form_a_browser_asks_for(self):
        status, kind, body = self.raw_get("/static/favicon.svg")
        self.assertEqual((status, kind), (200, "image/svg+xml"))
        self.assertIn(b"<svg", body)
        for path in ("/favicon.ico", "/static/favicon-32.png", "/static/apple-touch-icon.png"):
            status, kind, body = self.raw_get(path)
            self.assertEqual((status, kind), (200, "image/png"), path)
            self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"), path)
        _, _, page = self.raw_get("/")
        for link in (b'rel="icon" type="image/svg+xml" href="/static/favicon.svg"',
                     b'rel="apple-touch-icon" href="/static/apple-touch-icon.png"'):
            self.assertIn(link, page)

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
            return {"tickets": self.data, "wait_rows": [
                {"state": "queued", "median_h": 1.5, "worst_h": 4, "n": 3},
                {"state": "pr", "median_h": 5.5, "worst_h": 9, "n": 4},
                {"state": "verified", "median_h": 20, "worst_h": 30, "n": 8}],
                "pull_requests": [{"updated_at": "2026-10-14", "body": "Loom ticket example-owner/sample#32, lane codex, model model-y."}]}

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
        self.assertIn("median 1.5h", body)
        self.assertIn("worst 9.0h", body)
        self.assertIn("state-pr bottleneck", body)
        self.assertNotIn("state-verified bottleneck", body)
        self.assertIn("model-y", body)

    def test_goal_home_report_journal_and_needs_you_render(self):
        from dashboard.github import _floor_context
        from dashboard.server import _floor
        state = self.State()
        state.context = _floor_context(
            [{"body": "<!-- loom:report v1 -->\nProgress improved: 4 tickets verified.\n"
                      "Going well: Four tickets shipped\nNot going well: One check failed\n"
                      "Still stuck since last report: One item for two days\nNext: Review the next ticket",
              "created_at": "2026-10-14T12:00:00Z"}],
            [{"body": "<!-- loom:journal v1 -->\nTick 2026-10-14T12:10:00Z, brain\n"
                      "- Next: Review the next ticket", "created_at": "2026-10-14T12:10:00Z"}],
            [{}, {}], [])
        body = _floor(state)
        # The Floor reads times as "Oct 14 12:10 UTC" and labels the rows
        # well / not well / stuck / next, as the design does.
        for item in ("Progress improved: 4 tickets verified.", ">well<", "Four tickets shipped",
                     ">not well<", "One check failed", ">stuck<", "One item for two days",
                     ">next<", "Review the next ticket", "brain", "Oct 14 12:10 UTC",
                     '<strong class="count">2</strong>', 'href="/brain"'):
            self.assertIn(item, body)

    def test_missing_goal_sources_keep_not_looked_reason(self):
        from dashboard.server import _floor
        state = self.State()
        state.context = {"report_reason": "goal home unavailable", "journal_reason": "goal home unavailable",
                         "needs_you_reason": "needs-operator source unavailable"}
        body = _floor(state)
        self.assertIn("not looked: goal home unavailable", body)
        self.assertIn("not looked: needs-operator source unavailable", body)

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

    def test_ticket_model_filter_lists_only_that_model(self):
        from dashboard.server import _tickets
        from urllib.parse import urlsplit
        body = _tickets(self.State(), urlsplit("/tickets?model=model-x"))
        self.assertIn("Sample ticket", body)
        self.assertNotIn("Other ticket", body)
        body = _tickets(self.State(), urlsplit("/tickets?model=model-y"))
        self.assertIn("Other ticket", body)
        self.assertNotIn("Sample ticket", body)
        body = _tickets(self.State(), urlsplit("/tickets"))
        self.assertIn('href="/tickets?model=model-x"', body)
        self.assertIn('href="/tickets?model=model-y"', body)

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


class LanesAndImprovingTest(unittest.TestCase):
    def test_lane_tier_text_reads_factory_tables(self):
        from unittest.mock import patch
        from bus import factory
        from dashboard.views import render_lanes
        with patch.dict(factory.TIERS, {"easy": ("test-lane",)}):
            body = render_lanes({"lanes": [], "scoreboard": {}})
        self.assertIn("Easy: test-lane", body)

    def test_pull_request_footer_and_models_scoreboard(self):
        from dashboard import views
        from dashboard.server import _lane_view_data

        pr_body = (
            "## Summary\n"
            "This fixes the issue.\n\n"
            "---\n"
            "Loom ticket quadseven.loom-282-r2, lane codex, model gpt-6-luna.\n"
        )
        self.assertEqual(views.footer(pr_body), ("quadseven/loom", "282", "codex", "gpt-6-luna"))

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

        ticket = {"repo": "quadseven/loom", "number": 282, "difficulty": "medium", "state": "closed"}
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
        self.assertEqual(html_out.count("metric-prev"), 9)
        for num_name in ("Metric One", "Metric Two", "Metric Three", "Metric Four",
                         "Metric Five", "Metric Six", "Metric Seven", "Metric Eight", "Metric Nine"):
            self.assertIn(num_name, html_out)
        for previous in ("40", "8", "80%", "15", "95", "4", "0"):
            self.assertIn("was %s /" % previous, html_out)
        self.assertIn("better", html_out)


class FactoryNumberDirectionTest(unittest.TestCase):
    def render(self, was, now):
        data = {"records": [], "metrics_available": True,
                "latest": {"Flow": {"value": "v2", "passed": now}},
                "previous": {"Flow": {"value": "v1", "passed": was}}}
        return views.render_improving(data)

    def test_a_number_that_held_its_side_says_still(self):
        self.assertIn("was v1 / still passing", self.render(True, True))
        self.assertIn("was v1 / still failing", self.render(False, False))

    def test_a_flip_says_better_or_worse(self):
        self.assertIn("was v1 / better", self.render(False, True))
        self.assertIn("was v1 / worse", self.render(True, False))

    def test_no_friction_lines_says_so(self):
        self.assertIn("No friction lines in the journals yet.", self.render(True, True))
class LaneCardsTest(unittest.TestCase):
    def test_status_comment_renders_heartbeat_time_and_never_heartbeat_available(self):
        from dashboard.views import render_lanes
        from dashboard.server import _lane_view_data
        comment = (
            "<!-- loom:lanes v1 -->\n"
            "Updated 2026-10-14 20:17:00Z\n\n"
            "| lane | available | heartbeat (UTC) |\n"
            "|---|---|---|\n"
            "| codex | yes | 2026-10-14 20:17:00Z |\n"
            "| claude | yes | 20:17Z |\n"
            "| agy | no | 2026-10-14 19:45:00Z |\n"
        )
        payload = {"lanes": [{"body": comment, "created_at": "2026-10-14T20:17:00Z"}],
                   "pull_requests": [], "tickets": {}}
        data = _lane_view_data(payload, [])
        body = render_lanes(data)
        self.assertNotIn("heartbeat available", body)
        self.assertIn("heartbeat 20:17Z", body)
        self.assertIn("heartbeat 19:45Z", body)
        for lane in ("codex", "claude", "agy"):
            self.assertIn(lane, body)

    def test_running_ticket_has_separator_before_heartbeat_line(self):
        from dashboard.views import render_lanes
        from dashboard.server import _lane_view_data
        comment = (
            "<!-- loom:lanes v1 -->\n"
            "| lane | available | heartbeat (UTC) |\n"
            "|---|---|---|\n"
            "| codex | yes | 20:17Z |\n"
        )
        tickets = [{"repo": "example-owner/sample", "number": 362, "title": "Lane cards",
                    "state": "running", "lane": "codex"}]
        payload = {"lanes": [{"body": comment}], "pull_requests": [], "tickets": {}}
        data = _lane_view_data(payload, tickets)
        body = render_lanes(data)
        self.assertIn("Lane cards</a> - heartbeat 20:17Z", body)

    def test_pull_request_footer_shows_model_and_missing_says_model_missing(self):
        from dashboard.views import render_lanes
        from dashboard.server import _lane_view_data
        comment = (
            "<!-- loom:lanes v1 -->\n"
            "| lane | available | heartbeat (UTC) |\n"
            "|---|---|---|\n"
            "| codex | yes | 20:17Z |\n"
            "| claude | yes | 20:17Z |\n"
        )
        prs = [{
            "body": "Fixes #1\n\nLoom ticket example-owner/sample#31, lane codex, model gpt-6-sol.\n",
            "created_at": "2026-10-14T12:00:00Z",
        }]
        payload = {"lanes": [{"body": comment}], "pull_requests": prs, "tickets": {}}
        data = _lane_view_data(payload, [])
        body = render_lanes(data)
        self.assertIn("gpt-6-sol", body)
        self.assertIn("model missing", body)

    def test_the_runners_ticket_name_footer_names_the_lanes_model(self):
        from dashboard.views import footer, render_lanes
        from dashboard.server import _lane_view_data
        self.assertEqual(footer("Loom ticket example-owner.my-repo-31-r2, lane codex-2, model gpt-6-sol."),
                         ("example-owner/my-repo", "31", "codex-2", "gpt-6-sol"))
        quoted = "The footer reads `Loom ticket example-owner.my-repo-31, lane codex, model gpt-6-luna.` (doc)"
        self.assertIsNone(footer(quoted))
        comment = ("<!-- loom:lanes v1 -->\n| lane | available | heartbeat (UTC) |\n"
                   "|---|---|---|\n| codex-2 | yes | 20:17Z |\n")
        prs = [{"body": "Loom ticket example-owner.my-repo-31-r2, lane codex-2, model gpt-6-sol.\n",
                "created_at": "2026-10-14T12:00:00Z"}]
        body = render_lanes(_lane_view_data({"lanes": [{"body": comment}], "pull_requests": prs,
                                             "tickets": {}}, []))
        self.assertIn("gpt-6-sol", body)

    def test_usage_record_shows_quota_bar_and_missing_says_quota_missing(self):
        from dashboard.views import render_lanes
        from dashboard.server import _lane_view_data
        comment = (
            "<!-- loom:lanes v1 -->\n"
            "| lane | available | heartbeat (UTC) |\n"
            "|---|---|---|\n"
            "| codex | yes | 20:17Z |\n"
            "| claude | yes | 20:17Z |\n"
        )
        usage = [
            {"provider": "codex", "lanes": ["codex"], "window": "weekly", "used_pct": 65,
             "as_of": "2026-10-14T12:00:00Z", "source": "test"}
        ]
        payload = {"lanes": [{"body": comment}], "pull_requests": [], "tickets": {}, "usage": usage}
        data = _lane_view_data(payload, [])
        body = render_lanes(data)
        self.assertIn('<div class="quota"><span style="width:65%"></span></div>', body)
        self.assertIn("quota missing", body)

    def test_lanes_http_route_integration(self):
        from dashboard.server import _dispatch_get
        from dashboard.usage import Store
        class State:
            stale = False
            usage = Store()
            def response(self):
                comment = (
                    "<!-- loom:lanes v1 -->\n"
                    "| lane | available | heartbeat (UTC) |\n"
                    "|---|---|---|\n"
                    "| codex | yes | 20:17Z |\n"
                    "| claude | yes | 20:17Z |\n"
                )
                prs = [{
                    "body": "Loom ticket example-owner/sample#31, lane codex, model gpt-6-sol.\n",
                    "created_at": "2026-10-14T12:00:00Z",
                }]
                return {"tickets": {}, "lanes": [{"body": comment}], "pull_requests": prs,
                        "stale": False, "dashboard_errors": []}
        state = State()
        state.usage.upload([
            {"provider": "codex", "lanes": ["codex"], "window": "5-hour", "used_pct": 42,
             "as_of": "2026-10-14T12:00:00Z", "source": "test"}
        ])
        class Handler:
            path = "/lanes"
            def send_page(self, body, code=200):
                self.result = (code, body)
        handler = Handler()
        _dispatch_get(handler, state)
        body = handler.result[1]
        self.assertNotIn("heartbeat available", body)
        self.assertIn("heartbeat 20:17Z", body)
        self.assertIn("gpt-6-sol", body)
        self.assertIn('<div class="quota"><span style="width:42%"></span></div>', body)
        self.assertIn("quota missing", body)
        self.assertIn("model missing", body)


if __name__ == "__main__":
    unittest.main()
