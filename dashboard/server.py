"""HTTP routes and server-rendered views for the Loom dashboard."""
from __future__ import annotations

import html
import json
import os
import re
import ssl
import statistics
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import urlopen

from .github import TicketStore
from .shell import page

STATES = ("queued", "briefed", "running", "pr", "merged", "shipped", "verified", "failed")
WAIT_STATES = ("queued", "briefed", "running", "pr", "merged", "shipped")
from . import views


def _all_tickets(state):
    return [row for rows in state.response()["tickets"].values() if rows for row in rows]


def _ticket_href(row):
    return "/t/%s/%d" % (quote(str(row.get("repo") or ""), safe="/"), int(row.get("number") or 0))


def _ticket_row(row):
    current = row.get("state") if row.get("state") in STATES else None
    index = STATES.index(current) if current else -1
    segments = "".join('<span class="%s"></span>' % (
        "failed" if current == "failed" and i == index else
        "current" if i == index else "done" if i < index else "") for i in range(7))
    attempt = row.get("attempts")
    flag = ("attempt %s / " % html.escape(str(attempt)) if isinstance(attempt, int) and attempt > 1 else "")
    flag += html.escape(str(current or "unlabeled"))
    identifier = "%s#%s" % (row.get("repo", ""), row.get("number", ""))
    lane_model = "%s / %s" % (row.get("lane") or "unassigned", row.get("model") or "model unknown")
    return ('<a class="ticket-row" href="%s"><div class="ticket-top machine-fact"><span>%s</span>'
            '<span class="state-%s">%s</span></div><div class="ticket-title">%s</div>'
            '<div class="ticket-bottom"><div class="progress" aria-label="Ticket progress">%s</div>'
            '<span class="machine-fact">%s</span></div></a>') % (
                html.escape(_ticket_href(row), quote=True), html.escape(identifier),
                html.escape(str(current or "queued")), flag, html.escape(str(row.get("title") or "Untitled ticket")),
                segments, html.escape(lane_model))


def _backlog(state):
    source = os.environ.get("LOOM_BACKLOG_SOURCE", "").strip()
    if not source:
        return None, "LOOM_BACKLOG_SOURCE is not set"
    try:
        if source.startswith(("https://", "http://")):
            with urlopen(source, timeout=5, context=ssl.create_default_context()) as response:  # noqa: S310 - scheme is restricted above
                data = json.loads(response.read().decode("utf-8"))
        else:
            with open(source, encoding="utf-8") as handle:
                data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("epics"), list):
            epics = data["epics"]
            ready = data.get("ready_for_human", [])
            if not isinstance(ready, list):
                return None, "backlog ready_for_human must be a list"
            return len(epics) + len(ready), None
        if isinstance(data, dict):
            entries = data.get("tickets", data.get("issues", data.get("backlog", [])))
        else:
            entries = data
        if not isinstance(entries, list):
            return None, "backlog source must contain a list of tickets"
        return len(entries), None
    except (OSError, ValueError, TimeoutError) as exc:
        return None, str(exc)


def _conveyor(state, rows):
    by_state = {name: [row for row in rows if row.get("state") == name] for name in STATES}
    waits = getattr(state, "waits", None)
    waiting = [(name, waits.get(name)) for name in WAIT_STATES
               if isinstance(waits, dict) and isinstance(waits.get(name), list)
               and len(waits[name]) >= 3]
    bottleneck = max(waiting, key=lambda pair: statistics.median(pair[1]))[0] if waiting else None
    backlog_count, backlog_error = _backlog(state)
    cards = ['<a class="step-card" href="/backlog"><strong>%s</strong><span>backlog</span><small>%s</small></a>' % (
        str(backlog_count) if backlog_count is not None else "not looked",
        "open epic and ready issues" if backlog_error is None else html.escape(backlog_error))]
    for name in STATES:
        sample = waits.get(name, []) if isinstance(waits, dict) else []
        median = statistics.median(sample) if sample else None
        mark = " bottleneck" if name == bottleneck else ""
        note = ("<small>median %.1fh</small>" % median) if median is not None else "<small>median unavailable</small>"
        if name == bottleneck:
            note = "<small>bottleneck - median %.1fh</small>" % median
        cards.append('<a class="step-card state-%s%s" href="/tickets?state=%s"><strong>%d</strong><span>%s</span>%s</a>' % (
            name, mark, name, len(by_state[name]), name, note))
    return "".join(cards), bottleneck


