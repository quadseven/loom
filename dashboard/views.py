"""Data builders and HTML fragments for the improving and lane views."""
from __future__ import annotations

import html
import re
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import quote

from bus import factory

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
    newest_first = sorted(journals, key=lambda row: str(row.get("created_at") or ""), reverse=True)
    records, seen = [], {}
    for journal in newest_first:
        for record in _journal_records(journal, tickets, latest, previous):
            # The same friction is often carried from tick to tick: one card
            # per friction, from the newest journal that names it. A ticket
            # briefed from an older journal links that one, so the card
            # keeps the furthest stage any of its journals reached.
            key = _clean_text(record["friction"])
            kept = seen.get(key)
            if kept is None:
                seen[key] = record
                records.append(record)
            elif record["stage"] > kept["stage"]:
                kept.update({name: record[name] for name in ("ticket", "state", "moved", "stage")})
    return {"records": records, "latest": latest, "previous": previous,
            "metrics_available": bool(metrics)}


# "none", "None.", "nothing new observed this tick", "no new slowdown":
# a journal saying there was no friction this tick.
_NO_FRICTION = re.compile(r"(?:none|n a)(?: (?:this tick|new|today))?$|nothing\b"
                          r"|no new (?:slowdowns?|friction|blockers?)\b")


def _is_none(text: str) -> bool:
    return _NO_FRICTION.match(_clean_text(text)) is not None


def _clean_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


# The headings brains write a slowdown under (docs/BRAIN.md, Journal: the
# canonical one is "- Friction:"). Read from the loom#30 journals: "What
# slowed the factory [down] [this tick]", "Factory note(s)", "Factory
# friction", "Improving Loom itself" and a bare "Factory:" carry most of
# them; "Factory health:" is a status line and is not one. A heading may
# carry a qualifier before its colon: "(for the next brief)", "this tick",
# "for the next brain".
_FRICTION_HEAD = re.compile(
    r"^([ \t]*)((?:[-*+]|\d+[.)])[ \t]+)?(?:\*\*)?"
    r"(?:friction|blockers?|blocked|improving[ \t]+loom[ \t]+itself"
    r"|(?:what[ \t]+)?slowed[ \t]+the[ \t]+factory(?:[ \t]+down)?"
    r"|factory(?:[ \t]+(?:notes?|friction|observations?|lessons?))?)"
    r"(?:[ \t]*\([^)\n]*\))?(?:[ \t]+(?:this[ \t]+tick|for[ \t]+the[ \t]+[a-z ]+?))?"
    r"(?:[ \t]*\([^)\n]*\))?[ \t]*(?:\*\*)?[ \t]*:(?:\*\*)?[ \t]*(.*)$", re.I)
