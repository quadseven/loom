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
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
import ipaddress
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener

from bus import brain as bus_brain
from bus import tickets as bus_tickets
from bus import metrics as bus_metrics
from bus.naming import strip_kind

from .github import TicketStore
from .shell import page
from . import transcripts as transcript_store
from . import usage as usage_store
from . import brain_memory
from . import ticket as ticket_view
from . import goals as goals_view
from . import ticket_data
from . import tickets_table
from . import operators as operators_view
from . import needs_you as needs_you_view

STATES = ("queued", "briefed", "running", "pr", "merged", "shipped", "verified", "failed")
from . import views
from . import brain_view, hot_spots

def _all_tickets(state):
    payload = state.response()
    rows = [row for tickets in payload["tickets"].values() if tickets for row in tickets]
    models = payload.get("footer_models")
    if models is None:
        models = views.footer_models(payload.get("pull_requests", []))
    for row in rows:
        if not row.get("model"):
            model = models.get("%s#%s" % (row.get("repo"), row.get("number")))
            if model:
                row["model"] = model
    footer_runs = payload.get("footer_runs")
    if footer_runs is None:
        footer_runs = views.footer_runs(payload.get("pull_requests", []))
    # The newest stored run of each ticket, as the background refresh last
    # read it (TicketStore._read_latest_runs): a request never reads the
    # transcript database (#780), and an empty map falls back to the pull
    # request footer and claim comments.
    tickets_table.resolve_models(rows, footer_runs=footer_runs, latest=payload.get("latest_runs") or {},
                                 comments=payload.get("issue_comments") or [])
    return rows


def _ticket_href(row):
    return "/t/%s/%d" % (quote(str(row.get("repo") or ""), safe="/"), int(row.get("number") or 0))


def _next_refresh_text(next_refresh):
    """Format the numeric epoch timestamp as an HH:MM ET clock string for HTML
    display; this renders page text only and never writes to GITHUB_ENV."""
    if not isinstance(next_refresh, (int, float)) or isinstance(next_refresh, bool) or next_refresh <= 0:
        return "once GitHub can be reached"
    clock = datetime.fromtimestamp(next_refresh, timezone.utc).astimezone(
        ZoneInfo("America/New_York")).strftime("%H:%M ET")
    return "at " + clock


