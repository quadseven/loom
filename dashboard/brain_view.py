"""The Brain view: tick progress, step notes, handoff markers and recent ticks.

Everything shown is read from the tick step record (bus.brain step --show) and
the journal comments. Nothing is summarised by a model; a source that is not
there is shown as missing with its reason.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional

JOURNAL_MARKER = "<!-- loom:journal v1 -->"
HANDOFF_RE = re.compile(r"<!-- loom:(?:defer|done) v1 [^>]*-->")
TICK_RE = re.compile(r"^Tick (\S+), (\S+)\s*$", re.M)
STEP_FAULT_RE = re.compile(r"^- Step ([a-z]+): (failed|degraded)\b", re.M)
# First match wins, so a regression note that also says "merged" stays a regression.
TAGS = (("regression", r"regress"), ("operator", r"operator|discord"),
        ("merged", r"\bmerged\b"), ("verifying", r"verif"), ("deferred", r"defer"))
MAX_TICKS = 8


def tag_note(text: str) -> Optional[str]:
    for tag, pattern in TAGS:
        if re.search(pattern, text, re.I):
            return tag
    return None


def _newest_first(comments: List[Dict[str, Any]], marker: str) -> List[Dict[str, Any]]:
    found = [c for c in comments if str(c.get("body") or "").startswith(marker)]
    return sorted(found, key=lambda c: str(c.get("created_at") or ""), reverse=True)


def _field(body: str, name: str) -> Optional[str]:
    match = re.search(r"^- %s: (.*)$" % re.escape(name), body, re.M)
    return match.group(1).strip() if match else None


def recent_ticks(comments: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    ticks = []
    for comment in _newest_first(comments, JOURNAL_MARKER)[:MAX_TICKS]:
        body = str(comment.get("body") or "")
        head = TICK_RE.search(body)
        faults = STEP_FAULT_RE.findall(body)
        outcome = ("failed" if any(kind == "failed" for _, kind in faults)
                   else "degraded" if faults else "ok")
        said = ["%s %s" % (name.lower(), value) for name in ("Shipped", "Briefed")
                for value in [_field(body, name)] if value and value.lower() != "none"]
        ticks.append({"at": head.group(1) if head else str(comment.get("created_at") or "time missing"),
                      "holder": head.group(2) if head else "holder missing",
                      "outcome": outcome,
                      "line": "; ".join(said) or "nothing shipped or briefed",
                      "url": str(comment.get("html_url") or "")})
    return ticks


def build_brain(record: Optional[Dict[str, Any]], record_error: Optional[str],
                journals: List[Dict[str, Any]], reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Join the step record and the journals into what the view shows."""
    step_index = (record or {}).get("step_index")
    recorded = isinstance(step_index, int) and bool(record and record.get("step"))
    newest = _newest_first(journals, JOURNAL_MARKER)
    handoffs = HANDOFF_RE.findall(str(newest[0].get("body") or "")) if newest else None
    latest_report = next(iter(_newest_first(reports, "<!-- loom:report v1 -->")), None)
    next_move = _field(str(latest_report.get("body") or ""), "Next") if latest_report else None
    notes = [{**note, "tag": tag_note(str(note.get("text") or ""))}
             for note in (record or {}).get("notes") or []] if recorded else []
    return {"recorded": recorded, "record": record, "record_error": record_error,
            "steps": list((record or {}).get("steps") or []),
            "step_index": step_index if recorded else None,
            "notes": notes, "handoffs": handoffs, "next_move": next_move,
            "ticks": recent_ticks(journals)}


def _missing(what: str, reason: str) -> str:
    return '<p class="empty-state">%s: missing. %s</p>' % (html.escape(what), html.escape(reason))


def _progress(data: Dict[str, Any]) -> str:
    if not data["recorded"]:
        reason = data["record_error"] or (
            "No step is recorded on the lock, so no tick is running or its record was never written.")
        return ('<section class="card"><h2>Tick progress</h2><p><strong>not recorded</strong></p>'
                '<p class="muted">%s</p></section>' % html.escape(reason))
    index, steps = data["step_index"], data["steps"]
    cells, words = [], []
    for position, name in enumerate(steps, 1):
        status = "done" if position < index else "current" if position == index else "ahead"
        cells.append('<span class="%s" title="%s: %s"></span>' % (
            "" if status == "ahead" else status, html.escape(name), status))
        words.append('<li class="step-%s"><span class="machine-fact">%d. %s</span> %s</li>' % (
            status, position, html.escape(name), status))
    record = data["record"] or {}
    current = html.escape(steps[index - 1]) if 1 <= index <= len(steps) else "unknown"
    return ('<section class="card"><h2>Tick progress</h2><div class="progress ten" aria-label="Tick progress">%s</div>'
            '<p>Step %d of %d, <strong>%s</strong>. Holder %s, started %s.</p><ol class="step-list">%s</ol></section>' % (
                "".join(cells), index, len(steps), current,
                html.escape(str(record.get("holder") or "holder missing")),
                html.escape(str(record.get("started_at") or "start time missing")), "".join(words)))


def render_brain(data: Dict[str, Any]) -> str:
    esc = html.escape
    parts = [_progress(data)]
    parts.append('<section class="card"><h2>Next move</h2>%s</section>' % (
        "<p>%s</p>" % esc(data["next_move"]) if data["next_move"] else
        _missing("Next move", "No loom:report comment with a Next line was found.")))
    parts.append('<section class="card"><h2>Thinking, this tick</h2>')
    if not data["recorded"]:
        parts.append(_missing("Step notes", "There is no step record for this tick."))
    elif not data["notes"]:
        parts.append('<p class="muted">The brain has recorded no notes this tick.</p>')
    for note in data["notes"]:
        tag = note["tag"]
        chip = '<span class="tag tag-%s">%s</span>' % (tag, tag) if tag else ""
        parts.append('<p class="thought"><span class="machine-fact">%s %s</span> %s %s</p>' % (
            esc(str(note.get("ts") or "")), esc(str(note.get("step") or "")), chip,
            esc(str(note.get("text") or ""))))
    parts.append('</section><section class="card"><h2>Ranking</h2>%s</section>' % _missing(
        "Ranking", "The brain does not record a ranking yet, so there is no members-times-hours arithmetic to show."))
    parts.append('<section class="card"><h2>Handoff markers</h2>')
    if data["handoffs"] is None:
        parts.append(_missing("Handoff markers", "No journal comment was found."))
    elif not data["handoffs"]:
        parts.append('<p class="muted">The newest journal carries no handoff markers.</p>')
    parts.extend('<pre class="marker"><code>%s</code></pre>' % esc(marker) for marker in data["handoffs"] or [])
    parts.append('</section><section class="card"><h2>Recent ticks</h2>')
    if not data["ticks"]:
        parts.append(_missing("Recent ticks", "No loom:journal comment was found."))
    for tick in data["ticks"]:
        label = esc(tick["at"])
        link = '<a href="%s">%s</a>' % (esc(tick["url"], True), label) if tick["url"] else label
        parts.append('<p class="tick-row"><span class="machine-fact">%s %s</span> <strong>%s</strong> %s</p>' % (
            link, esc(tick["holder"]), esc(tick["outcome"]), esc(tick["line"])))
    parts.append("</section>")
    return "".join(parts)
