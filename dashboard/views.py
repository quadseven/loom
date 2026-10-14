"""Data builders and HTML fragments for the improving and lane views."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import quote

METRICS_MARKER = "<!-- loom:metrics v1 -->"
JOURNAL_MARKER = "<!-- loom:journal v1 -->"
LANES_MARKER = "<!-- loom:lanes v1 -->"
def map_repo(repo: str) -> str:
    """Map dot-separated repo names like owner.name back to owner/name."""
    if "/" not in repo and "." in repo:
        owner, _, name = repo.partition(".")
        return f"{owner}/{name}"
    return repo


class _FooterMatch:
    def __init__(self, raw_repo, number, lane, model, real_match):
        self._repo = map_repo(raw_repo)
        self._number = str(number)
        self._lane = str(lane)
        self._model = str(model)
        self._real_match = real_match

    def groups(self, default=None):
        return (self._repo, self._number, self._lane, self._model)

    def group(self, *args):
        if not args or args == (0,):
            return self._real_match.group(0)
        if len(args) == 1:
            idx = args[0]
            if idx == 1:
                return self._repo
            if idx == 2:
                return self._number
            if idx == 3:
                return self._lane
            if idx == 4:
                return self._model
            return self._real_match.group(idx)
        return tuple(self.group(idx) for idx in args)

    def __getitem__(self, item):
        return self.group(item)

    def __bool__(self):
        return True

    def span(self, *args):
        return self._real_match.span(*args)

    def start(self, *args):
        return self._real_match.start(*args)

    def end(self, *args):
        return self._real_match.end(*args)


class _FooterPattern:
    _RE = re.compile(
        r"Loom ticket\s+([A-Za-z0-9_.-]+?)(?:#|-)(\d+)(?:-r\d+)?,?\s+lane\s+([A-Za-z0-9_.-]+),\s+model\s+(.+?)\.(?:\s|$)"
    )

    def search(self, string, *args, **kwargs):
        m = self._RE.search(str(string or ""), *args, **kwargs)
        if m is None:
            return None
        return _FooterMatch(m.group(1), m.group(2), m.group(3), m.group(4), m)

    def match(self, string, *args, **kwargs):
        m = self._RE.match(str(string or ""), *args, **kwargs)
        if m is None:
            return None
        return _FooterMatch(m.group(1), m.group(2), m.group(3), m.group(4), m)

    def finditer(self, string, *args, **kwargs):
        for m in self._RE.finditer(str(string or ""), *args, **kwargs):
            yield _FooterMatch(m.group(1), m.group(2), m.group(3), m.group(4), m)

    def findall(self, string, *args, **kwargs):
        return [m.groups() for m in self.finditer(string, *args, **kwargs)]

    @property
    def pattern(self):
        return self._RE.pattern


FOOTER = _FooterPattern()
NUMBER = re.compile(r"^\d+\. (PASS|FAIL) (.+?): (.*?)(?: \(target (.*?)\))?$", re.M)
REF = re.compile(r"(?:https?://[A-Za-z0-9_.-]+/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")
REMOTE_HEARTBEAT_MARKER = "<!-- loom:remote-heartbeat v1"
REMOTE_HEARTBEAT_RE = re.compile(r"<!-- loom:remote-heartbeat v1\s+([A-Za-z0-9_.-]+)(?:\s+([0-9.]+))?\s*-->")


def parse_footer(body):
    match = FOOTER.search(str(body or ""))
    if not match:
        return None
    return match.groups()


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


def _ticket_for_friction(journal, friction, body, tickets):
    # 1. Direct ticket ref in friction line
    refs = REF.findall(friction)
    for repo, number in refs:
        ticket = next((row for row in tickets if row.get("repo") == repo
                       and str(row.get("number")) == number), None)
        if ticket:
            return ticket

    # 2. Match ticket that has a Refs to this journal comment
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

    # 3. Match ticket whose Why section (or body) quotes the friction text
    friction_clean = _clean_text(friction)
    if friction_clean and len(friction_clean) > 8:
        for row in tickets:
            ticket_body = str(row.get("body") or "")
            why_match = re.search(r"##\s*Why\s*\n(.*?)(?=\n##|\Z)", ticket_body, re.S | re.I)
            why_text = why_match.group(1) if why_match else ticket_body
            why_clean = _clean_text(why_text)
            if friction_clean in why_clean or why_clean in friction_clean:
                return row
            f_words = [w for w in friction_clean.split() if len(w) > 3]
            if len(f_words) >= 4:
                matches = sum(1 for w in f_words if w in why_clean)
                if matches >= len(f_words) * 0.75:
                    return row

    # 4. Refs in journal body
    refs = REF.findall(body)
    for repo, number in refs:
        ticket = next((row for row in tickets if row.get("repo") == repo
                       and str(row.get("number")) == number), None)
        if ticket:
            return ticket

    # 5. Ticket referencing the journal's repo and issue_number
    if journal.get("repo") and journal.get("issue_number"):
        ticket = next((row for row in tickets
                       if (journal["repo"], str(journal["issue_number"])) in row.get("refs", [])), None)
        if ticket:
            return ticket

    return None


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
        match = FOOTER.search(body)
        if not match:
            continue
        repo, number, lane, model = match.groups()
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


def render_improving(data):
    esc = html.escape
    parts = ['<section class="card"><h2>Noticed to moved</h2><div class="improvement-list">']
    for row in data["records"]:
        ticket = row["ticket"]
        source = row["journal"]
        ref = source.get("html_url") or ""
        journal = '<a href="%s">journal</a>' % esc(ref, True) if ref else "journal"
        ticket_html = ('<a href="%s">#%s %s</a>' % (_ticket_href(ticket["repo"], ticket["number"]),
                       esc(str(ticket["number"])), esc(str(ticket.get("title") or "ticket")))
                       if ticket else "ticket not found")
        moved = '<strong class="good">moved: %s</strong>' % esc(row["moved"]) if row["moved"] else ""
        segments = "".join('<span class="%s"></span>' % (
            "done" if index < row["stage"] else "current" if index == row["stage"] else "")
            for index in range(4))
        parts.append('<article class="improvement"><div class="progress four">%s</div><p>"%s"</p><p>%s / %s / %s %s</p></article>' % (
            segments, esc(row["friction"]), journal, ticket_html,
            esc(str(row["state"])), moved))
    parts.append("</div></section><section><h2>Factory numbers</h2>")
    if not data["metrics_available"]:
        parts.append('<p class="empty-state">Daily metrics are missing: no metrics comment was found.</p>')
    else:
        parts.append('<div class="factory-grid">')
        for name, current in data["latest"].items():
            old = data["previous"].get(name)
            if old is not None:
                direction = ("better" if current["passed"] and not old["passed"] else
                             "worse" if old["passed"] and not current["passed"] else
                             "better" if current["passed"] else "worse")
                direction_class = "good" if direction == "better" else "bad"
            else:
                direction = "previous value missing"
                direction_class = ""
            parts.append('<article class="card metric factory-card"><span class="metric-val">%s</span><strong class="metric-name">%s</strong><span class="metric-prev %s">was %s / %s</span></article>' % (
                esc(current["value"]), esc(name), esc(direction_class), esc(old["value"] if old is not None else "missing"), esc(direction)))
        parts.append('</div>')
    parts.append("</section>")
    return "".join(parts)


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
        reason = lane.get("reason") or ("heartbeat available" if lane["available"] else lane.get("heartbeat") or "unavailable reason missing")
        ticket = lane.get("ticket")
        ticket_html = ('<a href="%s">%s</a>' % (_ticket_href(ticket["repo"], ticket["number"]), esc(str(ticket["title"] or "ticket"))) if ticket else "")
        quota = lane.get("quota")
        quota_html = '<div class="quota"><span style="width:%d%%"></span></div>' % max(0, min(100, int(quota))) if quota is not None else '<small>quota missing</small>'
        parts.append('<article class="card lane-card"><strong>%s</strong><span>%s / %s</span><p>%s %s</p>%s</article>' % (
            esc(lane["lane"]), esc(availability), esc(lane.get("model") or "model missing"),
            ticket_html, esc(reason), quota_html))
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