def _stale_notice(payload, now=None):
    refreshed = payload.get("ticket_refreshed_at")
    age = ("is %d minutes old" % max(0, int(((time.time() if now is None else now) - refreshed) // 60))
           if refreshed else "has not been read yet")
    reason = html.escape(payload.get("stale_reason") or "GitHub could not refresh ticket data", quote=False)
    # next_refresh_at is a numeric epoch timestamp formatted as HH:MM ET and
    # HTML-escaped for the page banner; it is never written to GITHUB_ENV.
    when = _next_refresh_text(payload.get("next_refresh_at"))
    return ('<p class="stale-note">Ticket data %s: %s; it refreshes %s.</p>'
            % (age, reason, html.escape(when)))


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
    lane_model = "%s / %s" % (row.get("lane") or "unassigned",
                              row.get("model_text") or row.get("model") or tickets_table.NOT_RUN)
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
    response = state.response()
    wait_rows = {row.get("state"): row for row in response.get("wait_rows", []) if isinstance(row, dict)}
    # The same function and rows as Hot spots, so the two pages name one bottleneck.
    named = list(wait_rows.values()) if wait_rows else [
        {"state": name, "median_h": statistics.median(sample), "n": len(sample)}
        for name, sample in (waits.items() if isinstance(waits, dict) else ())
        if isinstance(sample, list) and sample]
    bottleneck = bus_metrics.bottleneck(named)
    backlog_count, backlog_error = _backlog(state)
    cards = ['<a class="step-card" href="/backlog"><strong>%s</strong><span>backlog</span><small>%s</small></a>' % (
        str(backlog_count) if backlog_count is not None else "not looked",
        "open epic and ready issues" if backlog_error is None else html.escape(backlog_error))]
    for name in STATES:
        sample = waits.get(name, []) if isinstance(waits, dict) else []
        stats = wait_rows.get(name, {})
        median = stats.get("median_h") if wait_rows else (statistics.median(sample) if sample else None)
        worst = stats.get("worst_h")
        mark = " bottleneck" if name == bottleneck else ""
        note = ("<small>median %.1fh%s</small>" % (median, " | worst %.1fh" % worst if worst is not None else "")) if median is not None else "<small>%s</small>" % html.escape(getattr(state, "waits_error", "median unavailable"))
        if name == bottleneck:
            note = "<small>bottleneck - median %.1fh%s</small>" % (median, " | worst %.1fh" % worst if worst is not None else "")
        cards.append('<a class="step-card state-%s%s" href="/tickets?state=%s"><strong>%d</strong><span>%s</span>%s</a>' % (
            name, mark, name, len(by_state[name]), name, note))
    return "".join(cards), bottleneck


MOVING_ORDER = ("running", "pr", "briefed", "merged", "shipped")


def _when(stamp):
    """A comment time as the operator reads it: 'Oct 15 02:48 UTC', or in
    LOOM_DASHBOARD_TZ (an IANA name) when that zone is available."""
    from datetime import datetime, timezone
    try:
        moment = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    zone_name = os.environ.get("LOOM_DASHBOARD_TZ", "").strip()
    if zone_name:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            local = moment.astimezone(ZoneInfo(zone_name))
        except (ZoneInfoNotFoundError, ValueError):
            # An unknown or malformed zone name reads in UTC, which says so.
            return moment.strftime("%b %d %H:%M UTC")
        return local.strftime("%b %d %H:%M ") + (local.tzname() or zone_name)
    return moment.strftime("%b %d %H:%M UTC")


def _start_here_line(state):
    """Hunt's start-here: the top epic and its next ticket, from backlog.json."""
    raw = _backlog_bytes()
    if raw is None:
        return None, "the backlog source is not set or unreadable"
    epics = _backlog_document(raw).get("epics") or []
    if not epics or not isinstance(epics[0], dict):
        return None, "the backlog has no epics"
    first, nxt = epics[0], epics[0].get("next") or {}
    return (str(first.get("title") or ""), _issue_url({**first, **nxt}),
            str(nxt.get("title") or ("#%s" % nxt.get("number") if nxt.get("number") else ""))), None


PRIORITIES_SHOWN = 5


def _priorities_card(rows):
    """The open tickets not yet merged, in the order lanes take them:
    urgent and regression labels, then Priority (P0 first), then the oldest."""
    waiting = [row for row in rows if row.get("open", True) and row.get("state") not in
               ("merged", "shipped", "verified", "closed-unmerged")]
    waiting.sort(key=lambda row: bus_tickets.work_order(str(row.get("repo") or ""), row))
    esc = html.escape
    items = "".join(
        '<li><span class="machine-fact">%s</span><div><a href="%s">%s</a><span class="muted">%s</span></div></li>' % (
            esc(bus_tickets.priority_of(row)), esc(_ticket_href(row), quote=True),
            esc(goals_view.short(str(row.get("title") or "Untitled ticket"), 90)),
            esc("%s#%s - %s" % (row.get("repo", ""), row.get("number", ""), row.get("state") or "unlabeled")))
        for row in waiting[:PRIORITIES_SHOWN])
    body = '<ol class="priority-list">%s</ol>' % items if items else '<p class="muted">Nothing is waiting.</p>'
    return '<section class="card priorities-card"><span class="label">Priorities</span>%s<a href="/tickets">all tickets &rarr;</a></section>' % body


def _floor(state):
    rows = _all_tickets(state)
    runaway = [row for row in rows if "loom:runaway" in (row.get("labels") or []) and row.get("open", True)]
    conveyor, bottleneck = _conveyor(state, rows)
    order = {name: index for index, name in enumerate(MOVING_ORDER)}
    moving = sorted((row for row in rows if row.get("open", True) and row.get("state") in order),
                    key=lambda row: (order[row["state"]], str(row.get("updated_at") or "")[::-1]))
    payload = state.response()
    context = getattr(state, "context", None) or payload.get("floor_context", {})
    esc = html.escape
    headline = context.get("report_headline")
    if headline:
        when = _when(context.get("report_time"))
        standup = '<p class="standup-line">%s</p><p class="standup-meta machine-fact">standup%s</p>' % (
            esc(goals_view.short(str(headline), 220)), esc(" - %s report" % when) if when else "")
    else:
        standup = '<p class="standup-line">No standup yet.</p><p class="standup-meta machine-fact">not looked: %s</p>' % esc(
            str(context.get("report_reason") or "no goal report found"))
    head = '<div class="section-head"><span class="label">Conveyor - count and median wait</span>%s</div>' % (
        '<span class="bottleneck-note">%s is the bottleneck</span>' % esc(bottleneck) if bottleneck else "")
    parts = ['<section class="standup">%s</section>' % standup,
             '<section class="section">%s<div class="conveyor">%s</div></section>' % (head, conveyor)]
    runaway_rows = "".join(_ticket_row(row) for row in runaway)
    parts.append('<section class="section runaway-tickets"><div class="section-head"><span class="label">Runaway tickets</span></div>%s</section>' % (
        runaway_rows or '<p class="empty-state">None held for review.</p>'))
    parts.append('<div class="floor-columns"><section class="section"><div class="section-head"><span class="label">Moving now</span>'
                 '<a href="/tickets">all tickets &rarr;</a></div><div class="ticket-list">%s</div></section>' % (
                     "".join(_ticket_row(row) for row in moving[:24]) or '<p class="empty-state">Nothing is moving right now.</p>'))
    step = context.get("journal_step") or context.get("journal_lead")
    label = "Brain's next move" if context.get("journal_step") else "Brain, last tick"
    if step:
        title = goals_view.short(str(step), 140)
        rest = " ".join(str(step).split())[len(title.rstrip(" .")):].strip(" .")
        who = ", ".join(x for x in (context.get("journal_holder"), _when(context.get("journal_time"))) if x)
        brain = '<p class="next-move">%s</p>%s<p class="machine-fact">%s</p><a href="/brain">see the reasoning &rarr;</a>' % (
            esc(title), '<p class="muted">%s</p>' % esc(goals_view.short(rest, 200)) if rest else "", esc(who))
    elif context.get("journal_time"):
        brain = '<p class="muted">The newest journal (%s) names no move.</p><a href="/brain">the Brain view &rarr;</a>' % esc(
            _when(context.get("journal_time")) or str(context.get("journal_time")))
    else:
        brain = '<p class="muted">not looked: %s</p>' % esc(str(context.get("journal_reason") or "no goal journal found"))
    parts.append('<aside class="floor-aside"><section class="card next-move-card"><span class="label">%s</span>%s</section>' % (esc(label), brain))
    parts.append(_priorities_card(rows))
    repeated = payload.get("repeated_failures") or []
    repeated_html = ("<ul>" + "".join(
        '<li><a href="%s">%s</a></li>' % (
            esc(str(item.get("html_url") or "/tickets"), quote=True),
            esc("%s#%s: %s" % (item.get("repo", ""), item.get("number", ""), item.get("title", "Repeated failure"))))
        for item in repeated if isinstance(item, dict)) + "</ul>" if repeated else
        '<p class="empty-state">No open repeated failure invariants.</p>')
    parts.append('<section class="card"><span class="label">Repeated failures</span>%s</section>' % repeated_html)
    start, why = _start_here_line(state)
    if start:
        epic, url, ticket = start
        hunt = '<p class="next-move">%s</p><p><a href="%s">%s</a></p><a href="/backlog">the backlog &rarr;</a>' % (
            esc(epic), esc(url, quote=True), esc(ticket or "first ticket"))
    else:
        hunt = '<p class="muted">not looked: %s</p>' % esc(why)
    parts.append('<section class="card"><span class="label">Hunt - start here</span>%s</section>' % hunt)
    since = context.get("since_report") or {}
    missing = "not in the latest report" if headline else "no report found"
    parts.append('<section class="card"><span class="label">Since last report</span><dl class="since">%s</dl></section>' % "".join(
        '<dt class="since-%s">%s</dt><dd>%s</dd>' % (key.replace(" ", "-"), esc(label), esc(str(since.get(key) or missing)))
        for key, label in (("well", "well"), ("not well", "not well"), ("stuck", "stuck"), ("next", "next"))))
    needs = context.get("needs_you")
    asks = needs_you_view.entries(payload.get("operator_items") or [],
                                  (payload.get("operator_claims") or {}).get("needs") or [],
                                  getattr(state, "repos", None) or [])
    if isinstance(needs, int):
        top = asks[0] if asks else None
        needs_html = '<p><strong class="count">%d</strong> %s</p>%s<a href="/needs-you">all asks &rarr;</a>' % (
            needs, "ask" if needs == 1 else "asks",
            '<p>%s</p>' % esc(str(top.get("title") or "")) if top else "")
    else:
        needs_html = '<p class="muted">not looked: %s</p>' % esc(str(context.get("needs_you_reason") or "needs-operator source unavailable"))
    parts.append(operators_view.floor_html(payload.get("operator_claims")))
    parts.append('<section class="card"><span class="label">Needs you</span>%s</section></aside></div>' % needs_html)
    return page("Floor", "floor", "".join(parts), status="snapshot")


def _short_ref(goal):
    """quadseven/loom#30 -> loom#30: the owner is the same everywhere."""
    return goal.split("/", 1)[-1]


def _goal_report_html(info, missing, now):
    """The card's report line: the newest report and how old it is, "No
    report on this goal in the last two weeks" only when the goal's reports
    were read and none is that recent, or the source that was not read."""
    esc = html.escape
    if not info:
        return '<p class="muted">report not read: %s</p>' % esc(missing)
    report, stamp = info.get("report"), info.get("report_time")
    when = _when(stamp)
    ago = goals_view.age(stamp, now)
    if info.get("report_quiet"):
        last = " Last report %s." % when if report and when else ""
        return "<p>No report on this goal in the last two weeks.%s</p>" % esc(last)
    if report:
        return '<p>%s</p><p class="machine-fact">last report %s%s</p>' % (
            esc(report), esc(when or str(stamp)), esc(" (%s ago)" % ago) if ago else "")
    return '<p class="muted">report not read: %s</p>' % esc(
        str(info.get("report_reason") or "the goal home's comments were not read"))


def _goal_child_row(goal, info, own, now):
    """One smaller goal inside its parent's card: title, in-flight count,
    last report age; it links to that goal's tickets."""
    esc = html.escape
    title = strip_kind(info.get("title")) or _short_ref(goal)
    flying = info.get("in_flight") if info else None
    if flying is None:
        flying = sum(1 for row in own if row.get("open", True) and row.get("state") in goals_view.IN_FLIGHT_STATES)
    ago = goals_view.age(info.get("report_time"), now) if info else None
    if ago:
        report = "report %s ago" % ago
    elif info and info.get("report_quiet"):
        report = "no report in two weeks"
    else:
        report = "report not read"
    return ('<li><a href="/tickets?goal=%s"><span class="goal-child-title">%s</span>'
            '<span class="machine-fact">%s / %d in flight / %s</span></a></li>') % (
                quote(goal, safe=""), esc(goals_view.short(str(title), 90)),
                esc(_short_ref(goal)), flying, esc(report))


def _goal_epic_row(epic):
    esc = html.escape
    return ('<li><a href="/tickets?epic=%s"><span class="goal-child-title">%s</span>'
            '<span class="machine-fact">%s / tickets %d/%d / %d in flight</span></a></li>') % (
                quote(epic["ref"], safe=""), esc(goals_view.short(strip_kind(epic.get("title")) or _short_ref(epic["ref"]), 90)),
                esc(_short_ref(epic["ref"])), epic.get("tickets", 0), goals_view.EPIC_TICKET_CAP,
                epic.get("in_flight", 0))


def _goal_epics_html(info):
    """The goal's epics: the open ones against the cap of 6, each with its
    tickets against 15, then the closed ones as a collapsed record."""
    found = [e for e in (info.get("epics") or []) if isinstance(e, dict) and e.get("ref")]
    if not found:
        return ""
    live = [e for e in found if e.get("open", True)]
    done = [e for e in found if not e.get("open", True)]
    out = ['<div class="goal-epics"><p class="machine-fact">open epics %d/%d</p>' % (len(live), goals_view.EPIC_CAP)]
    if live:
        out.append('<ul class="goal-children" aria-label="Open epics of this goal">%s</ul>'
                   % "".join(_goal_epic_row(e) for e in live))
    if done:
        out.append('<details class="goal-epics-closed"><summary>%d closed epic%s</summary>'
                   '<ul class="goal-children">%s</ul></details>' % (
                       len(done), "" if len(done) == 1 else "s", "".join(_goal_epic_row(e) for e in done)))
    out.append("</div>")
    return "".join(out)


def _goal_unproven_html(info):
    held, limit = info.get("unproven"), info.get("unproven_limit")
    if not (isinstance(held, int) and isinstance(limit, int)):
        return ""
    note = "unproven work %d/%d (merged or shipped with no standing proof)" % (held, limit)
    if held > limit:
        note += " - over limit: no new build tickets until the goal finishes its proof"
    return '<p class="machine-fact">%s</p>' % html.escape(note)


def _goal_metric_html(info, missing):
    esc = html.escape
    if info.get("metric") is not None:
        prev = esc(" / was %s" % info["previous"]) if info.get("previous") not in (None, "") else ""
        return '<div class="metric">%s</div><div class="machine-fact">%s%s</div>' % (
            esc(str(info["metric"])), esc(str(info.get("unit") or "")), prev)
    return ('<div class="metric metric-missing">not read</div>'
            '<div class="machine-fact">verified with proof: %s</div>') % esc(
                str(info.get("metric_reason") or missing))


def _goal_spark_html(info):
    history = [v for v in info.get("history", [])][-7:]
    scale = info.get("scale") or max([v for v in history if isinstance(v, (int, float))] or [1]) or 1
    return "".join('<i style="height:%d%%"%s></i>' % (
        max(6, min(100, int(100 * v / scale))) if isinstance(v, (int, float)) else 6,
        ' class="last"' if index == len(history) - 1 else "")
        for index, v in enumerate(history))


def _goal_card(goal, children, grouped, goal_data, missing, now):
    esc = html.escape
    tickets = grouped.get(goal, [])
    info = goal_data.get(goal) or {}
    counts = "".join('<a class="chip" href="/tickets?state=%s&amp;goal=%s">%s %d</a>' % (
        esc(name, quote=True), quote(goal, safe=""), esc(name),
        sum(row.get("state") == name for row in tickets)) for name in STATES
        if any(row.get("state") == name for row in tickets))
    title = strip_kind(info.get("title")) or _short_ref(goal)
    measure = info.get("measure")
    child_html = ('<ul class="goal-children" aria-label="Smaller goals in this goal">%s</ul>' % "".join(
        _goal_child_row(child, goal_data.get(child) or {}, grouped.get(child, []), now)
        for child in sorted(children, key=lambda c: -len(grouped.get(c, []))))) if children else ""
    return (
        '<article class="goal-card card"><div class="machine-fact">%s</div><h2>%s</h2>'
        '<div class="goal-metric"><div>%s</div>'
        '<div class="sparkline" aria-label="Last 7 days, one bar a day">%s</div></div>%s'
        '<div class="chips">%s</div>%s%s%s<a href="/tickets?goal=%s">%d tickets on this goal &rarr;</a></article>' % (
            esc(_short_ref(goal)), esc(str(title)), _goal_metric_html(info, missing),
            _goal_spark_html(info), '<p class="machine-fact">measure: %s</p>' % esc(measure) if measure else "",
            counts, _goal_unproven_html(info), _goal_report_html(info, missing, now),
            _goal_epics_html(info) + child_html,
            quote(goal, safe=""), len(tickets)))


def _goals(state):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rows = _all_tickets(state)
    grouped = {}
    for row in rows:
        if row.get("goal"):
            grouped.setdefault(row["goal"], []).append(row)
    context = getattr(state, "context", None) or {}
    response = state.response()
    goal_data = context.get("goals") or response.get("goal_context") or {}
    if not isinstance(goal_data, dict):
        goal_data = {}
    # Why a goal has no record: the snapshot says, else it is not in it.
    missing = str(response.get("goal_context_error") or
                  "the goal is not in the last snapshot's goal data")
    parents = {goal: (info or {}).get("parent") for goal, info in goal_data.items()}
    tree = goals_view.rollup(list(grouped) + [g for g in goal_data if g not in grouped], parents)

    def size(top):
        return len(grouped.get(top, [])) + sum(len(grouped.get(child, [])) for child in tree[top])

    cards = [_goal_card(goal, tree[goal], grouped, goal_data, missing, now)
             for goal in sorted(tree, key=lambda top: -size(top))]
    body = ('<p class="page-lede">Each top-level goal home, with the smaller goals that roll up into it,'
            ' measured by what reached a live proof.</p><div class="goal-grid">%s</div>') % (
        "".join(cards) or ('<p class="empty-state">%s.</p>' % html.escape(missing)
                           if response.get("goal_context_error") else
                           '<p class="empty-state">No goals with ticket data.</p>'))
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


def _child_counts(epic):
    """(done, total) children of an epic. The Hunt agent view carries the
    counts as `closed` and `total`; an older document lists `children`."""
    total = epic.get("total")
    if isinstance(total, int) and not isinstance(total, bool):
        done = epic.get("closed")
        done = done if isinstance(done, int) and not isinstance(done, bool) else 0
        return max(0, min(done, total)), max(0, total)
    children = [child for child in epic.get("children") or [] if isinstance(child, dict)]
    done = sum(bool(child.get("closed")) or str(child.get("state", child.get("status", ""))).lower() == "closed"
               for child in children)
    return done, len(children)


def _health_inspectors(document):
    epics = [epic for epic in document.get("epics") or [] if isinstance(epic, dict)]
    finished, unlinked, titles = [], [], []
    for epic in epics:
        done, total = _child_counts(epic)
        if total and done == total and str(epic.get("state", "open")).lower() != "closed":
            finished.append(epic)
        for child in epic.get("children") or []:
            if not child.get("parent") and not child.get("parent_number") and not child.get("parent_url"):
                unlinked.append(child)
        title = str(epic.get("title", ""))
        if title and (title != title.strip() or title.startswith(("[", "#")) or len(title) > 120):
            titles.append(epic)
    repeated = [item for item in document.get("repeated_failures") or []
                if isinstance(item, dict) and item.get("open", True)]
    return (("Finished children, open epic", finished), ("Children without parent link", unlinked),
            ("Nonstandard titles", titles), ("Repeated failure invariants", repeated))


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
    return out, selected, repo_filter


# The ranked table shows this many epics before folding the rest into a
# collapsed group, so the ranking fits two screens on a wide window.
RANKED_SHOWN = 40
EPIC_COLUMNS = ("Repo", "Epic", "Tier", "Score", "Children", "Next child", "Age")


def _short_repo(value):
    """example-owner/app and app name the same repo: the backlog source keys
    epics by the bare repo name, the ticket snapshot by OWNER/NAME."""
    if isinstance(value, dict):
        value = value.get("full_name") or value.get("name") or ""
    return str(value or "").rsplit("/", 1)[-1]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _score(epic):
    try:
        return float(epic.get("score"))
    except (TypeError, ValueError):
        return None


def _epic_age(epic):
    """Time since the epic was last touched, or None when the source has no
    date for it (the Hunt agent view carries none today)."""
    from datetime import datetime, timezone
    metrics = epic.get("metrics") if isinstance(epic.get("metrics"), dict) else {}
    stamp = epic.get("updated_at") or epic.get("created_at")
    if stamp:
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - moment).total_seconds() / 86400
    elif isinstance(metrics.get("stale_days"), (int, float)):
        days = float(metrics["stale_days"])
    else:
        return None
    days = max(0.0, days)
    return "%dh" % int(days * 24) if days < 1 else "%dd" % int(days)


def _link_or_text(url, text, css=""):
    cls = ' class="%s"' % css if css else ""
    # The title attribute carries the full text where a narrow cell cuts it.
    if url:
        return '<a%s href="%s" title="%s">%s</a>' % (cls, html.escape(url, quote=True), html.escape(text, quote=True), html.escape(text))
    return '<span%s title="%s">%s</span>' % (cls, html.escape(text, quote=True), html.escape(text))


def _epic_row(epic):
    esc = html.escape
    done, total = _child_counts(epic)
    left = total - done
    percent = int(round(100 * done / total)) if total else 0
    nxt = epic.get("next") if isinstance(epic.get("next"), dict) else {}
    number = _int_or_none(nxt.get("number"))
    if nxt:
        label = ("#%d " % number if number is not None else "") + str(nxt.get("title") or "")
        next_html = _link_or_text(nxt.get("html_url") or nxt.get("url"), label.strip() or "next ticket", "epic-next")
    else:
        next_html = '<span class="muted">no open child</span>'
    score = _score(epic)
    tier = epic.get("tier")
    age = _epic_age(epic)
    epic_number = _int_or_none(epic.get("number"))
    title = strip_kind(epic.get("title")) or "Untitled epic"
    return ('<tr><td class="machine-fact">%s%s</td><td class="epic-title">%s</td>'
            '<td class="machine-fact%s">%s</td><td class="machine-fact num">%s</td>'
            '<td><span class="epic-bar" role="img" aria-label="%d of %d children done, %d open">'
            '<i style="width:%d%%"></i></span><span class="machine-fact">%d open / %d done</span></td>'
            '<td class="epic-next-cell">%s</td><td class="machine-fact%s">%s</td></tr>') % (
        esc(_short_repo(epic.get("repo"))), "#%d" % epic_number if epic_number is not None else "",
        _link_or_text(epic.get("html_url") or epic.get("url"), title),
        "" if tier else " muted", esc(str(tier or "unjudged")),
        esc("%.1f" % score if score is not None else "-"),
        done, total, left, percent, left, done, next_html,
        "" if age else " muted", esc(age or "-"))


def _epic_table(epics, caption):
    head = "".join('<th scope="col">%s</th>' % name for name in EPIC_COLUMNS)
    return ('<table class="epic-table"><caption>%s</caption><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (
        html.escape(caption), head, "".join(_epic_row(epic) for epic in epics)))


