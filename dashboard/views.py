"""Data builders and HTML fragments for the improving and lane views."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import quote

METRICS_MARKER = "<!-- loom:metrics v1 -->"
JOURNAL_MARKER = "<!-- loom:journal v1 -->"
LANES_MARKER = "<!-- loom:lanes v1 -->"
REMOTE_HEARTBEAT_MARKER = "<!-- loom:remote-heartbeat v1"
REMOTE_HEARTBEAT_RE = re.compile(r"<!-- loom:remote-heartbeat v1\s+([A-Za-z0-9_.-]+)(?:\s+([0-9.]+))?\s*-->")


def parse_remote_heartbeats(comments):
    beats = {}
    for comment in comments or []:
        body = str(comment.get("body") or "") if isinstance(comment, dict) else str(comment or "")
        match = REMOTE_HEARTBEAT_RE.search(body)
        if match:
            lane = match.group(1).strip()
            ts = match.group(2) if match.group(2) else ""
            beats[lane] = {
                "lane": lane,
                "available": True,
                "heartbeat": ts or "heartbeat available",
            }
    return beats
# A lane pull request's footer names its ticket in the runner's form,
# owner.name-N or owner.name-N-rK (bus/tickets.ticket_name), or as
# owner/name#N, at the start of a line. An owner never has a dot, so the
# first dot splits it; a model id is one token.
FOOTER = re.compile(r"^Loom ticket ([A-Za-z0-9-]+)[./]([A-Za-z0-9_.-]+?)(?:#|-)(\d+)(?:-r\d+)?, "
                    r"lane ([A-Za-z0-9_.-]+), model (\S+?)\.(?:\s|$)", re.M)


def footer(text):
    """(repo, number, lane, model) from a lane pull request footer, or None."""
    match = FOOTER.search(str(text or ""))
    if not match:
        return None
    owner, name, number, lane, model = match.groups()
    return "%s/%s" % (owner, name), number, lane, model.strip()
NUMBER = re.compile(r"^\d+\. (PASS|FAIL) (.+?): (.*?)(?: \(target (.*?)\))?$", re.M)
REF = re.compile(r"(?:https?://[A-Za-z0-9_.-]+/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")


def _ticket_href(repo: str, number: int) -> str:
    return "/t/%s/%d" % (quote(repo, safe="/"), int(number))


def parse_metrics(comments):
    found = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if body.startswith(METRICS_MARKER):
            date = re.search(r"\n(\d{4}-\d\d-\d\d):", body)
            nums = {name: {"value": value, "passed": passed == "PASS"}
                    for passed, name, value, _ in NUMBER.findall(body)}
            found.append((date.group(1) if date else "", nums))
    return sorted(found, key=lambda row: row[0], reverse=True)


def build_improvements(journals, tickets, metrics_comments):
    """Join journal friction lines to referenced tickets and metric movement."""
    metrics = parse_metrics(metrics_comments)
    latest = metrics[0][1] if metrics else {}
    previous = metrics[1][1] if len(metrics) > 1 else {}
    records = [record for journal in journals
               for record in _journal_records(journal, tickets, latest, previous)]
    return {"records": records, "latest": latest, "previous": previous,
            "metrics_available": bool(metrics)}


def _is_none(text: str) -> bool:
    clean = text.strip().strip(".").strip().lower()
    return not clean or clean == "none"


def _clean_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _journal_records(journal, tickets, latest, previous):
    records = []
    body = str(journal.get("body") or "")
    if not body.startswith(JOURNAL_MARKER):
        return records
    for line in body.splitlines():
        match = re.match(r"^[ \t]*-?[ \t]*(?:Friction|Blockers?|Blocked)[ \t]*:[ \t]*(.+)$", line, re.I)
        if not match:
            continue
        friction = match.group(1).strip()
        if _is_none(friction):
            continue
        ticket = _ticket_for_friction(journal, friction, body, tickets)
        state = (ticket or {}).get("state") or "not ticketed"
        moved = _moved_metric(ticket, state, latest, previous)
        stage = _improvement_stage(ticket, state, moved)
        records.append({"friction": friction, "journal": journal, "ticket": ticket,
                        "state": state, "moved": moved, "stage": stage})
    return records


def _find_ticket(tickets, refs):
    for repo, number in refs:
        ticket = next((row for row in tickets if row.get("repo") == repo
                       and str(row.get("number")) == number), None)
        if ticket:
            return ticket
    return None


def _ticket_refs_journal(tickets, journal):
    journal_url = journal.get("html_url") or ""
    journal_id = str(journal.get("id") or "")
    for row in tickets:
        ticket_body = str(row.get("body") or "")
        if journal_url and journal_url in ticket_body:
            return row
        if journal_id and ("issuecomment-" + journal_id) in ticket_body:
            return row
        if journal_id and re.search(r"Refs\b[^\n]*\b" + re.escape(journal_id) + r"\b", ticket_body):
            return row
    return None


def _why_quotes_friction(why_clean, friction_clean):
    if friction_clean in why_clean or why_clean in friction_clean:
        return True
    words = [w for w in friction_clean.split() if len(w) > 3]
    return len(words) >= 4 and sum(1 for w in words if w in why_clean) >= len(words) * 0.75


def _ticket_quoting_friction(tickets, friction):
    friction_clean = _clean_text(friction)
    if not friction_clean or len(friction_clean) <= 8:
        return None
    for row in tickets:
        ticket_body = str(row.get("body") or "")
        why_match = re.search(r"##\s*Why\s*\n(.*?)(?=\n##|\Z)", ticket_body, re.S | re.I)
        why_clean = _clean_text(why_match.group(1) if why_match else ticket_body)
        if _why_quotes_friction(why_clean, friction_clean):
            return row
    return None


def _ticket_for_journal_ids(tickets, journal):
    if journal.get("repo") and journal.get("issue_number"):
        return next((row for row in tickets
                     if (journal["repo"], str(journal["issue_number"])) in row.get("refs", [])), None)
    return None


def _ticket_for_friction(journal, friction, body, tickets):
    return (_find_ticket(tickets, REF.findall(friction))
            or _ticket_refs_journal(tickets, journal)
            or _ticket_quoting_friction(tickets, friction)
            or _find_ticket(tickets, REF.findall(body))
            or _ticket_for_journal_ids(tickets, journal))


def _moved_metric(ticket, state, latest, previous):
    if not ticket or state not in ("shipped", "merged", "verified"):
        return None
    return next((name for name, value in latest.items()
                 if name in previous and value["value"] != previous[name]["value"]), None)


def _improvement_stage(ticket, state, moved):
    if moved:
        return 4
    if ticket and state in ("shipped", "merged", "verified"):
        return 2
    return 1 if ticket else 0


def build_scoreboard(pull_requests):
    """Count lane PRs by footer model and difficulty tier."""
    tiers = {}
    for pr in pull_requests:
        body = str(pr.get("body") or "")
        parsed = footer(body)
        if not parsed:
            continue
        repo, number, lane, model = parsed
        tier = str(pr.get("tier") or pr.get("difficulty") or "unknown").lower()
        if tier not in ("easy", "medium", "hard", "frontier"):
            tier = "unknown"
        counts = tiers.setdefault(tier, {}).setdefault(model.strip(),
            {"merged": 0, "open": 0, "closed": 0, "lane": lane.strip()})
        state = str(pr.get("state") or "open").lower()
        key = "merged" if state == "closed" and pr.get("merged_at") else state
        if key in ("merged", "open", "closed"):
            counts[key] += 1
    return tiers


def _improvement_card(row):
    esc = html.escape
    ticket = row["ticket"]
    ref = row["journal"].get("html_url") or ""
    journal = '<a href="%s">journal</a>' % esc(ref, True) if ref else "journal"
    ticket_html = ('<a href="%s">#%s %s</a>' % (_ticket_href(ticket["repo"], ticket["number"]),
                   esc(str(ticket["number"])), esc(str(ticket.get("title") or "ticket")))
                   if ticket else "ticket not found")
    moved = '<strong class="good">moved: %s</strong>' % esc(row["moved"]) if row["moved"] else ""
    segments = "".join('<span class="%s"></span>' % (
        "done" if index < row["stage"] else "current" if index == row["stage"] else "")
        for index in range(4))
    return '<article class="improvement"><div class="progress four">%s</div><p>"%s"</p><p>%s / %s / %s %s</p></article>' % (
        segments, esc(row["friction"]), journal, ticket_html, esc(str(row["state"])), moved)


def _factory_card(name, current, old):
    esc = html.escape
    if old is None:
        direction, direction_class = "previous value missing", ""
    else:
        # Better or worse only when the target flips; a number that
        # held its side of the target says so, never "better".
        direction = ("better" if current["passed"] and not old["passed"] else
                     "worse" if old["passed"] and not current["passed"] else
                     "still passing" if current["passed"] else "still failing")
        direction_class = "good" if current["passed"] else "bad"
    return '<article class="card metric factory-card"><span class="metric-val">%s</span><strong class="metric-name">%s</strong><span class="metric-prev %s">was %s / %s</span></article>' % (
        esc(current["value"]), esc(name), esc(direction_class), esc(old["value"] if old is not None else "missing"), esc(direction))


def render_improving(data):
    parts = ['<section class="card"><h2>Noticed to moved</h2><div class="improvement-list">']
    if not data["records"]:
        parts.append('<p class="empty-state">No friction lines in the journals yet.</p>')
    parts.extend(_improvement_card(row) for row in data["records"])
    parts.append("</div></section><section><h2>Factory numbers</h2>")
    if not data["metrics_available"]:
        parts.append('<p class="empty-state">Daily metrics are missing: no metrics comment was found.</p>')
    else:
        parts.append('<div class="factory-grid">')
        parts.extend(_factory_card(name, current, data["previous"].get(name))
                     for name, current in data["latest"].items())
        parts.append('</div>')
    parts.append("</section>")
    return "".join(parts)


def format_heartbeat(raw):
    raw = str(raw or "").strip()
    if not raw or raw == "-":
        return "heartbeat missing"
    match = re.search(r"(\d{1,2}:\d{2})(?::\d{2})?Z?", raw)
    if match:
        return "heartbeat %sZ" % match.group(1)
    if raw.startswith("heartbeat "):
        return raw
    return "heartbeat " + raw


def format_age(dt, now=None):
    if not dt:
        return None
    now = now or datetime.now(timezone.utc)
    if isinstance(dt, (int, float)) and not isinstance(dt, bool):
        dt = datetime.fromtimestamp(dt, timezone.utc)
    elif isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return "less than a minute ago"
    minutes = seconds // 60
    if minutes < 60:
        return "%d minute%s ago" % (minutes, "" if minutes == 1 else "s")
    hours = minutes // 60
    if hours < 24:
        return "%d hour%s ago" % (hours, "" if hours == 1 else "s")
    days = hours // 24
    return "%d day%s ago" % (days, "" if days == 1 else "s")


def parse_lane_rows(body):
    rows = []
    for line in str(body or "").splitlines():
        match = re.match(r"\|\s*([^|]+)\|\s*(yes|no)\s*\|\s*([^|]+)\|", line, re.I)
        if match and match.group(1).strip().lower() != "lane":
            rows.append({"lane": match.group(1).strip(), "available": match.group(2).lower() == "yes",
                         "heartbeat": match.group(3).strip()})
    return rows


def render_lanes(data):
    esc = html.escape
    parts = ['<section><h2>Anthropic share</h2>']
    share = data.get("anthropic_share")
    if share is None:
        parts.append('<p class="empty-state">Share is missing: no lane run totals were available.</p>')
    else:
        parts.append('<div class="share-track"><span style="width:%d%%"></span><i></i></div><p>%d%% of lane runs / cap 50%%</p>' % (min(100, share), share))
    parts.append('</section><section><h2>Lanes</h2><div class="lane-grid">')
    if not data.get("lanes"):
        parts.append('<p class="empty-state">Lane status is missing: no loom:lanes comment was found.</p>')
    for lane in data.get("lanes", []):
        availability = "busy" if lane["ticket"] else ("free" if lane["available"] else "held")
        heartbeat = format_heartbeat(lane.get("heartbeat"))
        ticket = lane.get("ticket")
        ticket_html = ('<a href="%s">%s</a>' % (_ticket_href(ticket["repo"], ticket["number"]), esc(str(ticket["title"] or "ticket"))) if ticket else "")
        line = "%s - %s" % (ticket_html, esc(heartbeat)) if ticket_html else esc(heartbeat)
        quota = lane.get("quota")
        quota_html = '<div class="quota"><span style="width:%d%%"></span></div>' % max(0, min(100, int(quota))) if quota is not None else '<small>quota missing</small>'
        reason = ('<p>%s</p>' % esc(lane["reason"])) if lane.get("reason") else ""
        parts.append('<article class="card lane-card"><strong>%s</strong><span>%s / %s</span><p>%s</p>%s%s</article>' % (
            esc(lane["lane"]), esc(availability), esc(lane.get("model") or "model missing"),
            line, reason, quota_html))
    parts.append('</div></section><section><h2>Models by tier</h2>')
    if not data.get("scoreboard"):
        parts.append('<p class="empty-state">Scoreboard is missing: no lane pull requests matched the runner footer.</p>')
    for tier, models in data.get("scoreboard", {}).items():
        parts.append('<h3>%s</h3>' % esc(tier.title()))
        for model, counts in models.items():
            parts.append('<article class="card metric"><strong>%s</strong><span>%s</span><span>%d merged / %d open / %d closed</span></article>' % (
                esc(model), esc(counts["lane"]), counts["merged"], counts["open"], counts["closed"]))
    return "".join(parts)


def render_usage_row(row, now=None):
    """Render one provider window with text equivalents for every visual cue."""
    esc = html.escape
    now = now or datetime.now(timezone.utc)
    used = row.get("used_pct")
    provider = esc(str(row.get("provider", "")))
    window_label = "5 hours" if row.get("window") == "5-hour" else str(row.get("window", ""))
    window = esc(window_label)
    lanes = ", ".join(row.get("lanes") or []) or "no lanes listed"
    note = esc(str(row.get("note") or ""))
    as_of = datetime.fromisoformat(str(row["as_of"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    age_seconds = max(0, int((now - as_of).total_seconds()))
    age = "less than a minute ago" if age_seconds < 60 else "%d minutes ago" % (age_seconds // 60)
    stale = age_seconds > 7200
    parts = ['<article class="card usage-card"><div class="label">%s | %s</div><h2>%s</h2><p>Lanes: %s</p>' %
             (provider, window, window, esc(lanes))]
    if used is None:
        parts.append('<p><strong>Not exposed yet.</strong> %s</p>' % note)
    else:
        pct = max(0, min(100, float(used)))
        remaining = max(0, 100 - pct)
        parts.append('<div class="usage-track" role="img" aria-label="%.1f%% used, %.1f%% left"><span style="width:%.1f%%"></span></div><p>%.1f%% used / %.1f%% left</p>' % (pct, remaining, pct, pct, remaining))
    reset = row.get("resets_at")
    if reset:
        try:
            reset_at = datetime.fromisoformat(reset.replace("Z", "+00:00")).astimezone(timezone.utc)
            seconds = int((reset_at - now).total_seconds())
            relative = "in %dh" % (seconds // 3600) if seconds >= 3600 else "in %dm" % max(0, seconds // 60)
            absolute = reset_at.strftime("%Y-%m-%d %H:%M UTC")
            parts.append('<p>Resets at %s (%s)</p>' % (esc(absolute), esc(relative)))
        except ValueError:
            parts.append('<p>Reset time unavailable.</p>')
    else:
        parts.append('<p>Reset time unavailable.</p>')
    parts.append('<p>Source: %s</p><p>Reading: %s%s</p></article>' %
                 (esc(str(row.get("source", ""))), age, ' <strong>Stale</strong>' if stale else ""))
    return "".join(parts)
