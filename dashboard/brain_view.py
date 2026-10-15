"""Data builders and HTML for the /brain view: the tick in progress, its
step notes, the wait ranking, handoff markers and recent ticks."""
from __future__ import annotations

import html
import re

from bus.brain import TICK_STEPS as STEPS
from .github import journal_next

JOURNAL_MARKER = "<!-- loom:journal v1 -->"
TICK_RE = re.compile(r"^Tick (\S+), (\S+)\s*$", re.M)
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
# A ticket sitting in verified is done, not waiting, so it is never ranked.
RANKED_OUT = ("verified",)
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
            "holder": header.group(2) if header else "holder missing",
            "outcome": outcome, "line": lines[0] if lines else "nothing changed"}


def rank_waits(rows, error):
    """Ticket states ranked by tickets affected times their median wait."""
    items = []
    for row in rows or []:
        median, count = row.get("median_h"), row.get("n") or 0
        if median is None or not count or row.get("state") in RANKED_OUT:
            continue
        lost = count * float(median)
        items.append({"state": row["state"], "lost": lost,
                      "why": "%d ticket%s x %.1fh median wait = %.1f ticket-hours lost" % (
                          count, "" if count == 1 else "s", float(median), lost)})
    items.sort(key=lambda item: -item["lost"])
    reason = "waits are missing: %s" % error if error else "no state has wait samples yet"
    return {"items": items[:3], "reason": reason}


def build_brain(record, error, journals, wait_rows=(), waits_error=""):
    """Everything /brain shows, computed from the step record and journals."""
    ordered = sorted((j for j in journals or [] if str(j.get("body") or "").startswith(JOURNAL_MARKER)),
                     key=lambda j: str(j.get("created_at") or ""), reverse=True)
    newest = str(ordered[0].get("body") or "") if ordered else ""
    return {"progress": _progress(record),
            "reason": error or "no step record on the lock branch",
            "holder": (record or {}).get("holder"), "started_at": (record or {}).get("started_at"),
            "thoughts": _thoughts(record), "next": journal_next(newest) if ordered else None,
            "journal_reason": "" if ordered else "no goal journal found",
            "markers": MARKER_RE.findall(newest),
            "ticks": [_tick(j) for j in ordered[:RECENT_TICKS]],
            "ranking": rank_waits(wait_rows, waits_error)}


def _render_progress(data):
    esc = html.escape
    if data["progress"] is None:
        return '<p class="empty-state">Tick progress not recorded: %s.</p>' % esc(data["reason"])
    steps = "".join('<li class="tick-step %s"%s><span>%d</span>%s <small>%s</small></li>' % (
        status, ' aria-current="step"' if status == "current" else "", i + 1, esc(name), status)
        for i, (name, status) in enumerate(data["progress"]))
    return '<p class="machine-fact">%s / started %s</p><ol class="tick-steps">%s</ol>' % (
        esc(str(data["holder"] or "holder missing")), esc(_when(data["started_at"])), steps)


def _render_thoughts(data):
    esc = html.escape
    if data["progress"] is None:
        return '<p class="empty-state">Step notes not recorded: %s.</p>' % esc(data["reason"])
    if not data["thoughts"]:
        return '<p class="empty-state">No step notes were written this tick.</p>'
    return '<ul class="thoughts">%s</ul>' % "".join(
        '<li><span class="tag tag-%s">%s</span> <span class="machine-fact">%s / %s</span> %s</li>' % (
            esc(t["tag"] or "none"), esc(t["tag"] or "note"), esc(_when(t["ts"])),
            esc(str(t["step"] or "step missing")), esc(t["text"]))
        for t in data["thoughts"])


def render_ranking(ranking):
    esc = html.escape
    if not ranking["items"]:
        return '<p class="empty-state">Ranking not computed: %s.</p>' % esc(ranking["reason"])
    return '<ol class="ranking">%s</ol>' % "".join(
        '<li><strong>%s</strong> <span>%s</span></li>' % (esc(item["state"]), esc(item["why"]))
        for item in ranking["items"])


def _render_markers(data):
    if data["journal_reason"]:
        return '<p class="empty-state">Handoff markers are missing: %s.</p>' % html.escape(data["journal_reason"])
    if not data["markers"]:
        return '<p class="empty-state">The newest journal has no handoff markers.</p>'
    return '<pre class="markers">%s</pre>' % html.escape("\n".join(data["markers"]))


def _render_ticks(data):
    esc = html.escape
    if not data["ticks"]:
        return '<p class="empty-state">Recent ticks are missing: %s.</p>' % esc(data["journal_reason"])
    return '<ol class="recent-ticks">%s</ol>' % "".join(
        '<li><span class="machine-fact">%s / %s</span> <strong class="outcome-%s">%s</strong> %s</li>' % (
            esc(_when(t["at"])), esc(t["holder"]), esc(t["outcome"]), esc(t["outcome"]), esc(t["line"]))
        for t in data["ticks"])


def render_brain(data):
    esc = html.escape
    next_move = (esc(data["next"]) if data["next"] else
                 '<span class="empty-state">Next move not recorded: %s.</span>' % esc(
                     data["journal_reason"] or "the newest journal has no Next line"))
    sections = (("Tick progress", _render_progress(data)),
                ("Next move", "<p>%s</p>" % next_move),
                ("Thinking, this tick", _render_thoughts(data)),
                ("Ranking: hours lost waiting", render_ranking(data["ranking"])),
                ("Handoff markers", _render_markers(data)),
                ("Recent ticks", _render_ticks(data)))
    return "".join('<section class="card brain-card"><h2>%s</h2>%s</section>' % (esc(title), part)
                   for title, part in sections)
