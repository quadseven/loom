"""HTTP routes and server-rendered views for the Loom dashboard."""
from __future__ import annotations

import html
import json
import os
import re
import hmac
import ssl
import statistics
import socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
import ipaddress
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener

from .github import TicketStore
from .shell import page
from . import transcripts as transcript_store
from . import usage as usage_store
from . import ticket as ticket_view

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
        raw = _backlog_bytes()
        if raw is None:
            return None, "backlog source unavailable or rejected"
        data = json.loads(raw.decode("utf-8"))
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
    except (OSError, ValueError, TimeoutError, UnicodeDecodeError) as exc:
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
            # Verify the public source with system CAs and never follow redirects.
            opener = build_opener(_RejectRedirects, HTTPSHandler(context=ssl.create_default_context()))
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


def _tickets(state, parsed):
    return _ticket_page(state, parsed)


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


def _handle_transcript_post(handler, state):
    token = os.environ.get("LOOM_UPLOAD_TOKEN", "")
    supplied = handler.headers.get("Authorization", "")
    if not token or not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], token):
        return handler.send_json(401, {"error": "unauthorized"})
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 1 or length > 2_000_000:
            raise ValueError("invalid request size")
        body = json.loads(handler.rfile.read(length))
        store = getattr(state, "transcripts", None)
        if store is None:
            store = transcript_store.Store()
            state.transcripts = store
        return handler.send_json(201, store.upload(body))
    except (ValueError, json.JSONDecodeError) as exc:
        return handler.send_json(400, {"error": str(exc)})
    except RuntimeError as exc:
        return handler.send_json(503, {"error": str(exc)})
    except transcript_store.storage_errors():
        return handler.send_json(503, {"error": "transcript storage unavailable"})


def _handle_usage_post(handler, state):
    token = os.environ.get("LOOM_UPLOAD_TOKEN", "")
    supplied = handler.headers.get("Authorization", "")
    if not token or not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], token):
        return handler.send_json(401, {"error": "unauthorized"})
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 1 or length > 2_000_000:
            raise ValueError("invalid request size")
        body = json.loads(handler.rfile.read(length))
        store = getattr(state, "usage", None)
        if store is None:
            store = usage_store.Store()
            state.usage = store
        return handler.send_json(201, store.upload(body))
    except (ValueError, json.JSONDecodeError) as exc:
        return handler.send_json(400, {"error": str(exc)})