def _epic_members(epics):
    """(bare repo, number) for every epic in the backlog and every child it
    lists: the agent view lists open children and the next ticket, an older
    document lists every child."""
    members = set()
    for epic in epics:
        repo = _short_repo(epic.get("repo"))
        members.add((repo, _int_or_none(epic.get("number"))))
        nxt = epic.get("next") if isinstance(epic.get("next"), dict) else None
        for child in list(epic.get("children") or []) + list(epic.get("open_children") or []) + ([nxt] if nxt else []):
            if isinstance(child, dict):
                members.add((_short_repo(child.get("repo") or repo), _int_or_none(child.get("number"))))
    return members


FINISHED_STATES = ("merged", "shipped", "verified", "closed-unmerged")


def _outside_any_epic(rows, epics, repo_filter=""):
    """Open tickets under no epic (decision 0003: their Part of line or
    sub-issue parent names no epic) that no backlog epic lists as a child:
    each one is work nobody has placed."""
    members = _epic_members(epics)
    found = [row for row in rows
             if row.get("open", True) and row.get("state") not in FINISHED_STATES and not row.get("epic")
             and (_short_repo(row.get("repo")), _int_or_none(row.get("number"))) not in members
             and (not repo_filter or _short_repo(row.get("repo")) == _short_repo(repo_filter))]
    return sorted(found, key=lambda row: (str(row.get("repo") or ""), _int_or_none(row.get("number")) or 0))