_LIST_ITEM = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)]|\(\d+\))[ \t]+(.*)$")
# Another journal line ("- Reports: ...", "Cap+8 rule: ...") ends a
# hard-wrapped friction line.
_STATUS_LINE = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?[A-Za-z][A-Za-z0-9+' -]{0,40}:(?:\*\*)?(?:[ \t]|$)")


def _ends_line(text: str) -> bool:
    return not text or text.rstrip()[-1:] in ".!?;)" or text.rstrip().endswith(".\"")


def _joined(text, lines, at):
    """text plus the hard-wrapped lines that continue it from lines[at],
    and the index after them."""
    text = text.strip()
    while (at < len(lines) and not _ends_line(text) and lines[at].strip()
           and not _LIST_ITEM.match(lines[at]) and not _STATUS_LINE.match(lines[at])
           and not _FRICTION_HEAD.match(lines[at])
           and not lines[at].lstrip().startswith("<!--")):
        text += " " + lines[at].strip()
        at += 1
    return text, at


def friction_lines(body):
    """Each slowdown a journal names, in order. A heading with text after
    its colon is one friction, with any hard-wrapped lines that continue it;
    a heading with nothing after it takes the list under it (a bulleted
    heading only its nested items) or the paragraph that follows. Lines that
    say there was none are left out."""
    lines = str(body or "").splitlines()
    found, at = [], 0
    while at < len(lines):
        head = _FRICTION_HEAD.match(lines[at])
        at += 1
        if not head:
            continue
        indent, bulleted, rest = head.group(1), head.group(2), head.group(3)
        if rest.strip():
            text, at = _joined(rest, lines, at)
            found.append(text)
            continue
        if at < len(lines) and lines[at].strip() and not _LIST_ITEM.match(lines[at]) \
                and not _FRICTION_HEAD.match(lines[at]):
            text, at = _joined(lines[at], lines, at + 1)
            found.append(text)
            continue
        while at < len(lines) and not _FRICTION_HEAD.match(lines[at]):
            item = _LIST_ITEM.match(lines[at])
            if not item or (bulleted and len(item.group(1)) <= len(indent)):
                break
            text, at = _joined(item.group(2), lines, at + 1)
            found.append(text)
    return [text for text in found if not _is_none(text)]


def _journal_records(journal, tickets, latest, previous):
    records = []
    body = str(journal.get("body") or "")
    if not body.startswith(JOURNAL_MARKER):
        return records
    for friction in friction_lines(body):
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


# A ticket names the journal it was noticed in (docs/BRAIN.md, Ticket
# issues): "Noticed in <journal comment link>: "<the Friction line>"".
_NOTICED = re.compile(r"^[ \t>*-]*Noticed in\b[ \t]*:?(.*)$", re.I | re.M)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)|<?(https?://[^\s>)]+)>?|(\S*issuecomment-\d+)")


def _names_journal(text, journal_url, journal_id):
    """True when text links the journal: its URL, or its comment id as
    issuecomment-ID, never a longer id that starts with it."""
    return bool((journal_url and re.search(re.escape(journal_url) + r"(?!\d)", text))
                or (journal_id and re.search(r"issuecomment-" + re.escape(journal_id) + r"\b", text)))


def _ticket_refs_journal(tickets, journal, friction=""):
    """The ticket noticed in this journal. A "Noticed in" line naming the
    journal that quotes a friction matches only that friction; one that
    quotes nothing, or a bare link or Refs line, matches the journal."""
    journal_url = journal.get("html_url") or ""
    journal_id = str(journal.get("id") or "")
    friction_clean = _clean_text(friction)
    for row in tickets:
        ticket_body = str(row.get("body") or "")
        noticed = [line for line in _NOTICED.findall(ticket_body)
                   if _names_journal(line, journal_url, journal_id)]
        if noticed:
            quotes = [_clean_text(_LINK.sub(" ", line)) for line in noticed]
            if any(len(quote.split()) < 3 or _why_quotes_friction(quote, friction_clean)
                   for quote in quotes):
                return row
            continue
        if _names_journal(ticket_body, journal_url, journal_id):
            return row
        if journal_id and re.search(r"Refs\b[^\n]*\b" + re.escape(journal_id) + r"\b", ticket_body):
            return row
    return None


def _why_quotes_friction(why_clean, friction_clean):
    if not why_clean:
        return False  # an empty Why is in every string, so it quotes nothing
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
            or _ticket_refs_journal(tickets, journal, friction)
            or _ticket_quoting_friction(tickets, friction)
            or _find_ticket(tickets, REF.findall(body))
            or _ticket_for_journal_ids(tickets, journal))


def _moved_metric(ticket, state, latest, previous):
    if not ticket or state not in ("shipped", "merged", "verified"):
        return None
    return next((name for name, value in latest.items()
                 if name in previous and value["value"] != previous[name]["value"]), None)


# The loop's four stages, by index: noticed in a journal, ticketed, shipped,
# and moved a factory number.
LOOP_STAGES = (("noticed", "in a tick journal"), ("ticketed", "on loom#30 or grug#1143"),
               ("shipped", "live on the factory"), ("moved", "a factory number changed"))


def _improvement_stage(ticket, state, moved):
    if moved:
        return 3
    if ticket and state in ("shipped", "merged", "verified"):
        return 2
    return 1 if ticket else 0


def footer_models(prs):
    """Each ticket's model from its newest lane pull request footer, as
    {"owner/name#N": model}: one pass over the pull requests. Matching every
    ticket against every pull request cost 618,472 regex searches, about 5 s
    of CPU, on each page after each refresh (2026-10-15)."""
    newest = {}
    for pr in prs:
        parsed = footer(pr.get("body"))
        if not parsed:
            continue
        key, when = "%s#%s" % (parsed[0], parsed[1]), str(pr.get("updated_at") or "")
        if key not in newest or when >= newest[key][0]:
            newest[key] = (when, parsed[3])
    return {key: model for key, (_, model) in newest.items()}