def _handler_for(state):
    """Keep GET dispatch small; route groups are handled by module helpers."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            return _dispatch_get(self, state)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/usage":
                return _handle_usage_post(self, state)
            if path != "/api/transcripts":
                return self.send_text(404, "not found")
            return _handle_transcript_post(self, state)

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
    if re.fullmatch(r"/api/tickets/[^/]+/[^/]+/[0-9]+(?:/runs(?:/[0-9]+)?)?", path):
        return _dispatch_ticket_api(handler, state, parsed)
    if path in ("/backlog", "/backlog.json", "/needs-you"):
        return _dispatch_backlog(handler, state, path, parsed)
    if path in ("/static/loom.css", "/static/loom.js"):
        return _dispatch_static(handler, path)
    if path in ("/improving", "/lanes"):
        return _send_dashboard_view(handler, state, path)
    if path == "/usage":
        return _dispatch_usage(handler, state)
    if path in ("/", "/goals", "/tickets"):
        return _dispatch_view(handler, state, path, parsed)
    if re.fullmatch(r"/t/[^/]+/[^/]+/[0-9]+", path):
        return _dispatch_ticket_page(handler, state, path, parsed)
    return _dispatch_basic(handler, state, path)


def _dispatch_ticket_api(handler, state, parsed):
    path = parsed.path
    api_match = re.fullmatch(r"/api/tickets/([^/]+)/([^/]+)/([0-9]+)(?:/runs(?:/([0-9]+))?)?", path)
    owner, name, number, run_number = api_match.groups()
    repo = unquote(owner) + "/" + unquote(name)
    record = next((row for row in (state.data.get(repo) or []) if int(row.get("number", -1)) == int(number)), None)
    if record is None:
        return handler.send_json(404, {"error": "ticket missing from current snapshot"})
    store = _transcript_store(state)
    runs = store.attempts(repo + "#" + number) if store else []
    if run_number is not None:
        return _dispatch_run(handler, parsed, store, runs, run_number)
    if path.endswith("/runs"):
        reason = None if store else "transcript store unavailable: configure dashboard database or explicit memory mode"
        return handler.send_json(200, {"runs": runs, "missing_reason": reason})
    payload = state.response()
    prs = [pr for pr in payload.get("pull_requests", []) if re.search(r"(?:/issues/|/pull/)" + re.escape(number) + r"(?:\b|$)", str(pr.get("body") or ""))]
    return handler.send_json(200, {"ticket": record, "checks": ticket_view.checks(record.get("body")),
                                   "pull_request": prs, "timeline": [],
                                   "timeline_missing": "ticket comments and label events are not in the current snapshot"})


def _transcript_store(state):
    store = getattr(state, "transcripts", None)
    if store is None:
        try:
            store = transcript_store.Store()
            state.transcripts = store
        except RuntimeError:
            return None
    return store


def _dispatch_run(handler, parsed, store, runs, run_number):
    if not 1 <= int(run_number) <= len(runs):
        return handler.send_json(404, {"error": "attempt missing"})
    run = runs[int(run_number) - 1]
    full = store.get(run["id"])
    after = parse_qs(parsed.query).get("after", [""])[0]
    events = [event for event in (full or {}).get("events", []) if not after or event.get("ts", "") > after]
    return handler.send_json(200, {"run": run, "events": events})


def _dispatch_backlog(handler, state, path, parsed):
    raw = _backlog_bytes()
    if path == "/backlog.json":
        return handler.send_bytes(503, b"backlog source unavailable", "text/plain; charset=utf-8") if raw is None else handler.send_bytes(200, raw, "application/json")
    if path == "/backlog":
        if raw is None:
            return handler.send_page(page("Backlog", "backlog", "<p>Backlog source unavailable.</p>", status="Unavailable"), 503)
        return handler.send_page(_backlog_page(_backlog_document(raw), parsed, state))
    return handler.send_page(_needs_you(_backlog_document(raw or b"{}"), state, source_missing=raw is None))


def _dispatch_static(handler, path):
    asset = Path(__file__).parent / "static" / path.rsplit("/", 1)[1]
    content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
    return handler.send_bytes(200, asset.read_bytes(), content_type)


def _dispatch_view(handler, state, path, parsed):
    if path == "/tickets":
        return handler.send_page(_tickets(state, parsed))
    return handler.send_page({"/": _floor, "/goals": _goals}[path](state))


def _dispatch_ticket_page(handler, state, path, parsed):
    detail = _ticket_detail(state, path)
    if detail[1] != 200:
        return handler.send_page(detail[0], detail[1])
    match = re.fullmatch(r"/t/([^/]+)/([^/]+)/([0-9]+)", path)
    repo = unquote(match.group(1)) + "/" + unquote(match.group(2))
    number = int(match.group(3))
    record = next(row for row in (state.data.get(repo) or []) if int(row.get("number", -1)) == number)
    store = _transcript_store(state)
    runs = store.attempts(repo + "#" + str(number)) if store else []
    params = parse_qs(parsed.query)
    selected = params.get("run", [""])[0]
    run = next((item for item in runs if str(item["id"]) == selected or str(item["attempt"]) == selected), runs[-1] if runs else None)
    full = store.get(run["id"]) if store and run else None
    body = ticket_view.render(record, runs, run.get("id") if run else None, (full or {}).get("events", []), [])
    return handler.send_page(page("Ticket %s#%d" % (repo, number), "tickets", body, status="Live"))


def _dispatch_basic(handler, state, path):
    if path == "/livez":
        return handler.send_text(200, "live")
    if path == "/readyz":
        return handler.send_text(200 if state.ready else 503, "ready" if state.ready else "not ready")
    if path == "/api/tickets":
        return handler.send_json(200, state.response())
    return handler.send_text(404, "not found")


def _dispatch_usage(handler, state):
    from datetime import datetime, timezone
    rows = [row for items in getattr(getattr(state, "usage", None), "providers", {}).values() for row in items]
    now = datetime.now(timezone.utc)
    rows.sort(key=lambda row: (row.get("used_pct") is None,
                               -(float(row["used_pct"]) if row.get("used_pct") is not None else 0)))
    body = ['<p class="empty-state">Usage limits have not been uploaded yet.</p>'] if not rows else []
    for row in rows:
        body.append(views.render_usage_row(row, now))
    return handler.send_page(page("Usage", "usage", "".join(body), status="Live"))


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
