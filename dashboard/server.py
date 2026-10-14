"""HTTP routes for the Loom dashboard."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .github import TicketStore
from . import views
from .shell import page


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
            return _dispatch_get(self, state)

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
            document = body if body.lstrip().lower().startswith("<!doctype html>") else (
                "<!doctype html><meta charset=utf-8><title>Loom</title>" + body)
            self.send_bytes(code, document.encode("utf-8"), "text/html; charset=utf-8")

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


def _dispatch_get(handler, state):
    parsed = urlsplit(handler.path)
    path = parsed.path
    if path == "/livez":
        return handler.send_text(200, "live")
    if path == "/readyz":
        return handler.send_text(200 if state.ready else 503, "ready" if state.ready else "not ready")
    if path == "/api/tickets":
        return handler.send_json(200, state.response())
    if path in ("/static/loom.css", "/static/loom.js"):
        asset = Path(__file__).parent / "static" / path.rsplit("/", 1)[1]
        content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
        return handler.send_bytes(200, asset.read_bytes(), content_type)
    if path in ("/improving", "/lanes"):
        return _send_dashboard_view(handler, state, path)
    if path in ("/", "/tickets"):
        tickets = state.response()["tickets"]
        data = tickets if path == "/" else _ticket_page(state, parsed)
        return handler.send_page(handler.ticket_list(data))
    detail = _ticket_detail(state, path)
    if detail is not None:
        return handler.send_page(detail[0], detail[1])
    return handler.send_text(404, "not found")


def _send_dashboard_view(handler, state, path):
    payload = state.response()
    tickets = [row for rows in payload["tickets"].values() if rows for row in rows]
    status = "stale" if payload["stale"] else "live"
    if path == "/improving":
        source = payload.get("improving", {})
        data = views.build_improvements(source.get("journals", []), tickets,
                                        source.get("metrics_comments", []))
        body = views.render_improving(data)
        title, active = "Improving", "improving"
    else:
        body = views.render_lanes(_lane_view_data(payload, tickets))
        title, active = "Lanes and models", "lanes"
    if payload.get("dashboard_errors"):
        body += '<p class="empty-state">Some dashboard sources could not be read: %s</p>' % html.escape("; ".join(payload["dashboard_errors"]))
    return handler.send_page(page(title, active, body, status=status))


def make_server(host: str = "", port: int = 8080, store: TicketStore = None):
    state = store or TicketStore()

    class DashboardHTTPServer(ThreadingHTTPServer):
        def server_close(self):
            state.stop_background()
            super().server_close()

    server = DashboardHTTPServer((host, port), _handler_for(state))
    state.start_background()
    return server


def _lane_view_data(payload, tickets):
    comments = payload.get("lanes", [])
    latest = max(comments, key=lambda c: str(c.get("created_at") or ""), default=None)
    lane_rows = views.parse_lane_rows(latest.get("body") if latest else "")
    lanes = []
    for item in lane_rows:
        active = next((ticket for ticket in tickets if ticket.get("lane") == item["lane"]
                       and ticket.get("state") in ("running", "pr")), None)
        item.update({"ticket": active, "model": active.get("model") if active else None,
                     "quota": None,
                     "reason": None if item["available"] else "held reason missing from lane status comment"})
        lanes.append(item)
    prs = payload.get("pull_requests", [])
    tickets_by_ref = {(row.get("repo"), str(row.get("number"))): row for row in tickets}
    prepared = []
    footer = views.FOOTER
    anthropic = total = 0
    for pr in prs:
        match = footer.search(str(pr.get("body") or ""))
        if not match:
            continue
        repo, number, lane, model = match.groups()
        ticket = tickets_by_ref.get((repo, number))
        prepared.append({**pr, "tier": ticket.get("difficulty") if ticket else None})
        total += 1
        anthropic += lane.startswith("anthropic")
    metric_rows = views.parse_metrics(payload.get("improving", {}).get("metrics_comments", []))
    share = None
    if metric_rows:
        spread = metric_rows[0][1].get("Spread", {}).get("value", "")
        match = re.search(r"Anthropic\s+(\d+)/(\d+)", spread)
        if match and int(match.group(2)):
            share = round(100 * int(match.group(1)) / int(match.group(2)))
    return {"lanes": lanes, "scoreboard": views.build_scoreboard(prepared),
            "anthropic_share": share}