def footer_runs(prs):
    """Each ticket's newest lane pull request footer as {"owner/name#N":
    (opened_at, model, pull request number, pull request URL)}. The runner
    writes the footer when it opens the pull request and never rewrites it
    on a fix push, so the opening time dates the run that model made."""
    newest = {}
    for pr in prs:
        parsed = footer(pr.get("body"))
        if not parsed:
            continue
        key = "%s#%s" % (parsed[0], parsed[1])
        when = str(pr.get("created_at") or pr.get("updated_at") or "")
        if key not in newest or when >= newest[key][0]:
            newest[key] = (when, parsed[3], pr.get("number"), pr.get("html_url"))
    return newest


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


def _short_ref(repo, number):
    return "%s#%s" % (str(repo).split("/")[-1], number)


def _improvement_card(row):
    esc = html.escape
    ticket = row["ticket"]
    ref = row["journal"].get("html_url") or ""
    journal = '<a href="%s">journal</a>' % esc(ref, True) if ref else "journal"
    ticket_html = ('<a href="%s">%s %s</a>' % (_ticket_href(ticket["repo"], ticket["number"]),
                   esc(_short_ref(ticket["repo"], ticket["number"])), esc(str(ticket.get("title") or "ticket")))
                   if ticket else "not ticketed yet")
    moved = ('<p class="moved good">moved: %s</p>' % esc(row["moved"])) if row["moved"] else ""
    segments = "".join('<span class="%s"></span>' % (
        ("moved" if index == 3 else "done") if index <= row["stage"] else "")
        for index in range(4))
    state = str(row["state"])
    return ('<article class="improvement"><div class="loop-steps">%s</div>'
            '<p class="friction">"%s"</p>'
            '<div class="improvement-meta"><span>%s -&gt; %s</span><span class="state-%s">%s</span></div>%s</article>') % (
        segments, esc(row["friction"]), journal, ticket_html,
        esc(state.replace(" ", "-")), esc(state), moved)


# Each daily number reduced to the value a person reads first, and what it
# counts. The metrics comment line carries every detail; a card shows one.
def _first(pattern, value):
    found = re.search(pattern, value)
    return found.group(1) if found else None


def _flow(value):
    median, over = _first(r"median ([\d.]+h)", value), _first(r"(\d+) PRs? >6h", value)
    return median and (median, "%s PR over 6h" % over if over else "median briefed to merged")


def _spread(value):
    ratio, idle = _first(r"Anthropic (\d+/\d+)", value), _first(r"idle (\d+/\d+)", value)
    if not ratio:
        return None
    used, total = (int(x) for x in ratio.split("/"))
    share = "%d%%" % round(100 * used / total) if total else ratio
    return share, "Anthropic share; idle %s passes" % idle if idle else "Anthropic share"


def _quality(value):
    parts = [p.strip() for p in value.split(";") if p.strip()]
    worst = next((p for p in parts if not p.startswith("0 ")), parts[0] if parts else "")
    number = re.match(r"(\d+)\s+(.*)", worst)
    return (number.group(1), number.group(2)) if number else None


def _visibility(value):
    ages = [float(age) for age in re.findall(r"(-?[\d.]+)h", value)]
    return ("%.1fh" % max(ages), "oldest goal report") if ages else ("missing", "no goal report")


def _counted(pattern, label):
    return lambda value: (lambda n: n and (n, label))(_first(pattern, value))


_SUMMARIES = {
    "Flow": _flow,
    "Throughput": _counted(r"verified (\d+/\d+)", "tickets verified"),
    "Spread": _spread,
    "Escalation": _counted(r"(\d+) tickets? failed twice", "failed twice on one model"),
    "Quality": _quality,
    "Autonomy": _counted(r"(\d+) asks? opened", "asks opened"),
    "Visibility": _visibility,
    "Self-repair": _counted(r"(\d+) un-ticketed", "un-ticketed factory faults"),
    "Generic": _counted(r"(\d+) files?", "files naming one product"),
}


def metric_summary(name, value):
    """(value, what it counts) for one daily number, never the raw line."""
    value = str(value or "")
    summary = _SUMMARIES.get(name)
    found = summary(value) if summary else None
    if found:
        return found
    first = re.match(r"\s*(\S+)", value)
    return (first.group(1)[:24] if first else "missing"), ""


