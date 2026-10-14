"""HTTP routes for the Loom dashboard."""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener

from . import views
from .github import TicketStore
from . import insights
from .shell import page


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise HTTPError(request.full_url, code, "backlog source redirects are disabled", headers, None)


def _backlog_bytes():
    source = os.environ.get("LOOM_BACKLOG_SOURCE", "").strip()
    if not source:
        return None
    try:
        parsed = urlsplit(source)
        if parsed.scheme in ("http", "https"):
            if parsed.scheme != "https" or not parsed.hostname:
                return None
            addresses = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
            if not addresses or any(not address.is_global for address in addresses):
                return None
            opener = build_opener(_RejectRedirects)
            with opener.open(source, timeout=5) as response:
                return response.read()
        return Path(source).read_bytes()
    except (OSError, ValueError):
        return None


def _backlog_document(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _repo_number(item):
    repo = item.get("repo") or item.get("repository") or ""
    if isinstance(repo, dict):
        repo = repo.get("full_name", "")
    number = item.get("number")
    return "%s#%s" % (repo, number) if repo and number is not None else ""


def _issue_url(item):
    return item.get("html_url") or item.get("url") or "/tickets"


def _health_inspectors(document):
    epics = document.get("epics") or []
    finished, unlinked, titles = [], [], []
    for epic in epics:
        children = epic.get("children") or []
        if children and all(str(child.get("state", "")).lower() == "closed" for child in children) and str(epic.get("state", "open")).lower() != "closed":
            finished.append(epic)
        for child in children:
            if not child.get("parent") and not child.get("parent_number") and not child.get("parent_url"):
                unlinked.append(child)
        title = str(epic.get("title", ""))
        if title and (title != title.strip() or title.startswith(("[", "#")) or len(title) > 120):
            titles.append(epic)
    return (("Finished children, open epic", finished), ("Children without parent link", unlinked),
            ("Nonstandard titles", titles))


def _backlog_filters(epics, parsed):
    params = parse_qs(parsed.query)
    repo_filter, tier_filter = (params.get("repo") or [""])[0], (params.get("tier") or [""])[0]
    repos = sorted({str(epic.get("repo", "")) for epic in epics if epic.get("repo")})
    tiers = sorted({str(epic.get("tier", "")) for epic in epics if epic.get("tier")})
    out = ['<form method="get" class="card backlog-filters"><label>Repo <select name="repo"><option value="">All repos</option>']
    out.extend('<option value="%s"%s>%s</option>' % (html.escape(repo, quote=True), ' selected' if repo == repo_filter else '', html.escape(repo)) for repo in repos)
    out.append('</select></label> <label>Tier <select name="tier"><option value="">All tiers</option>')
    out.extend('<option value="%s"%s>%s</option>' % (html.escape(tier, quote=True), ' selected' if tier == tier_filter else '', html.escape(tier)) for tier in tiers)
    out.append('</select></label> <button type="submit">Filter</button></form>')
    selected = [epic for epic in epics if (not repo_filter or epic.get("repo") == repo_filter)
                and (not tier_filter or str(epic.get("tier", "")) == tier_filter)]
    return out, selected


def _render_epic(epic, states):
    children = epic.get("children") or []
    closed = sum(child.get("closed", False) or str(child.get("state", child.get("status", ""))).lower() == "closed" for child in children)
    next_item = epic.get("next") or {}
    criteria = next_item.get("criteria") or next_item.get("acceptance_criteria") or []
    checked = sum(bool(item.get("checked")) if isinstance(item, dict) else str(item).lstrip().startswith(("[x]", "- [x]")) for item in criteria)
    markings = epic.get("markings") or []
    if not isinstance(markings, list):
        markings = [markings]
    out = ['<article class="card backlog-epic"><div class="label">%s</div><h2><a href="%s">%s</a></h2><p>%s | %s | %s</p><p>%s</p><p>Completeness: %d/%d | Next: %s (%d/%d criteria)</p><ul>' % (
        html.escape(str(epic.get("tier", "Cave"))), html.escape(_issue_url(epic), quote=True), html.escape(_repo_number(epic)),
        html.escape(str(epic.get("title", ""))), html.escape(str(epic.get("tier", ""))), html.escape(str(epic.get("score", ""))),
        html.escape(str(epic.get("why", ""))), closed, len(children), html.escape(str(next_item.get("title") or next_item.get("number") or "None")), checked, len(criteria))]
    if markings:
        out.append('<p>%s</p>' % html.escape("Markings: " + ", ".join(str(marking) for marking in markings)))
    for child in children:
        key = "%s#%s" % (child.get("repo", epic.get("repo", "")), child.get("number", ""))
        label = " | Loom: " + str(states[key]) if states.get(key) else ""
        item = {**epic, **child}
        out.append('<li><a href="%s">%s</a>%s</li>' % (html.escape(_issue_url(item), quote=True), html.escape(_repo_number(item) or str(child.get("title", ""))), html.escape(label)))
    out.append('</ul><p>Open children: %d</p></article>' % (len(children) - closed))
    return "".join(out)


def _start_here(epics):
    if not epics:
        return ""
    first = epics[0]
    nxt = first.get("next") or {}
    return '<section class="card start-here"><span class="label">Start here</span><h2>%s</h2><p><a href="%s">%s</a></p></section>' % (
        html.escape(str(first.get("title", ""))), html.escape(_issue_url({**first, **nxt}), quote=True),
        html.escape(str(nxt.get("title") or nxt.get("number") or "Next ticket")))


def _ticket_states(state):
    return {"%s#%s" % (repo, row.get("number")): row.get("state")
            for repo, rows in state.data.items() for row in rows or []}


def _render_inspectors(document):
    out = []
    for title, items in _health_inspectors(document):
        out.append('<section class="card"><h2>%s</h2><ul>' % html.escape(title))
        for item in items:
            out.append('<li><a href="%s">%s</a></li>' % (html.escape(_issue_url(item), quote=True), html.escape(_repo_number(item) or str(item.get("title", "")))))
        out.append('</ul></section>')
    return "".join(out)


def _backlog_page(document, parsed, state):
    all_epics = document.get("epics") or []
    out, epics = _backlog_filters(all_epics, parsed)
    out.append(_start_here(all_epics))
    states = _ticket_states(state)
    out.extend(_render_epic(epic, states) for epic in epics)
    out.append(_render_inspectors(document))
    return page("Backlog", "backlog", "".join(out), status="Read only")


def _needs_you(document, state, *, source_missing=False):
    merged = {}
    for field in ("needs_operator", "ready_for_human"):
        for item in document.get(field) or []:
            key = _repo_number(item)
            if key:
                merged.setdefault(key, {**item, "queue": field})
    for item in getattr(state, "operator_items", []):
        key = _repo_number(item)
        if key:
            merged.setdefault(key, item)
    items = sorted(merged.values(), key=lambda item: (-_number(item.get("unblocks")), str(item.get("waiting_since", "9999"))))
    out = []
    if source_missing:
        out.append('<p class="card">Backlog source unavailable.</p>')
    for item in items:
        ask = item.get("ask") or item.get("body") or item.get("title", "")
        waiting = "idle" if item.get("waiting_since_source") == "updated" else "waiting"
        out.append('<article class="card"><div class="label">%s | %s</div><h2><a href="%s">%s</a></h2><blockquote>%s</blockquote><p>%s | unblocks %s</p></article>' % (
            html.escape(str(item.get("queue", "needs_operator"))), waiting, html.escape(_issue_url(item), quote=True),
            html.escape(_repo_number(item)), html.escape(str(ask)), waiting, html.escape(str(item.get("unblocks", 0)))))
    counts = getattr(state, "human_counts", None) or document.get("loom_human_counts") or {}
    out.append('<footer>loom:human counts by repo: %s</footer>' % html.escape(", ".join("%s: %s" % (repo, count) for repo, count in counts.items())))
    return page("Needs you", "needs-you", "".join(out), status="Read only")


def _number(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
    if path in ("/backlog", "/backlog.json", "/needs-you"):
        raw = _backlog_bytes()
        if path == "/backlog.json":
            return handler.send_bytes(503, b"backlog source unavailable", "text/plain; charset=utf-8") if raw is None else handler.send_bytes(200, raw, "application/json")
        if path == "/backlog":
            if raw is None:
                return handler.send_page(page("Backlog", "backlog", "<p>Backlog source unavailable.</p>", status="Unavailable"), 503)
            return handler.send_page(_backlog_page(_backlog_document(raw), parsed, state))
        return handler.send_page(_needs_you(_backlog_document(raw or b"{}"), state, source_missing=raw is None))
    if path in ("/static/loom.css", "/static/loom.js"):
        asset = Path(__file__).parent / "static" / path.rsplit("/", 1)[1]
        content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
        return handler.send_bytes(200, asset.read_bytes(), content_type)
    if path in ("/improving", "/lanes"):
        return _send_dashboard_view(handler, state, path)
    if path == "/brain":
        return handler.send_page(page("Brain", "brain", insights.brain_page(state), status="live"))
    if path == "/hot-spots":
        return handler.send_page(page("Hot spots", "hot-spots", insights.hotspots_page(state), status="live"))
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