def _outside_section(rows):
    esc = html.escape
    lede = ('<p class="backlog-note">Each open ticket below belongs to no epic. Each is an incoherence '
            'to fix: move it under an open epic of its goal, or close it. Read from the ticket snapshot, which holds '
            'open loom:ticket issues only; open issues without that label are not checked here.</p>')
    if rows:
        items = "".join('<li><a href="%s"><span class="machine-fact">%s#%s</span> %s</a><span class="machine-fact state-%s">%s</span></li>' % (
            esc(_ticket_href(row), quote=True), esc(_short_repo(row.get("repo"))), esc(str(row.get("number", ""))),
            esc(str(row.get("title") or "Untitled ticket")), esc(str(row.get("state") or "queued"), quote=True),
            esc(str(row.get("state") or "unlabeled"))) for row in rows)
        body = '<ul class="backlog-outside">%s</ul>' % items
    else:
        body = '<p class="empty-state">Every open ticket is in an epic.</p>'
    return ('<section class="section backlog-outside-section"><div class="section-head"><span class="label">Outside any epic</span>'
            '<span class="machine-fact%s">%d</span></div>%s%s</section>') % (
        " bad" if rows else "", len(rows), lede, body)


def _render_inspectors(document):
    groups = _health_inspectors(document)
    out = ['<details class="backlog-group"><summary>Bookkeeping checks (%d)</summary>' % sum(len(items) for _, items in groups)]
    for title, items in groups:
        out.append('<h2>%s (%d)</h2><ul>' % (html.escape(title), len(items)))
        for item in items:
            out.append('<li><a href="%s">%s</a></li>' % (html.escape(_issue_url(item), quote=True), html.escape(_repo_number(item) or str(item.get("title", "")))))
        out.append('</ul>')
    out.append('</details>')
    return "".join(out)