def _factory_card(name, current, old):
    esc = html.escape
    value, counts = metric_summary(name, current["value"])
    if old is None:
        was, direction = "previous value missing", "no earlier day"
    else:
        was = "was %s" % metric_summary(name, old["value"])[0]
        # Better or worse only when the target flips; a number that held its
        # side of the target says so, never "better".
        direction = ("better" if current["passed"] and not old["passed"] else
                     "worse" if old["passed"] and not current["passed"] else
                     "still passing" if current["passed"] else "still failing")
    tone = "good" if current["passed"] else "bad"
    return ('<article class="card factory-card"><span class="metric-val">%s</span>'
            '<strong class="metric-name">%s</strong><span class="metric-counts">%s</span>'
            '<span class="metric-prev %s">%s / %s</span></article>') % (
        esc(value), esc(name), esc(counts), tone, esc(was), esc(direction))


def render_improving(data):
    records = data["records"]
    parts = ['<p class="page-lede">Loom is a goal too (loom#30). Each tick\'s journal notes what slowed '
             'the factory down; the biggest becomes a ticket, and it counts only when a factory number moves.</p>',
             '<div class="loop-stages">']
    for index, (name, sub) in enumerate(LOOP_STAGES):
        count = sum(1 for row in records if row["stage"] >= index)
        parts.append('<div class="loop-stage%s"><span class="loop-count">%d</span><span class="loop-name">%s</span>'
                     '<small>%s</small></div>' % (" moved" if index == 3 else "", count, name, sub))
    parts.append('</div><section><h2 class="label">Noticed to moved</h2><div class="improvement-list">')
    if not records:
        parts.append('<p class="empty-state">No friction lines in the journals yet.</p>')
    parts.extend(_improvement_card(row) for row in records)
    parts.append('</div></section><section><h2 class="label">The nine factory numbers, vs the day before</h2>')
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


def _lane_card(lane):
    esc = html.escape
    ticket = lane.get("ticket")
    state = "busy" if ticket else ("free" if lane["available"] else "held")
    heartbeat = format_heartbeat(lane.get("heartbeat"))
    if ticket:
        line = '<a href="%s">%s %s</a> - %s' % (
            _ticket_href(ticket["repo"], ticket["number"]), esc(_short_ref(ticket["repo"], ticket["number"])),
            esc(str(ticket.get("title") or "ticket")), esc(heartbeat))
    elif lane.get("reason"):
        line = "%s - %s" % (esc(lane["reason"]), esc(heartbeat))
    else:
        line = "waiting for work - %s" % esc(heartbeat)
    quota = lane.get("quota")
    if quota is None:
        quota_html = '<div class="quota-row"><span class="quota-label">quota missing</span></div>'
    else:
        pct = max(0, min(100, int(quota)))
        quota_html = ('<div class="quota-row"><div class="quota%s"><span style="width:%d%%"></span></div>'
                      '<span class="quota-label">quota %d%% used</span></div>') % (" low" if pct >= 75 else "", pct, pct)
    return ('<article class="card lane-card lane-%s"><div class="lane-head"><i class="lane-dot"></i>'
            '<strong>%s</strong><span class="lane-state">%s</span></div>'
            '<span class="lane-model">%s</span><p>%s</p>%s</article>') % (
        state, esc(lane["lane"]), state, esc(lane.get("model") or "model missing"), line, quota_html)


def _tier_card(tier, models):
    esc = html.escape
    most = max((sum(c[k] for k in ("merged", "open", "closed")) for c in models.values()), default=0) or 1
    rows = []
    for model, counts in sorted(models.items(), key=lambda item: -sum(item[1][k] for k in ("merged", "open", "closed"))):
        total = counts["merged"] + counts["open"] + counts["closed"]
        bars = "".join('<span class="bar-%s" style="width:%.1f%%"></span>' % (key, 100.0 * counts[key] / most)
                       for key in ("merged", "open", "closed"))
        rows.append('<div class="model-row"><div><span class="model-name">%s</span> <span class="lane-model">%s</span>'
                    '<span class="model-label">%d PRs, %d merged / %d open / %d closed</span></div>'
                    '<div class="stack">%s</div></div>' % (esc(model), esc(counts["lane"]), total,
                                                          counts["merged"], counts["open"], counts["closed"], bars))
    return '<section class="card tier-card"><h3 class="label">%s</h3>%s</section>' % (esc(tier.title()), "".join(rows))


