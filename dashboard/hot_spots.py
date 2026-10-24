"""Data builders and HTML for the /hot-spots view: where tickets wait, what
the review bot keeps sending back, lane failure rates and stuck tickets.
Every count and rate here is computed from GitHub records."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from typing import Optional

from bus import ladders
from bus.metrics import bottleneck as name_bottleneck
from bus.station import failure_class
from .views import _ticket_href

WINDOW = timedelta(days=7)
DONE_STATES = ("merged", "shipped", "verified")
# A lane failing more than this share of its runs is marked failing often.
FAIL_LIMIT_PCT = 20
LANE_BARS = 5
TOP_FINDINGS = 8
# The station's outcome comment on a ticket (bus/station.py, _report).
OUTCOME_RE = re.compile(r"^Lane ([A-Za-z0-9_.-]+) (failed: |opened a pull request|pushed fixes to a pull request)")
# The bot opens each finding "<symbol> medium | _category_ | `rule` | ...".
FINDING_RE = re.compile(r"^\W*(?:critical|high|medium|low) \|")
RULE_RE = re.compile(r"`([^`\n]+)`")
ISSUE_URL_RE = re.compile(r"/issues/([0-9]+)$")


def _stamp(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _within(comment, start, end) -> bool:
    at = _stamp(comment.get("created_at"))
    return at is not None and start < at <= end


def _outcome(comment):
    """(lane, failed) for a station outcome comment, or None. Provider limits
    and transient failures say nothing about the lane, so they are left out."""
    body = str(comment.get("body") or "")
    match = OUTCOME_RE.match(body)
    if not match:
        return None
    failed = match.group(2) == "failed: "
    if failed and failure_class(body[match.end():]) != "work":
        return None
    # A slot (codex-2) counts toward its lane (codex).
    return re.sub(r"-[0-9]+$", "", match.group(1)), failed


def lane_failure_rates(comments, now):
    """The busiest lanes over the last 7 days with their work failure rate."""
    counts = {}
    for comment in comments or []:
        found = _outcome(comment) if _within(comment, now - WINDOW, now) else None
        if found:
            row = counts.setdefault(found[0], {"lane": found[0], "failed": 0, "runs": 0})
            row["runs"] += 1
            row["failed"] += found[1]
    rows = sorted(counts.values(), key=lambda row: (-row["runs"], row["lane"]))[:LANE_BARS]
    for row in rows:
        row["pct"] = round(100 * row["failed"] / row["runs"])
        row["high"] = row["failed"] * 100 > row["runs"] * FAIL_LIMIT_PCT
    return rows


def _by_bot(comment, bot) -> bool:
    user = comment.get("user") or {}
    return (user.get("type") == "Bot"
            and str(user.get("login") or "").lower() in (bot.lower(), bot.lower() + "[bot]"))


def _rule(body: str) -> Optional[str]:
    first = body.split("\n", 1)[0]
    if not FINDING_RE.match(first):
        return None
    match = RULE_RE.search(first)
    fields = [part.strip(" _*") for part in first.split("|")]
    return match.group(1) if match else (fields[1] if len(fields) > 1 and fields[1] else "rule missing")


def _trend(count, previous) -> str:
    if count > previous:
        return "up from %d" % previous
    if count < previous:
        return "down from %d" % previous
    return "same as the 7 days before"


def finding_counts(comments, bot, now):
    """Review bot findings per rule over the last 7 days, against the 7 before."""
    current, previous = {}, {}
    for comment in comments or []:
        rule = _rule(str(comment.get("body") or "")) if _by_bot(comment, bot) else None
        if rule is None:
            continue
        if _within(comment, now - WINDOW, now):
            current[rule] = current.get(rule, 0) + 1
        elif _within(comment, now - 2 * WINDOW, now - WINDOW):
            previous[rule] = previous.get(rule, 0) + 1
    rows = [{"rule": rule, "count": count, "trend": _trend(count, previous.get(rule, 0)),
             "direction": ("up" if count > previous.get(rule, 0) else
                           "down" if count < previous.get(rule, 0) else "same")}
            for rule, count in current.items()]
    return sorted(rows, key=lambda row: (-row["count"], row["rule"]))[:TOP_FINDINGS]


def _rungs(comments):
    """{(repo, number): rung} from the newest rung marker on each ticket."""
    newest = {}
    for comment in sorted(comments or [], key=lambda c: str(c.get("created_at") or "")):
        rung = ladders.RUNG_RE.search(str(comment.get("body") or ""))
        number = ISSUE_URL_RE.search(str(comment.get("issue_url") or ""))
        if rung and number:
            newest[(comment.get("repo"), int(number.group(1)))] = int(rung.group(1))
    return newest


def stuck_tickets(tickets, comments):
    """Tickets above rung 0 or failed, highest rung first."""
    rungs = _rungs(comments)
    stuck = [{**row, "rung": rungs.get((row.get("repo"), row.get("number")), 0)} for row in tickets]
    # A ticket that climbed and then merged is no longer stuck, and neither
    # is a closed one: it is out of the factory, whatever label it kept.
    stuck = [row for row in stuck if row.get("open", True)
             and (row.get("state") == "failed"
                  or (row["rung"] > 0 and row.get("state") not in DONE_STATES))]
    return sorted(stuck, key=lambda row: (-row["rung"], str(row.get("repo")), row.get("number") or 0))


def build_hot_spots(payload, waits_error, now, bot=ladders.DEFAULT_REVIEW_BOT):
    """Everything /hot-spots shows, from the dashboard snapshot."""
    tickets = [row for rows in (payload.get("tickets") or {}).values() if rows for row in rows]
    comments = payload.get("issue_comments") or []
    waits = payload.get("wait_rows") or []
    return {"waits": waits, "bottleneck": name_bottleneck(waits),
            "waits_error": waits_error or "waits have not been read yet",
            "comments_error": payload.get("hot_spots_error") or "",
            "findings": finding_counts(payload.get("review_comments"), bot, now),
            "rates": lane_failure_rates(comments, now),
            "stuck": stuck_tickets(tickets, comments)}


def _pct(value, top) -> int:
    return max(2, min(100, round(100 * value / top))) if top else 2


def _wait_row(row, bottleneck, top):
    esc = html.escape
    state = esc(str(row.get("state")))
    if row.get("median_h") is None:
        return ('<li><p class="bar-head"><span>%s</span><span>no samples</span></p>'
                '<span class="hbar"><i style="width:0%%"></i></span></li>' % state)
    worst = " (%s)" % esc(str(row["worst_ticket"])) if row.get("worst_ticket") else ""
    mark = " bottleneck" if row.get("state") == bottleneck else ""
    return ('<li class="wait%s"><p class="bar-head machine-fact"><span class="state">%s</span>'
            '<span>%.1fh median / %.1fh worst%s / %d tickets</span></p>'
            '<span class="hbar"><i style="width:%d%%"></i></span></li>') % (
        mark, state, row["median_h"], row["worst_h"], worst, row.get("n") or 0,
        _pct(row["median_h"], top))


def render_waits(data):
    if not data["waits"]:
        return '<p class="empty-state">Waits are missing: %s.</p>' % html.escape(data["waits_error"])
    top = max((row.get("median_h") or 0) for row in data["waits"])
    rows = "".join(_wait_row(row, data["bottleneck"], top) for row in data["waits"])
    by_state = {row.get("state"): row for row in data["waits"]}
    note = ('<p class="bottleneck-note">%s is the bottleneck: median %.1fh, the longest.</p>' % (
        html.escape(data["bottleneck"]), by_state[data["bottleneck"]]["median_h"])
        if data["bottleneck"] in by_state else '<p>No bottleneck: no state has 3 samples yet.</p>')
    return '<ul class="wait-list">%s</ul>%s' % (rows, note)


def render_findings(data):
    esc = html.escape
    if data["comments_error"]:
        return '<p class="empty-state">Review findings are missing: %s.</p>' % esc(data["comments_error"])
    if not data["findings"]:
        return '<p class="empty-state">The review bot opened no findings in the last 7 days.</p>'
    top = max(row["count"] for row in data["findings"])
    return '<ul class="finding-list">%s</ul>' % "".join(
        '<li><p class="bar-head"><code>%s</code><span class="machine-fact">%d '
        '<span class="trend trend-%s">%s</span></span></p>'
        '<span class="hbar bad"><i style="width:%d%%"></i></span></li>' % (
            esc(row["rule"]), row["count"], esc(row.get("direction", "same")), esc(row["trend"]),
            _pct(row["count"], top)) for row in data["findings"])


def render_failure_rates(rates):
    esc = html.escape
    if not rates:
        return '<p class="empty-state">No lane outcome comments in the last 7 days.</p>'
    top = max(row["pct"] for row in rates) or 1
    bars = "".join(
        '<div class="lane-bar%s"><span class="bar"><i style="height:%d%%"></i></span>'
        '<strong>%s</strong><span class="pct">%d%%</span><small>%d of %d failed%s</small></div>' % (
            " bad" if row["high"] else "", _pct(row["pct"], top), esc(row["lane"]),
            row["pct"], row["failed"], row["runs"], ", failing often" if row["high"] else "")
        for row in rates)
    return '<div class="lane-bars">%s</div><p class="note">Red, and marked failing often, above %d%%.</p>' % (
        bars, FAIL_LIMIT_PCT)


def _stuck_row(row):
    esc = html.escape
    attempts = row.get("attempts")
    ladder = "rung %d" % row["rung"] + (" / %s attempts" % attempts if isinstance(attempts, int) and attempts > 1 else "")
    lane = "%s / %s" % (row.get("lane") or "unassigned", row.get("model") or "model unknown")
    return ('<a class="ticket-row" href="%s"><div class="ticket-top machine-fact"><span>%s#%s</span>'
            '<span class="state-failed">%s</span></div><div class="ticket-title">%s</div>'
            '<div class="machine-fact stuck-meta">%s / %s</div></a>') % (
        esc(_ticket_href(str(row.get("repo") or ""), row.get("number") or 0), True),
        esc(str(row.get("repo"))), esc(str(row.get("number"))), esc(ladder),
        esc(str(row.get("title") or "Untitled ticket")), esc(str(row.get("state") or "unlabeled")), esc(lane))


def render_stuck(data):
    esc = html.escape
    note = ('<p class="empty-state">Rungs could not be read: %s. Failed tickets only.</p>' % esc(
        data["comments_error"]) if data["comments_error"] else "")
    if not data["stuck"]:
        return note + '<p class="empty-state">No ticket is above rung 0 or failed.</p>'
    return note + '<div class="stuck-grid">%s</div>' % "".join(_stuck_row(row) for row in data["stuck"])


def render_hot_spots(data):
    rates = ('<p class="empty-state">Lane outcomes are missing: %s.</p>' % html.escape(data["comments_error"])
             if data["comments_error"] else render_failure_rates(data["rates"]))
    return ('<p class="page-lede">Where tickets wait, what keeps failing, and what is stuck. Last 7 days.</p>'
            '<div class="hot-grid">'
            '<section class="card hot-spot"><h2 class="label">Where it slows / median wait, worst</h2>%s</section>'
            '<section class="card hot-spot"><h2 class="label">What the review bot keeps sending back</h2>%s'
            '<h3 class="label">Lane failure rate</h3>%s</section></div>'
            '<section class="hot-stuck"><h2 class="label">Repeatedly stuck / up the ladder</h2>%s</section>') % (
        render_waits(data), render_findings(data), rates, render_stuck(data))