def _backlog_page(document, parsed, state):
    all_epics = [epic for epic in document.get("epics") or [] if isinstance(epic, dict)]
    form, epics, repo_filter = _backlog_filters(all_epics, parsed)
    empty = [epic for epic in epics if _child_counts(epic)[1] == 0]
    ranked = [epic for epic in epics if _child_counts(epic)[1] > 0]
    # Sorted by score, highest first; the source's own order breaks ties and
    # an epic with no score sorts last.
    ranked.sort(key=lambda epic: (_score(epic) is None, -(_score(epic) or 0.0)))
    out = [_outside_section(_outside_any_epic(_all_tickets(state), all_epics, repo_filter)),
           _ranked_section(ranked, form), _empty_epics(empty), _render_inspectors(document)]
    return page("Backlog", "backlog", "".join(out), status="Read only")


def _ranked_section(ranked, form):
    """The Epics by score section: the filter form, the top RANKED_SHOWN
    epics as a table and the rest collapsed below it."""
    dated = any(_epic_age(epic) for epic in ranked)
    out = ['<section class="section"><div class="section-head"><span class="label">Epics by score</span>'
           '<span class="machine-fact">%d ranked</span></div>' % len(ranked)]
    out.append('<p class="backlog-note">One row per epic with children, ranked by Hunt\'s score.%s</p>' % (
        "" if dated else " Age shows a dash: the backlog source carries no epic dates."))
    out.extend(form)
    if ranked:
        out.append(_epic_table(ranked[:RANKED_SHOWN], "Epics ranked by score"))
        rest = ranked[RANKED_SHOWN:]
        if rest:
            out.append('<details class="backlog-group"><summary>Ranks %d to %d (%d more)</summary>%s</details>' % (
                RANKED_SHOWN + 1, len(ranked), len(rest), _epic_table(rest, "Epics ranked by score, continued")))
    else:
        out.append('<p class="empty-state">No epic with children matches this filter.</p>')
    out.append('</section>')
    return "".join(out)


def _empty_epics(empty):
    """The collapsed list of epics tracking no children; nothing when none."""
    if not empty:
        return ""
    return '<details class="backlog-group"><summary>Empty epics (%d), tracking no children</summary><ul>%s</ul></details>' % (
        len(empty), "".join('<li>%s <span class="machine-fact">%s</span></li>' % (
            _link_or_text(epic.get("html_url") or epic.get("url"), strip_kind(epic.get("title")) or "Untitled epic"),
            html.escape(_repo_number(epic))) for epic in empty))


def _needs_you(document, state, *, source_missing=False, who=""):
    """Everything waiting on a person (loom#810): needs-operator asks from
    Hunt's document and the store, and needs:<login> items from the
    Operators pass, each once, filtered by ?who=."""
    asks = list(document.get("needs_operator") or []) + list(getattr(state, "operator_items", []))
    response = getattr(state, "response", None)
    claims = (response() if callable(response) else {}).get("operator_claims") or {}
    repos = list(getattr(state, "repos", None) or [])
    found, cards = needs_you_view.render(asks, claims, who, repos)
    out = ['<p class="page-lede">Only what only a person can do: each item names who it waits on. '
           'Everything else the brain does itself.</p>']
    if source_missing:
        out.append('<p class="card">Backlog source unavailable: Hunt\'s items are not shown.</p>')
    out.append(cards)
    if not who:
        out.append(_backlog_asks(document, found, repos))
    counts = getattr(state, "human_counts", None) or document.get("loom_human_counts") or {}
    out.append('<footer>loom:human counts by repo: %s</footer>' % html.escape(
        ", ".join("%s: %s" % (repo, count) for repo, count in counts.items()) or "none"))
    return page("Needs you", "needs-you", "".join(out), status="Read only")