def _dashboard_zone():
    name = os.environ.get("LOOM_DASHBOARD_TZ", "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc


def _local_time(stamp):
    moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("timezone required")
    local = moment.astimezone(_dashboard_zone())
    return local.strftime("%Y-%m-%d %H:%M ") + (local.tzname() or "UTC")


def _factory_tables():
    tiers = ['<table class="factory-tiers"><thead><tr><th>Tier</th><th>Lanes</th></tr></thead><tbody>']
    for name, lanes in factory.TIERS.items():
        tiers.append('<tr class="factory-tier"><th>%s</th><td>%s</td></tr>' %
                     (html.escape(name.title()), html.escape(", ".join(lanes))))
    tiers.append("</tbody></table>")
    ladder = ['<table class="model-ladder"><thead><tr><th>Lanes</th><th>Model</th></tr></thead><tbody>']
    for lanes, model in factory.LADDER:
        ladder.append("<tr><td>%s</td><td>%s</td></tr>" %
                      (html.escape(", ".join(lanes)), html.escape(model)))
    ladder.append("</tbody></table>")
    return "".join(tiers + ladder)


def render_lane_usage(rows):
    """Render one compact state row for every configured factory lane."""
    esc = html.escape
    names = set(lane for lanes in factory.TIERS.values() for lane in lanes)
    names.update(lane for lanes, _ in factory.LADDER for lane in lanes)
    holds = {}
    for row in rows:
        if isinstance(row.get("holds"), dict):
            holds.update(row["holds"])
    parts = ['<table class="lane-usage"><thead><tr><th>Lane</th><th>State</th></tr></thead><tbody>']
    for lane in sorted(names):
        reason = str(holds.get(lane) or "")
        if not reason:
            state = "available"
        elif any(word in reason.lower() for word in ("endpoint", "503", "unreachable", "down")):
            state = "endpoint down: " + reason
        else:
            state = re.sub(r"^limit:\s*", "", reason)
            match = re.search(r";\s*(?:lane waits|unavailable) until (\S+)", state)
            if match:
                try:
                    state = state[:match.start()] + " until " + _local_time(match.group(1).rstrip(";"))
                except ValueError:
                    state = state[:match.start()] + " until " + match.group(1)
        parts.append("<tr><th>%s</th><td>%s</td></tr>" % (esc(lane), esc(state)))
    parts.append("</tbody></table>")
    return "".join(parts)


def render_lanes(data):
    parts = ['<section><h2 class="label">Factory tiers</h2>%s</section>' % _factory_tables(),
             '<p class="page-lede">At most half of runs on Anthropic.</p>'
             '<section><h2 class="label">Anthropic share of runs</h2>']
    share = data.get("anthropic_share")
    if share is None:
        parts.append('<p class="empty-state">Share is missing: no lane run totals were available.</p>')
    else:
        parts.append('<div class="share-track"><span style="width:%d%%"></span><i></i></div><p class="machine-fact">%d%% of lane runs / cap 50%%</p>' % (min(100, share), share))
    parts.append('</section><section><h2 class="label">Lanes</h2><div class="lane-grid">')
    if not data.get("lanes"):
        parts.append('<p class="empty-state">Lane status is missing: no loom:lanes comment was found.</p>')
    parts.extend(_lane_card(lane) for lane in data.get("lanes", []))
    parts.append('</div></section><section><h2 class="label">Which model opened each lane PR</h2>'
                 '<p class="legend"><span class="bar-merged"></span> merged <span class="bar-open"></span> open '
                 '<span class="bar-closed"></span> closed</p><div class="tier-grid">')
    if not data.get("scoreboard"):
        parts.append('<p class="empty-state">Scoreboard is missing: no lane pull requests matched the runner footer.</p>')
    parts.extend(_tier_card(tier, models) for tier, models in data.get("scoreboard", {}).items())
    parts.append("</div></section>")
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
            absolute = _local_time(reset)
            parts.append('<p>Resets at %s (%s)</p>' % (esc(absolute), esc(relative)))
        except ValueError:
            parts.append('<p>Reset time unavailable.</p>')
    else:
        parts.append('<p>Reset time unavailable.</p>')
    parts.append('<p>Source: %s</p><p>Reading: %s%s</p></article>' %
                 (esc(str(row.get("source", ""))), age, ' <strong>Stale</strong>' if stale else ""))
    return "".join(parts)