def _floor(state):
    rows = _all_tickets(state)
    conveyor, bottleneck = _conveyor(state, rows)
    moving = [row for row in rows if row.get("state") in ("running", "pr", "briefed", "merged", "shipped")]
    context = getattr(state, "context", {}) or {}
    reports = context.get("reports", [])
    report_line = reports[0] if reports else "No goal report has been loaded yet."
    brain = context.get("journal") or "Journal not looked"
    hunt = context.get("hunt") or "Start here not looked"
    parts = ['<section class="standup">%s</section>' % html.escape(str(report_line)),
             '<section class="section"><h2>Conveyor</h2><div class="conveyor">%s</div>' % conveyor]
    if bottleneck:
        parts.append('<p class="bottleneck-note">%s is the bottleneck: longest median wait.</p>' % html.escape(bottleneck))
    parts.append('</section><div class="floor-columns"><section class="section"><h2>Moving now</h2><div class="ticket-grid">%s</div></section>' % (
        "".join(_ticket_row(row) for row in moving) or '<p class="empty-state">Nothing is moving right now.</p>'))
    parts.append('<aside class="floor-aside"><section class="card"><h2>Brain\'s next move</h2><p>%s</p></section>' % html.escape(str(brain)))
    parts.append('<section class="card"><h2>Hunt - start here</h2><p>%s</p></section>' % html.escape(str(hunt)))
    since = context.get("since_report") or {}
    parts.append('<section class="card"><h2>Since last report</h2>%s</section>' % "".join(
        '<p><b>%s</b> %s</p>' % (html.escape(key), html.escape(str(since.get(key, "not looked"))))
        for key in ("well", "not well", "stuck", "next")))
    needs = context.get("needs_you")
    parts.append('<section class="card"><h2>Needs you</h2><strong>%s</strong> - <a href="/needs-you">View asks</a></section></aside></div>' % (
        str(needs) if isinstance(needs, int) else "not looked"))
    return page("Floor", "floor", "".join(parts), status="snapshot")


def _goals(state):
    rows = _all_tickets(state)
    grouped = {}
    for row in rows:
        if row.get("goal"):
            grouped.setdefault(row["goal"], []).append(row)
    context = getattr(state, "context", {}) or {}
    goal_data = context.get("goals", {})
    cards = []
    for goal, tickets in sorted(grouped.items()):
        info = goal_data.get(goal, {}) if isinstance(goal_data, dict) else {}
        bars = info.get("history", [])[-7:]
        spark = ''.join('<i style="height:%d%%"></i>' % max(5, min(100, int(v))) for v in bars)
        counts = "".join('<a class="chip" href="/tickets?state=%s&amp;goal=%s">%s %d</a>' % (
            html.escape(name, quote=True), quote(goal, safe=""), html.escape(name),
            sum(row.get("state") == name for row in tickets)) for name in STATES
            if any(row.get("state") == name for row in tickets))
        title = info.get("title", goal)
        cards.append('<article class="goal-card card"><div class="machine-fact">%s</div><h2>%s</h2>'
                     '<div class="metric">%s</div><div class="machine-fact">%s / was %s</div>'
                     '<div class="sparkline" aria-label="Seven recent measurements">%s</div>'
                     '<div class="chips">%s</div><p>%s</p><a href="/tickets?goal=%s">%d tickets on this goal &rarr;</a></article>' % (
                         html.escape(goal), html.escape(str(title)), html.escape(str(info.get("metric", "not looked"))),
                         html.escape(str(info.get("unit", "metric"))), html.escape(str(info.get("previous", "not looked"))),
                         spark or '<span>History not looked</span>', counts,
                         html.escape(str(info.get("report", "No report loaded."))), quote(goal, safe=""), len(tickets)))
    body = '<div class="goal-grid">%s</div>' % ("".join(cards) or '<p class="empty-state">No goals with ticket data.</p>')
    return page("Goals", "goals", body, status="snapshot")


def _tickets(state, parsed):
    params = parse_qs(parsed.query)
    rows = _all_tickets(state)
    filtered = [row for row in rows if (not params.get("state") or row.get("state") == params["state"][0])
                and (not params.get("goal") or row.get("goal") == params["goal"][0])]
    state_chips = ''.join('<a class="chip" href="/tickets?state=%s">%s %d</a>' % (
        name, name, sum(row.get("state") == name for row in rows)) for name in STATES
        if any(row.get("state") == name for row in rows))
    goals = sorted({row["goal"] for row in rows if row.get("goal")})
    goal_chips = ''.join('<a class="chip" href="/tickets?goal=%s">%s</a>' % (quote(goal, safe=""), html.escape(goal)) for goal in goals)
    body = '<div class="chips">%s %s</div>' % (state_chips, goal_chips)
    if filtered:
        body += '<div class="ticket-grid">%s</div>' % ''.join(_ticket_row(row) for row in filtered)
    else:
        body += '<div class="empty-state"><p>No tickets match these filters.</p><a class="chip" href="/tickets">Clear filters</a></div>'
    return page("Tickets", "tickets", body, status="snapshot")


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
            if path == "/":
                return self.send_page(_floor(state))
            if path == "/goals":
                return self.send_page(_goals(state))
            if path == "/tickets":
                return self.send_page(_tickets(state, parsed))
            if path in ("/static/loom.css", "/static/loom.js"):
                asset = Path(__file__).parent / "static" / path.rsplit("/", 1)[1]
                content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
                return self.send_bytes(200, asset.read_bytes(), content_type)
            if path in ("/improving", "/lanes"):
                return _send_dashboard_view(self, state, path)
            detail = _ticket_detail(state, path)
            if detail is not None:
                return self.send_page(*detail)
            return self.send_text(404, "not found")

        def send_json(self, code, value):
            body = json.dumps(value).encode("utf-8")
            self.send_bytes(code, body, "application/json; charset=utf-8")

        def send_page(self, body, code=200):
            document = body if body.lstrip().lower().startswith("<!doctype html>") else (
                "<!doctype html><meta charset=utf-8><title>Loom</title>" + body)
            if state.stale:
                document = document.replace('<main class="page-content">', '<main class="page-content"><p class="stale-note">Showing stale ticket data.</p>')
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