def _backlog_asks(document, found, repos):
    """Hunt's ready-for-a-person items not already listed above, collapsed."""
    names = needs_you_view.full_names(list(repos) + [entry["repo"] for entry in found])
    listed = {entry["key"] for entry in found}
    backlog = {}
    for item in document.get("ready_for_human") or []:
        key = needs_you_view.ref_key(item.get("repo"), item.get("number"), names) if _repo_number(item) else ""
        if key and key not in listed:
            backlog.setdefault(key, {**item, "queue": "ready_for_human"})
    if not backlog:
        return ""
    order = lambda item: (-_number(item.get("unblocks")), str(item.get("waiting_since", "9999")))
    rows = []
    for item in sorted(backlog.values(), key=order):
        waiting = "idle" if item.get("waiting_since_source") == "updated" else "waiting"
        rows.append('<p class="backlog-ask"><a href="%s">%s</a> %s <span class="machine-fact">%s | unblocks %s</span></p>' % (
            html.escape(_issue_url(item), quote=True), html.escape(_repo_number(item)),
            html.escape(str(item.get("title") or "")), waiting, html.escape(str(item.get("unblocks", 0)))))
    return ('<details class="backlog-asks"><summary>From the backlog (%d), ready for a person</summary>%s</details>'
            % (len(backlog), "".join(rows)))


