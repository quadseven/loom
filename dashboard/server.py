"""HTTP routes for the Loom dashboard."""
from __future__ import annotations

import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .github import TicketStore


def _ticket_page(state, parsed):
    params = parse_qs(parsed.query)
    tickets = state.response()["tickets"]
    records = [row for repo_rows in tickets.values() if repo_rows
               for row in repo_rows if (not params.get("state") or row["state"] == params["state"][0])
               and (not params.get("goal") or row["goal"] == params["goal"][0])]
    return {"filtered": records}


def _ticket_detail(state, path):
    match = re.fullmatch(r"/t/([^/]+)/([^/]+)/([0-9]+)", path)
    if not match:
        return None
    repo = "%s/%s" % (unquote(match.group(1)), unquote(match.group(2)))
    number = int(match.group(3))
    rows = state.data.get(repo)
    row = next((item for item in rows or [] if item["number"] == number), None)
    body = "<h1>%s</h1>" % html.escape(row["title"]) if row else "<h1>Ticket not found</h1>"
    return body, 200 if row else 404


def _handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/livez":
                return self.send_text(200, "live")
            if path == "/readyz":
                return self.send_text(200 if state.ready else 503, "ready" if state.ready else "not ready")
            if path == "/api/tickets":
                return self.send_json(200, state.response())
            if path in ("/", "/tickets"):
                tickets = state.response()["tickets"]
                data = tickets if path == "/" else _ticket_page(state, parsed)
                return self.send_page(self.ticket_list(data))
            detail = _ticket_detail(state, path)
            if detail is not None:
                return self.send_page(detail[0], detail[1])
            return self.send_text(404, "not found")

        def ticket_list(self, repos):
            rows = repos.get("filtered") if "filtered" in repos else [row for items in repos.values() if items for row in items]
            output = ["<h1>Loom tickets</h1>"]
            if state.stale:
                output.append("<p>Showing stale ticket data.</p>")
            output.append("<ul>")
            for row in rows:
                href = "/t/%s/%d" % (html.escape(row["repo"], quote=True), row["number"])
                output.append('<li><a href="%s">%s</a> [%s]</li>' % (href, html.escape(str(row["title"])), html.escape(str(row["state"]))))
            output.append("</ul>")
            return "".join(output)

        def send_json(self, code, value):
            body = json.dumps(value).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_page(self, body, code=200):
            self.send_bytes(code, ("<!doctype html><meta charset=utf-8><title>Loom</title>" + body).encode("utf-8"), "text/html; charset=utf-8")

        def send_text(self, code, value):
            self.send_bytes(code, value.encode("utf-8"), "text/plain; charset=utf-8")

        def send_bytes(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    return Handler


def make_server(host: str = "", port: int = 8080, store: TicketStore = None):
    state = store or TicketStore()

    class DashboardHTTPServer(ThreadingHTTPServer):
        def server_close(self):
            state.stop_background()
            super().server_close()

    server = DashboardHTTPServer((host, port), _handler_for(state))
    state.start_background()
    return server
