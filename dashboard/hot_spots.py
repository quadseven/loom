"""The Hot spots view: where the factory slows and what keeps failing.

Counts and rates are computed from comments here, never written by a model.
A source that was not read is shown as missing with its reason.
"""
from __future__ import annotations

import html
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

WAIT_STATES = ("queued", "briefed", "running", "pr", "merged", "shipped")
WINDOW = timedelta(days=7)
FAILURE_LIMIT = 20
OUTCOME_RE = re.compile(r"^Lane (\S+) (failed|opened a pull request|pushed fixes to a pull request)", re.M)
RUNG_RE = re.compile(r"<!-- loom:rung (\d+) -->")
RULE_RE = re.compile(r"\|\s*`([^`]+)`\s*\|")


def relevant(body: str) -> bool:
    """Whether an issue comment is a station outcome or a rung marker."""
    return bool(OUTCOME_RE.search(body) or RUNG_RE.search(body))


def _at(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def bottleneck(waits: Any) -> Optional[str]:
    """The state with the longest median wait, among states with 3 or more samples."""
    eligible = [(name, statistics.median(waits[name])) for name in WAIT_STATES
                if isinstance(waits, dict) and isinstance(waits.get(name), list) and len(waits[name]) >= 3]
    return max(eligible, key=lambda pair: pair[1])[0] if eligible else None


def lane_family(lane: str) -> str:
    return re.sub(r"-\d+$", "", lane)


def lane_failure_rates(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Failed over finished lane runs per lane family, from station outcome comments."""
    failed, total = Counter(), Counter()
    for comment in comments:
        for lane, outcome in OUTCOME_RE.findall(str(comment.get("body") or "")):
            family = lane_family(lane)
            total[family] += 1
            failed[family] += outcome == "failed"
    rows = [{"lane": lane, "failed": failed[lane], "total": total[lane],
             "rate": round(100 * failed[lane] / total[lane]),
             "over": 100 * failed[lane] / total[lane] > FAILURE_LIMIT} for lane in total]
    return sorted(rows, key=lambda row: (-row["rate"], row["lane"]))


def finding_rules(comments: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Review bot finding rules counted over the last 7 days against the 7 before."""
    this_week, last_week = Counter(), Counter()
    for comment in comments:
        rule = RULE_RE.search(str(comment.get("body") or "").split("\n", 1)[0])
        at = _at(comment.get("created_at"))
        if not rule or at is None:
            continue
        if now - WINDOW <= at <= now:
            this_week[rule.group(1)] += 1
        elif now - 2 * WINDOW <= at < now - WINDOW:
            last_week[rule.group(1)] += 1
    rows = []
    for rule, count in this_week.items():
        before = last_week.get(rule, 0)
        rows.append({"rule": rule, "count": count, "before": before,
                     "trend": "new" if not before else "up" if count > before else "down" if count < before else "same"})
    return sorted(rows, key=lambda row: (-row["count"], row["rule"]))


def stuck_tickets(tickets: List[Dict[str, Any]], comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tickets that are failed, or whose newest rung marker is above 0."""
    rungs: Dict[tuple, int] = {}
    for comment in sorted(comments, key=lambda c: str(c.get("created_at") or "")):
        marker = RUNG_RE.search(str(comment.get("body") or ""))
        issue = re.search(r"/repos/([^/]+/[^/]+)/issues/(\d+)$", str(comment.get("issue_url") or ""))
        if marker and issue:
            rungs[(issue.group(1), issue.group(2))] = int(marker.group(1))
    rows = []
    for ticket in tickets:
        rung = rungs.get((ticket.get("repo"), str(ticket.get("number"))))
        if ticket.get("state") == "failed" or (rung or 0) > 0:
            rows.append({"ticket": ticket, "rung": rung})
    return sorted(rows, key=lambda row: (-(row["rung"] or 0), str(row["ticket"].get("repo")), row["ticket"].get("number") or 0))


def build_hot_spots(tickets: List[Dict[str, Any]], waits: Any, sources: Optional[Dict[str, Any]],
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    sources = sources or {}
    issue_comments, review_comments = sources.get("issue_comments"), sources.get("review_comments")
    return {
        "waits": [{"state": name, "median": statistics.median(waits[name]) if waits[name] else None,
                   "worst": max(waits[name]) if waits[name] else None, "n": len(waits[name])}
                  for name in WAIT_STATES if isinstance(waits, dict) and isinstance(waits.get(name), list)] or None,
        "bottleneck": bottleneck(waits),
        "rules": finding_rules(review_comments, now) if review_comments is not None else None,
        "lanes": lane_failure_rates(issue_comments) if issue_comments is not None else None,
        "stuck": stuck_tickets(tickets, issue_comments) if issue_comments is not None else None,
        "errors": list(sources.get("errors") or []),
    }


def _missing(what: str, reason: str) -> str:
    return '<p class="empty-state">%s: missing. %s</p>' % (html.escape(what), html.escape(reason))


def _hours(value: float) -> str:
    return "%.1fh" % value


def _waits_html(data: Dict[str, Any]) -> str:
    if not data["waits"]:
        return _missing("Wait times", "No per-state wait samples have been loaded (docs: python3 -m bus.metrics waits).")
    rows = []
    for row in data["waits"]:
        flag = ' <strong class="bottleneck-note">bottleneck: longest median wait</strong>' if row["state"] == data["bottleneck"] else ""
        text = ("median %s, worst %s over %d tickets" % (_hours(row["median"]), _hours(row["worst"]), row["n"])
                if row["n"] else "no tickets waited here")
        rows.append('<p class="hot-row%s"><span class="machine-fact">%s</span> %s%s</p>' % (
            " bottleneck" if flag else "", html.escape(row["state"]), html.escape(text), flag))
    if not data["bottleneck"]:
        rows.append('<p class="muted">No bottleneck named: no state has 3 tickets yet.</p>')
    return "".join(rows)


def _rules_html(data: Dict[str, Any]) -> str:
    if data["rules"] is None:
        return _missing("Review findings", "Review comments were not read.")
    if not data["rules"]:
        return '<p class="muted">The review bot sent back no findings with a rule in the last 7 days.</p>'
    arrows = {"up": "&uarr; up", "down": "&darr; down", "same": "&rarr; same", "new": "&uarr; new"}
    return "".join('<p class="hot-row"><span class="machine-fact">%s</span> %d in 7 days, %s (%s)</p>' % (
        html.escape(row["rule"]), row["count"], arrows[row["trend"]],
        "was %d the 7 days before" % row["before"] if row["before"] else "none the 7 days before")
        for row in data["rules"])


def _lanes_html(data: Dict[str, Any]) -> str:
    if data["lanes"] is None:
        return _missing("Lane failure rate", "Station outcome comments were not read.")
    if not data["lanes"]:
        return '<p class="muted">No lane has finished a run in the comments read.</p>'
    bars = []
    for row in data["lanes"]:
        word = "over %d%%, failing too often" % FAILURE_LIMIT if row["over"] else "within %d%%" % FAILURE_LIMIT
        bars.append('<div class="lane-bar%s"><div class="bar-track"><span style="height:%d%%"></span></div>'
                    '<strong>%d%%</strong><span class="machine-fact">%s</span>'
                    '<small>%d of %d failed, %s</small></div>' % (
                        " over" if row["over"] else "", row["rate"], row["rate"], html.escape(row["lane"]),
                        row["failed"], row["total"], word))
    return '<div class="lane-bars">%s</div>' % "".join(bars)


def _stuck_html(data: Dict[str, Any]) -> str:
    if data["stuck"] is None:
        return _missing("Stuck tickets", "Rung markers were not read.")
    if not data["stuck"]:
        return '<p class="muted">No ticket is failed or above rung 0.</p>'
    out = []
    for row in data["stuck"]:
        ticket = row["ticket"]
        href = "/t/%s/%d" % (quote(str(ticket.get("repo") or ""), safe="/"), int(ticket.get("number") or 0))
        rung = "rung %d" % row["rung"] if row["rung"] is not None else "no rung marker in the comments read"
        out.append('<p class="hot-row"><a href="%s">%s#%s</a> %s <span class="muted">%s, %s</span></p>' % (
            html.escape(href, True), html.escape(str(ticket.get("repo"))), html.escape(str(ticket.get("number"))),
            html.escape(str(ticket.get("title") or "Untitled ticket")), html.escape(str(ticket.get("state") or "unlabeled")), rung))
    return "".join(out)


def render_hot_spots(data: Dict[str, Any]) -> str:
    notes = "".join('<p class="empty-state">Source error: %s</p>' % html.escape(err) for err in data["errors"])
    return (notes
            + '<section class="card"><h2>Where it slows</h2>%s</section>' % _waits_html(data)
            + '<section class="card"><h2>What the review bot keeps sending back</h2>%s</section>' % _rules_html(data)
            + '<section class="card"><h2>Lane failure rate</h2>%s</section>' % _lanes_html(data)
            + '<section class="card"><h2>Repeatedly stuck tickets</h2>%s</section>' % _stuck_html(data))