def _number(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ticket_page(state, parsed):
    rows = _all_tickets(state)
    payload = state.response()
    goal_data = payload.get("goal_context") or {}
    titles = {goal: (info or {}).get("title") for goal, info in goal_data.items()} if isinstance(goal_data, dict) else {}
    body = tickets_table.render(rows, parse_qs(parsed.query), goal_titles=titles,
                                entered=payload.get("state_entered") or {}, now=time.time(),
                                ticket_href=_ticket_href, short_ref=_short_ref)
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


def _authorized(handler, names=("LOOM_UPLOAD_TOKEN",)):
    """Whether the request's bearer token is the value of one of the named
    settings; a setting left empty matches nothing."""
    supplied = handler.headers.get("Authorization", "")
    if not supplied.startswith("Bearer "):
        return False
    tokens = [os.environ.get(name, "") for name in names]
    return any(token and hmac.compare_digest(supplied[7:], token) for token in tokens)


def _json_body(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length < 1 or length > 2_000_000:
        raise ValueError("invalid request size")
    return json.loads(handler.rfile.read(length))


def _handle_transcript_post(handler, state):
    if not _authorized(handler):
        return handler.send_json(401, {"error": "unauthorized"})
    try:
        body = _json_body(handler)
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


def _handle_usage_get(handler, state):
    """The station's live usage readings, for a brain off the lane machine
    (`python3 -m bus.usage --live`): the read-only brain token or the
    upload token, as the handoff packet."""
    if not _authorized(handler, BRAIN_READERS):
        return handler.send_json(401, {"error": "unauthorized"})
    store = getattr(state, "usage", None)
    if store is None:
        return handler.send_json(200, {"readings": [], "received_at": None})
    return handler.send_json(200, store.snapshot())


def _handle_usage_post(handler, state):
    if not _authorized(handler):
        return handler.send_json(401, {"error": "unauthorized"})
    try:
        body = _json_body(handler)
        store = getattr(state, "usage", None)
        if store is None:
            store = usage_store.Store()
            state.usage = store
        return handler.send_json(201, store.upload(body))
    except (ValueError, json.JSONDecodeError) as exc:
        return handler.send_json(400, {"error": str(exc)})


# Brain memory (dashboard/brain_memory.py): the station writes ticks,
# closings and rules with the upload token. Every brain reads the handoff
# packet with it or with LOOM_HANDOFF_TOKEN, which can only read.
BRAIN_WRITES = {"/api/brain/ticks": "record_tick", "/api/brain/rules": "load_rules",
                "/api/brain/commitments": "close_commitments"}
BRAIN_READERS = ("LOOM_UPLOAD_TOKEN", "LOOM_HANDOFF_TOKEN")


def _brain_store(state):
    store = getattr(state, "brain_memory", None)
    if store is None:
        store = brain_memory.Store()
        state.brain_memory = store
    return store


def _handle_brain(handler, state, write=None):
    """A brain memory write (BRAIN_WRITES names the store method) or, with
    write None, the handoff packet read."""
    if not _authorized(handler, BRAIN_READERS if write is None else ("LOOM_UPLOAD_TOKEN",)):
        return handler.send_json(401, {"error": "unauthorized"})
    try:
        if write is None:
            return handler.send_json(200, _brain_store(state).handoff())
        body = _json_body(handler)
        return handler.send_json(201, getattr(_brain_store(state), write)(body))
    except ValueError as exc:
        return handler.send_json(400, {"error": str(exc)})
    except RuntimeError as exc:
        return handler.send_json(503, {"error": str(exc)})
    except transcript_store.storage_errors():
        return handler.send_json(503, {"error": "brain memory storage unavailable"})


def _handler_for(state):
    """Keep GET dispatch small; route groups are handled by module helpers."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            return _dispatch_get(self, state)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/usage":
                return _handle_usage_post(self, state)
            if path in BRAIN_WRITES:
                return _handle_brain(self, state, BRAIN_WRITES[path])
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
                payload = state.response()
                notice = _stale_notice(payload)
                document = document.replace('<main class="page-content">', '<main class="page-content">' + notice)
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
    if path in STATIC_ASSETS:
        return _dispatch_static(handler, path)
    if path in ("/improving", "/lanes"):
        return _send_dashboard_view(handler, state, path)
    if path == "/results":
        return _dispatch_results(handler, state)
    if path == "/free":
        return _dispatch_free(handler, state, parsed)
    if path == "/usage":
        return _dispatch_usage(handler, state)
    if path == "/tiers":
        return _dispatch_tiers(handler, state)
    if path in ("/brain", "/hot-spots"):
        return _dispatch_brain_hot_spots(handler, state, path)
    if path in ("/", "/goals", "/tickets"):
        return _dispatch_view(handler, state, path, parsed)
    if re.fullmatch(r"/t/[^/]+/[^/]+/[0-9]+", path):
        return _dispatch_ticket_page(handler, state, path, parsed)
    if path == "/operators":
        return handler.send_page(operators_view.page_html(state.response(), time.time()))
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
    detail = _ticket_detail_data(state, repo, int(number))
    return handler.send_json(200, {
        "ticket": record, "checks": ticket_view.checks(record.get("body")),
        "pull_request": (detail or {}).get("pull_request"),
        "timeline": ticket_view.path_steps(record, (detail or {}).get("comments") or [],
                                           (detail or {}).get("events") or []) if detail else [],
        "missing": (detail or {}).get("errors") if detail else {"github": "no GitHub client"}})


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
    who = needs_you_view.who_param(parse_qs(parsed.query).get("who", [""])[0])
    return handler.send_page(_needs_you(_backlog_document(raw or b"{}"), state, source_missing=raw is None, who=who))


# Served path -> (file under static/, content type). Browsers ask for
# /favicon.ico whatever the page head says, so it serves the 32 px PNG.
STATIC_ASSETS = {
    "/static/loom.css": ("loom.css", "text/css; charset=utf-8"),
    "/static/loom.js": ("loom.js", "text/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/static/favicon-32.png": ("favicon-32.png", "image/png"),
    "/static/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/favicon.ico": ("favicon-32.png", "image/png"),
}


def _dispatch_static(handler, path):
    name, content_type = STATIC_ASSETS[path]
    asset = Path(__file__).parent / "static" / name
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
    body = ticket_view.render(record, runs, run, (full or {}).get("events", []),
                              detail=_ticket_detail_data(state, repo, number))
    return handler.send_page(page("Ticket %s#%d" % (repo, number), "tickets", body, status="Live"))


def _ticket_detail_data(state, repo, number):
    """What GitHub says about one ticket (dashboard/ticket_data.py), or None
    when the dashboard has no GitHub client to ask."""
    details = getattr(state, "ticket_details", None)
    if details is None:
        gh = getattr(state, "gh", None)
        if gh is None:
            return None
        details = ticket_data.TicketDetails(gh)
        state.ticket_details = details
    prs = state.response().get("pull_requests", []) if hasattr(state, "response") else []
    return details.get(repo, number, prs, views.footer)


def _dispatch_basic(handler, state, path):
    if path == "/livez":
        return handler.send_text(200, "live")
    if path == "/readyz":
        return handler.send_text(200 if state.ready else 503, "ready" if state.ready else "not ready")
    if path == "/api/tickets":
        return handler.send_json(200, _api_tickets(state.response()))
    if path == "/api/brain/handoff":
        return _handle_brain(handler, state)
    if path == "/api/usage":
        return _handle_usage_get(handler, state)
    return handler.send_text(404, "not found")


# The raw sources the views read in-process, served as counts: whole pull
# requests and two weeks of comments made /api/tickets 164 MB (2026-10-15).
RAW_SOURCES = ("pull_requests", "issue_comments", "review_comments", "footer_models", "footer_runs",
               "state_entered")


def _api_tickets(payload):
    out = {key: value for key, value in payload.items() if key not in RAW_SOURCES}
    out["source_counts"] = {key: len(payload.get(key) or ()) for key in RAW_SOURCES}
    return out


def _dispatch_usage(handler, state):
    from datetime import datetime, timezone
    rows = [row for items in getattr(getattr(state, "usage", None), "providers", {}).values() for row in items]
    now = datetime.now(timezone.utc)
    payload = state.response() if hasattr(state, "response") else {}
    lanes = {lane for row in rows for lane in (row.get("lanes") or []) if isinstance(lane, str)}
    models = {lane: model for lane in lanes for model in [bus_brain.lane_model(lane)] if model}
    data = usage_store.build(rows, now, prs=payload.get("pull_requests") or [], models=models)
    return handler.send_page(page("Usage", "usage", usage_store.render(data),
                                  status="stale" if payload.get("stale") else "live"))


def _tiers_usage(state, now):
    """The station's usage readings as a Tiers page SourceState: stale when
    none has arrived since the dashboard started, or the newest is older than
    the Usage page's limit."""
    from .tiers_sources import SourceState
    store = getattr(state, "usage", None)
    received = getattr(store, "received_at", None)
    if received is None:
        return SourceState.not_read(
            "Usage readings", "no usage snapshot since the dashboard started (the station uploads one each pass)")
    age = (now - received).total_seconds()
    stale = age > usage_store.STALE_AFTER
    return SourceState(name="Usage readings", data=[row for items in store.providers.values() for row in items],
                       last_good_at=received.timestamp(), read_at=received.timestamp(), stale=stale,
                       error="no station upload in %dh" % (age // 3600) if stale else "")


def _tiers_station(state, payload):
    """The running Loom tickets the dashboard already holds, as a Tiers page
    SourceState: stale with the ticket snapshot."""
    from .tiers_sources import SourceState
    if not any(rows is not None for rows in (payload.get("tickets") or {}).values()):
        return SourceState.not_read("Station tickets", "ticket data not read yet")
    entered = payload.get("state_entered") or {}
    runs = [{"repo": row.get("repo"), "number": row.get("number"), "title": row.get("title"),
             "lane": row.get("lane"), "model": row.get("run_model") or row.get("model"),
             "age": tickets_table.age_in_state(row, entered, time.time())[0], "html_url": row.get("html_url")}
            for row in _all_tickets(state) if row.get("state") == "running" and row.get("open", True)]
    stale = bool(payload.get("stale"))
    read = payload.get("ticket_refreshed_at")
    return SourceState(name="Station tickets", data=runs, last_good_at=read, read_at=read, stale=stale,
                       error=(payload.get("stale_reason") or "GitHub could not refresh ticket data") if stale else "")


def _dispatch_tiers(handler, state):
    """The Tiers and allowances page (loom#968), from the background reads:
    a request never reads the gateway, Grug or the database."""
    from . import tiers, tiers_sources
    now = datetime.now(timezone.utc)
    payload = state.response() if hasattr(state, "response") else {}
    background = getattr(state, "tier_sources", None)
    snapshot = background.snapshot() if background is not None else tiers_sources.empty_snapshot()
    sources = dict(snapshot, usage=_tiers_usage(state, now), station=_tiers_station(state, payload))
    body = tiers.render(tiers.build(sources, now))
    return handler.send_page(page("Tiers and allowances", "tiers", body,
                                  status="stale" if payload.get("stale") else "live"))


def _dispatch_brain_hot_spots(handler, state, path):
    from datetime import datetime, timezone
    payload = state.response()
    waits_error = getattr(state, "waits_error", "")
    if path == "/brain":
        data = brain_view.build_brain(payload.get("brain_step"), payload.get("brain_step_error"),
                                      payload.get("improving", {}).get("journals", []),
                                      payload.get("wait_rows"), waits_error)
        body, title, active = brain_view.render_brain(data), "Brain", "brain"
    else:
        bot = os.environ.get("LOOM_REVIEW_BOT", "").strip() or hot_spots.DEFAULT_REVIEW_BOT
        data = hot_spots.build_hot_spots(payload, waits_error, datetime.now(timezone.utc), bot)
        body, title, active = hot_spots.render_hot_spots(data), "Hot spots", "hot-spots"
    return handler.send_page(page(title, active, body, status="stale" if payload.get("stale") else "live"))


def _dispatch_results(handler, state):
    from datetime import datetime, timezone
    from . import results
    payload = state.response()
    body = results.render_results(results.build_results(payload, datetime.now(timezone.utc)))
    return handler.send_page(page("Results", "results", body,
                                  status="stale" if payload.get("stale") else "live"))


def _dispatch_free(handler, state, parsed):
    from . import free_view
    operator = free_view.operator_login((parse_qs(parsed.query).get("operator") or [""])[0])
    payload = state.response()
    found = free_view.build(getattr(state, "open_issues", {}) or {}, payload.get("tickets") or {},
                            operator, payload.get("issue_comments") or [])
    read_at = getattr(state, "open_issues_read_at", 0)
    stamp = datetime.fromtimestamp(read_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if read_at else None
    body = free_view.render(found, operator, stamp, getattr(state, "open_issues_error", ""))
    return handler.send_page(page("Free to take", "free", body, status="Read only"))


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
        body = views.render_lanes(_lane_view_data(payload, tickets, state=state))
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


def _merge_lane_rows(comments):
    """Lane rows from the newest status comment, plus remote heartbeat lanes."""
    status_comments = [c for c in comments if str(c.get("body") or "").startswith(views.LANES_MARKER)]
    latest = max(status_comments, key=lambda c: str(c.get("created_at") or ""), default=None)
    lane_rows = views.parse_lane_rows(latest.get("body") if latest else "")
    by_lane = {row["lane"]: row for row in lane_rows}
    for lane_name, remote in views.parse_remote_heartbeats(comments).items():
        if lane_name not in by_lane:
            lane_rows.append(remote)
            by_lane[lane_name] = remote
        elif remote.get("available"):
            by_lane[lane_name]["available"] = True
    return lane_rows


def _newest_lane_models(prs):
    """Each lane's model and time from its newest lane pull request footer."""
    newest = {}
    for pr in prs:
        parsed = views.footer(pr.get("body"))
        if not parsed:
            continue
        when = str(pr.get("created_at") or pr.get("updated_at") or "")
        if parsed[2] not in newest or when >= newest[parsed[2]]["time"]:
            newest[parsed[2]] = {"model": parsed[3], "time": when}
    return newest


def _usage_rows(payload, state):
    """The usage records: the payload's, else the server's usage store."""
    rows = payload.get("usage")
    if rows is None:
        providers = getattr(getattr(state, "usage", None), "providers", None)
        rows = [row for items in providers.values() for row in items] if isinstance(providers, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def _lane_quota(lane, rows):
    """The highest used percentage among the usage records for a lane: those
    naming it, or its provider (the lane name without a -N slot suffix)."""
    provider = re.sub(r"-[0-9]+$", "", lane)
    used = []
    for row in rows:
        named = lane in (row.get("lanes") or []) or row.get("provider") in (lane, provider)
        try:
            used += [float(row["used_pct"])] if named and row.get("used_pct") is not None else []
        except (TypeError, ValueError):
            continue
    return max(used) if used else None


def _hold_reason(lane, rows, now=None):
    """Why a held lane is held, from the usage records' heartbeat reasons:
    "limit: out of credit; unavailable until 2026-10-16T00:01:27Z" reads
    "out of credit until 2026-10-16 00:01 UTC". A hold whose time has passed
    says it ended and that the lane has not reported since, never a past
    "until" read as a live hold."""
    reason = next((row["holds"][lane] for row in rows
                   if isinstance(row.get("holds"), dict) and lane in row["holds"]), "")
    if not reason:
        return "no reason"
    reason = re.sub(r"^limit:\s*", "", reason)
    match = re.search(r";\s*(?:lane waits|unavailable) until (\S+)", reason)
    if not match:
        return reason
    stamp = match.group(1).rstrip(";")
    try:
        local = views._local_time(stamp)
        ends = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return reason
    if ends <= (now or datetime.now(timezone.utc)):
        return "its hold (%s) ended %s; no heartbeat from the lane since" % (reason[:match.start()], local)
    return reason[:match.start()] + " until " + local


def _lane_card(item, tickets, newest_pr_for_lane, usage_records):
    """One lane's card: its row plus the active ticket, model, quota and reason."""
    lane_name = item["lane"]
    active = next((ticket for ticket in tickets if ticket.get("lane") == lane_name
                   and ticket.get("state") in ("running", "pr")), None)
    model_info = newest_pr_for_lane.get(lane_name)
    model_display = None
    if model_info:
        age_str = views.format_age(model_info["time"])
        model_display = ("%s (last PR %s)" % (model_info["model"], age_str)
                         if age_str else model_info["model"])
    reason = _hold_reason(lane_name, usage_records)
    if item["available"] and reason == "no reason":
        reason = "available"
    item.update({
        "ticket": active,
        "model": model_display or bus_brain.lane_model(lane_name),
        "quota": _lane_quota(lane_name, usage_records),
        "reason": reason,
    })
    return item


def _scoreboard_rows(prs, tickets):
    """The lane pull requests with a footer, each tagged with its ticket's tier."""
    tickets_by_ref = {(row.get("repo"), str(row.get("number"))): row for row in tickets}
    prepared = []
    for pr in prs:
        parsed = views.footer(pr.get("body"))
        if not parsed:
            continue
        ticket = tickets_by_ref.get(parsed[:2])
        prepared.append({**pr, "tier": ticket.get("difficulty") if ticket else None})
    return prepared


def _anthropic_share(payload):
    """The Anthropic percentage from the newest metrics comment's Spread line."""
    metric_rows = views.parse_metrics(payload.get("improving", {}).get("metrics_comments", []))
    if not metric_rows:
        return None
    spread = metric_rows[0][1].get("Spread", {}).get("value", "")
    match = re.search(r"Anthropic\s+(\d+)/(\d+)", spread)
    if match and int(match.group(2)):
        return round(100 * int(match.group(1)) / int(match.group(2)))
    return None


def _lane_view_data(payload, tickets, state=None):
    prs = payload.get("pull_requests", [])
    newest_pr_for_lane = _newest_lane_models(prs)
    usage_records = _usage_rows(payload, state)
    lanes = [_lane_card(item, tickets, newest_pr_for_lane, usage_records)
             for item in _merge_lane_rows(payload.get("lanes", []))]
    return {"lanes": lanes, "scoreboard": views.build_scoreboard(_scoreboard_rows(prs, tickets)),
            "anthropic_share": _anthropic_share(payload)}
