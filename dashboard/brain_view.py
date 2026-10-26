"""Data builders and HTML for the /brain view: the tick in progress, its
step notes, the wait ranking, handoff markers and recent ticks."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from bus.brain import TICK_STEPS as STEPS
from bus.metrics import TERMINAL_STATES
from .github import journal_next

JOURNAL_MARKER = "<!-- loom:journal v1 -->"
TICK_RE = re.compile(r"^Tick (\S+), (\S+)\s*$", re.M)
# Brains write the tick header loosely ("Brain tick ..., holder muse.",
# "Tick 2026-10-15 03:05Z, muse", "Brain tick ... (muse)"); the holder is
# read from the first of these that matches the header line.
HOLDER_RES = (re.compile(r"\bholder[ =]([A-Za-z][A-Za-z0-9_-]*)"),
              re.compile(r"^(?:Brain )?[Tt]ick\b.*,\s*([a-z][a-z0-9-]*)\.?\s*$"),
              re.compile(r"\(([a-z][a-z0-9-]*)\)"))
BULLET_RE = re.compile(r"^- ([A-Za-z][A-Za-z ]*?): (.+)$", re.M)
STEP_TROUBLE_RE = re.compile(r"^- (Step \S+: (failed|degraded)\b.*)$", re.M)
# Handoff lines are HTML comments (docs/BRAIN.md, Journal); shown verbatim.
MARKER_RE = re.compile(r"^<!-- loom:(?:defer|done) v1 .*-->$", re.M)
# A step note's tag comes from its own words; the first match wins, so a
# regression that shipped reads as a regression.
TAGS = (("regression", re.compile(r"\bregress", re.I)),
        ("operator", re.compile(r"\boperator\b|\bdiscord\b", re.I)),
        ("merged", re.compile(r"\bmerged\b", re.I)),
        ("verifying", re.compile(r"\bverif|\bproof\b", re.I)),
        ("deferred", re.compile(r"\bdefer", re.I)))
# A ticket in a terminal state is done, not waiting, so it is never ranked:
# the same states the Floor and Hot spots bottleneck leaves out.
RANKED_OUT = TERMINAL_STATES
# What a count of tickets in each state is waiting for, for the ranking.
WAITING = {"queued": "tickets waiting for a lane", "briefed": "briefs waiting for a lane to start",
           "running": "runs still going", "pr": "pull requests waiting to merge",
           "merged": "merged tickets waiting to ship", "shipped": "shipped tickets waiting for live proof",
           "failed": "failed tickets waiting for a retry"}
# The next move's headline is its first sentence, cut at this many characters.
HEADLINE_MAX = 160
# A first sentence longer than this is cut at its first clause break instead.
HEADLINE_SOFT = 100
RECENT_TICKS = 8


def _when(value) -> str:
    text = str(value or "")
    match = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)", text)
    return "%s %sZ" % match.groups() if match else text or "time missing"


def _progress(record):
    """[(step, done|current|ahead)] for the recorded step, or None."""
    step = (record or {}).get("step")
    if step not in STEPS:
        return None
    # The unlock step ends the tick, so every step is done.
    index = len(STEPS) if step == "unlock" else STEPS.index(step)
    return [(name, "done" if i < index else "current" if i == index else "ahead")
            for i, name in enumerate(STEPS)]


def _tag(text: str):
    return next((tag for tag, pattern in TAGS if pattern.search(text)), None)


def _thoughts(record):
    return [{"ts": note.get("ts"), "step": note.get("step"), "text": str(note.get("text") or ""),
             "tag": _tag(str(note.get("text") or ""))}
            for note in (record or {}).get("notes") or [] if isinstance(note, dict)]


def _holder(body: str):
    lines = body.split("\n")
    header = next((line.strip("# ").strip() for line in lines[1:3] if line.strip()), "")
    for pattern in HOLDER_RES:
        match = pattern.search(header)
        if match:
            return match.group(1)
    return None


def _tick(journal):
    body = str(journal.get("body") or "")
    header = TICK_RE.search(body)
    trouble = STEP_TROUBLE_RE.findall(body)
    outcome = ("failed" if any(kind == "failed" for _, kind in trouble)
               else "degraded" if trouble else "ok")
    lines = [line for line, _ in trouble] or [
        "%s: %s" % (key, value.strip()) for key, value in BULLET_RE.findall(body)
        if key != "Cost" and not key.startswith("Step ")
        and value.strip().strip(".").lower() != "none"]
    return {"at": header.group(1) if header else journal.get("created_at"),
            "holder": (header.group(2) if header else _holder(body)) or "holder missing",
            "outcome": outcome, "line": lines[0] if lines else "nothing changed"}


def rank_waits(rows, error):
    """Ticket states ranked by tickets affected times their median wait."""
    items = []
    for row in rows or []:
        median, count = row.get("median_h"), row.get("n") or 0
        if median is None or not count or row.get("state") in RANKED_OUT:
            continue
        lost = count * float(median)
        plural = "" if count == 1 else "s"
        items.append({"state": row["state"], "lost": lost,
                      "text": "%d %s" % (count, WAITING.get(row["state"], "ticket%s in %s" % (plural, row["state"]))),
                      "why": "%d ticket%s x %.1f h median wait = %.0f ticket-hours lost" % (
                          count, plural, float(median), lost),
                      "score": "%.0fh" % lost})
    items.sort(key=lambda item: -item["lost"])
    reason = "waits are missing: %s" % error if error else "no state has wait samples yet"
    return {"items": items[:3], "reason": reason}


def split_next(text):
    """(headline, rest) of a journal's next move: its first sentence, then the
    reason that follows. A long first sentence is cut at a word boundary."""
    text = " ".join(str(text or "").split())
    match = re.match(r"(.+?[.!?])(\s+|$)", text)
    head = match.group(1) if match else text
    if len(head) > HEADLINE_SOFT:
        clause = re.match(r"(.{20,%d}?)[;:](\s|$)" % HEADLINE_MAX, head)
        if clause:
            return clause.group(1).rstrip() + ".", text[len(clause.group(0)):].strip()
    if len(head) > HEADLINE_MAX:
        head = head[:HEADLINE_MAX].rsplit(" ", 1)[0] + " ..."
        return head, text
    return head, text[len(head):].strip()


def build_brain(record, error, journals, wait_rows=(), waits_error=""):
    """Everything /brain shows, computed from the step record and journals."""
    ordered, seen = [], set()
    # One tick journals to every goal home it touched: keep each body once.
    for journal in sorted((j for j in journals or [] if str(j.get("body") or "").startswith(JOURNAL_MARKER)),
                          key=lambda j: str(j.get("created_at") or ""), reverse=True):
        body = str(journal.get("body") or "")
        if body not in seen:
            seen.add(body)
            ordered.append(journal)
    newest = str(ordered[0].get("body") or "") if ordered else ""
    return {"progress": _progress(record),
            "reason": error or "no step record on the lock branch",
            "holder": (record or {}).get("holder"), "started_at": (record or {}).get("started_at"),
            "thoughts": _thoughts(record), "next": journal_next(newest) if ordered else None,
            "journal_reason": "" if ordered else "no goal journal found",
            "markers": MARKER_RE.findall(newest),
            "ticks": [_tick(j) for j in ordered[:RECENT_TICKS]],
            "ranking": rank_waits(wait_rows, waits_error)}


def _journal_holders(journals):
    holders = {}
    for journal in journals or []:
        if not str(journal.get("body") or "").startswith(JOURNAL_MARKER):
            continue
        tick = _tick(journal)
        holder = _brain_name(tick["holder"])
        if holder != "holder missing" and (holder not in holders or
                                             str(tick["at"] or "") > str(holders[holder]["at"] or "")):
            holders[holder] = tick
    return holders


def _brain_name(holder):
    names = {"operator-session": "operator session", "operator": "operator session",
             "mastermind": "operator session", "lane-machine": "lane machine",
             "local-brain": "lane machine", "local": "lane machine"}
    return names.get(str(holder or "").lower(), str(holder or ""))


def _brain_state(holder, record, tick, now):
    if record and record.get("holder") == holder and record.get("step"):
        return "running", record.get("step")
    if tick:
        try:
            stamp = datetime.fromisoformat(str(tick.get("at")).replace("Z", "+00:00"))
            return ("off" if (now - stamp).total_seconds() > 86400 else "paused"), tick.get("line")
        except (ValueError, TypeError):
            return "paused", tick.get("line")
    return "off", "no recent step recorded"


def _age(value, now):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        seconds = max(0, int((now - stamp).total_seconds()))
    except (ValueError, TypeError):
        return "time unavailable"
    if seconds < 3600:
        return "%d minutes ago" % (seconds // 60)
    if seconds < 86400:
        return "%d hours ago" % (seconds // 3600)
    return "%d days ago" % (seconds // 86400)


def build_brains(record, journals, lanes, tickets, pull_requests, comments=(), now=None):
    """Build the read-only brain, lane and hand-off map from dashboard snapshot data."""
    now = now or datetime.now(timezone.utc)
    holders = _journal_holders(journals)
    for holder in ("operator session", "muse", "lane machine"):
        holders.setdefault(holder, None)
    if record and record.get("holder"):
        holders.setdefault(_brain_name(record["holder"]), None)
    brains = []
    for holder, tick in sorted(holders.items()):
        record_holder = _brain_name((record or {}).get("holder"))
        state, step = _brain_state(holder, record if record_holder == holder else None, tick, now)
        brains.append({"holder": holder, "state": state, "step": step,
                       "at": (record or {}).get("started_at") if state == "running" else (tick or {}).get("at"),
                       "age": _age((record or {}).get("started_at") if state == "running"
                                   else (tick or {}).get("at"), now)})
    ticket_by_lane = {}
    for ticket in tickets or []:
        if ticket.get("lane") and ticket.get("state") in ("running", "pr"):
            ticket_by_lane[ticket["lane"]] = ticket
    claim_times = _claim_times(comments)
    lane_cards = []
    for lane in lanes or []:
        name = lane.get("lane")
        ticket = ticket_by_lane.get(name)
        lane_cards.append({"lane": name, "holder": "muse" if name == "muse" else "lane machine",
                           "heartbeat": lane.get("heartbeat"), "ticket": ticket,
                           "age": _age(lane.get("heartbeat"), now),
                           "run_started": claim_times.get(_ticket_key(ticket)),
                           "claim": next((label for label in (ticket or {}).get("labels", [])
                                          if str(label).startswith("claim:")), None)})
    prs = []
    for pr in pull_requests or []:
        parsed = _lane_pr_ticket(pr)
        if parsed:
            prs.append((parsed, pr))
    for lane in lane_cards:
        ticket = lane["ticket"]
        lane["pull_request"] = next((pr for ref, pr in prs if ticket and ref ==
                                      (ticket.get("repo"), str(ticket.get("number")))), None)
    return {"brains": brains, "lanes": lane_cards, "now": now}


def _ticket_key(ticket):
    if not ticket:
        return None
    return str(ticket.get("repo")) + "#" + str(ticket.get("number"))


def _claim_times(comments):
    claims = {}
    for comment in comments or []:
        body = str(comment.get("body") or "")
        match = re.match(r"Claimed for lane (\S+)", body)
        issue = re.search(r"/issues/(\d+)$", str(comment.get("issue_url") or ""))
        if match and issue and comment.get("repo"):
            key = str(comment["repo"]) + "#" + issue.group(1)
            claims[key] = comment.get("created_at")
    return claims


def _lane_pr_ticket(pr):
    match = re.search(r"^Loom ticket ([A-Za-z0-9-]+)[./]([A-Za-z0-9_.-]+?)(?:#|-)(\d+)",
                      str(pr.get("body") or ""), re.M)
    return (match.group(1) + "/" + match.group(2), match.group(3)) if match else None


def render_brains(data):
    """Render the current brain and lane snapshot without page-time reads."""
    esc = html.escape
    brains = "".join('<article class="card"><h2>%s</h2><p>%s</p><p>%s</p></article>' % (
        esc(brain["holder"]), esc(brain["state"]),
        esc((brain.get("step") or "step unavailable") + " / last posted " + brain["age"]))
        for brain in data.get("brains", [])) or '<p class="empty-state">No brain ticks have been recorded.</p>'
    lanes = []
    for lane in data.get("lanes", []):
        ticket = lane.get("ticket")
        if ticket:
            href = "/t/%s/%s" % (esc(str(ticket.get("repo") or "")), esc(str(ticket.get("number") or "")))
            doing = '<a href="%s">%s</a>' % (href, esc(ticket.get("title") or "ticket"))
            if lane.get("run_started"):
                doing += ' / run started %s' % esc(lane["run_started"])
        else:
            doing = "idle"
        if lane.get("claim"):
            doing += " / claimed by " + esc(lane["claim"].partition(":")[2])
        pr = lane.get("pull_request")
        pr_link = (' <a href="%s">open PR</a> / brain review' %
                   esc(str(pr.get("html_url") or "#"))) if pr else ""
        lanes.append('<article class="card"><h3>%s</h3><p>%s / %s</p><p>%s%s</p><p>%s</p></article>' % (
            esc(lane.get("lane") or "lane"), esc(lane.get("holder") or "brain"),
            esc("busy" if ticket else "idle"), doing, pr_link,
            esc("last heartbeat " + lane["age"])))
    lane_html = "".join(lanes) or '<p class="empty-state">No lane heartbeats have been recorded.</p>'
    incidents = [ticket for lane in data.get("lanes", []) for ticket in [lane.get("ticket")]
                 if ticket and "needs-brain" in (ticket.get("labels") or [])]
    incident_html = "".join('<p><a href="/t/%s/%s">%s</a> held for brain review</p>' % (
        esc(str(ticket.get("repo") or "")), esc(str(ticket.get("number") or "")),
        esc(ticket.get("title") or "ticket")) for ticket in incidents)
    if not incident_html:
        incident_html = '<p class="empty-state">No factory incident holds are recorded.</p>'
    return ('<p class="page-lede">Brains, their lanes and the work each lane holds now.</p>'
            '<section class="section"><h2 class="label">Brains</h2><div class="lane-grid">%s</div></section>'
            '<section class="section"><h2 class="label">Lanes and hand-offs</h2><div class="lane-grid">%s</div></section>'
            '<section class="section"><h2 class="label">Factory incident holds</h2>%s</section>') % (
                brains, lane_html, incident_html)


TAG_LABELS = {None: "note"}


def _hhmm(value) -> str:
    match = re.search(r"T(\d\d:\d\d)", str(value or ""))
    return match.group(1) if match else _when(value)


def _render_progress(data):
    esc = html.escape
    if data["progress"] is None:
        return '<p class="empty-state">Tick progress not recorded: %s.</p>' % esc(data["reason"])
    steps = "".join('<li class="tick-step %s"%s><span class="tick-bar"></span>'
                    '<span class="tick-name">%s</span><span class="visually-hidden">%s</span></li>' % (
        status, ' aria-current="step"' if status == "current" else "", esc(name), status)
        for name, status in data["progress"])
    return '<ol class="tick-steps" aria-label="Tick steps">%s</ol>' % steps


def _lock_line(data):
    if data["progress"] is None:
        return ""
    return '<p class="brain-lock machine-fact">%s holds the lock / started %s</p>' % (
        html.escape(str(data["holder"] or "holder missing")), html.escape(_hhmm(data["started_at"])))


def _render_next(data):
    esc = html.escape
    if not data["next"]:
        body = '<p class="empty-state">Next move not recorded: %s.</p>' % esc(
            data["journal_reason"] or "the newest journal has no Next line")
    else:
        line, why = split_next(data["next"])
        body = '<p class="next-line">%s</p>%s' % (esc(line), '<p class="next-why">%s</p>' % esc(why) if why else "")
    return '<section class="next-move"><h2 class="label">Next move</h2>%s</section>' % body


def _render_thoughts(data):
    esc = html.escape
    if data["progress"] is None:
        inner = '<p class="empty-state">Step notes not recorded: %s.</p>' % esc(data["reason"])
    elif not data["thoughts"]:
        inner = '<p class="empty-state">No step notes were written this tick.</p>'
    else:
        inner = '<ul class="thoughts">%s</ul>' % "".join(
            '<li><p class="machine-fact"><span class="tag tag-%s">%s</span> <span class="when">%s / %s</span></p>'
            '<p>%s</p></li>' % (
                esc(t["tag"] or "none"), esc(t["tag"] or TAG_LABELS[None]), esc(_hhmm(t["ts"])),
                esc(str(t["step"] or "step missing")), esc(t["text"]))
            for t in data["thoughts"])
    return '<section class="brain-thoughts"><h2 class="label">Thinking, this tick</h2>%s</section>' % inner


def render_ranking(ranking):
    esc = html.escape
    if not ranking["items"]:
        return '<p class="empty-state">Ranking not computed: %s.</p>' % esc(ranking["reason"])
    return '<ol class="ranking">%s</ol>' % "".join(
        '<li%s><span class="rank machine-fact">%d</span><div><p>%s</p><p class="why machine-fact">%s</p></div>'
        '<span class="score machine-fact">%s</span></li>' % (
            ' class="top"' if i == 0 else "", i + 1, esc(item.get("text") or item["state"]),
            esc(item["why"]), esc(item.get("score") or ""))
        for i, item in enumerate(ranking["items"]))


def _render_markers(data):
    if data["journal_reason"]:
        inner = '<p class="empty-state">Handoff markers are missing: %s.</p>' % html.escape(data["journal_reason"])
    elif not data["markers"]:
        inner = '<p class="empty-state">The newest journal has no handoff markers.</p>'
    else:
        inner = '<pre class="markers">%s</pre>' % html.escape("\n".join(data["markers"]))
    return '<section><h2 class="label">Handoff markers</h2>%s</section>' % inner


def _render_ticks(data):
    esc = html.escape
    if not data["ticks"]:
        inner = '<p class="empty-state">Recent ticks are missing: %s.</p>' % esc(data["journal_reason"])
    else:
        inner = '<ol class="recent-ticks">%s</ol>' % "".join(
            '<li><span class="at machine-fact">%s</span><div><p class="head machine-fact outcome-%s">%s / %s</p>'
            '<p>%s</p></div></li>' % (
                esc(_hhmm(t["at"])), esc(t["outcome"]), esc(t["holder"]), esc(t["outcome"]), esc(t["line"]))
            for t in data["ticks"])
    return '<section><h2 class="label">Recent ticks</h2>%s</section>' % inner


def render_brain(data):
    ranking = ('<section class="card ranking-card"><h2 class="label">Ranked by tickets affected x hours lost</h2>'
               '%s</section>' % render_ranking(data["ranking"]))
    main = _lock_line(data) + _render_progress(data) + _render_next(data) + _render_thoughts(data)
    side = ranking + _render_markers(data) + _render_ticks(data)
    return '<div class="brain-layout"><div class="brain-main">%s</div><div class="brain-side">%s</div></div>' % (
        main, side)
