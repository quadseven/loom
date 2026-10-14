"""Ticket path, transcript, and pull request rendering."""
from __future__ import annotations

import html
import re
from datetime import datetime


STATES = ("queued", "briefed", "running", "pr", "merged", "shipped", "verified")


def _esc(value):
    return html.escape(str(value or ""), quote=True)


def checks(body):
    found = []
    for line in str(body or "").splitlines():
        match = re.match(r"\s*-\s*\[([ xX])\]\s*(.+)", line)
        if match:
            found.append({"text": match.group(2).strip(), "checked": match.group(1).lower() == "x"})
    return found[:3]


def _event_html(event):
    role, kind, text = event.get("role", ""), event.get("kind", ""), str(event.get("text", ""))
    if event.get("activity_type") == "review":
        return '<article class="review-thread"><b>grug / %s / %s</b><blockquote>%s</blockquote></article>' % (
            _esc(event.get("severity") or "severity missing"), _esc("resolved" if event.get("resolved") else "open"), _esc(text))
    if event.get("activity_type") == "answer":
        return '<article class="review-answer"><b>answered / %s</b><p>%s</p></article>' % (_esc(event.get("lane") or "lane missing"), _esc(text))
    if role == "user":
        first, rest = (text.split("\n", 1) + [""])[:2]
        return '<details class="transcript-brief"><summary><b>brief</b> %s <span>show the full brief</span></summary><pre>%s</pre></details>' % (_esc(first), _esc(text))
    if role == "thinking" or kind == "thinking":
        return '<div class="transcript-event thought" data-thought><b>thought</b><em>%s</em></div>' % _esc(text)
    if kind == "tool_use":
        label = "edited" if event.get("file") or re.search(r"(?:edit|write)_file", text) else "ran"
        return '<div class="transcript-event tool-block"><b>%s</b><pre>%s</pre>' % (label, _esc(text))
    if kind == "tool_result":
        tone = "bad" if re.search(r"FAIL|Error|failed", text, re.I) else "good" if re.search(r"passed|OK", text, re.I) else ""
        return '<pre class="tool-result %s">%s</pre></div>' % (tone, _esc(text))
    if role == "runner" or kind == "log":
        return '<div class="transcript-event runner"><b>runner</b><pre>%s</pre></div>' % _esc(text)
    return '<div class="transcript-event said"><b>said</b><p>%s</p></div>' % _esc(text)


def interleave_activity(events, review_threads):
    rows = [dict(event, activity_type="transcript") for event in events]
    for thread in review_threads:
        rows.append(dict(thread, activity_type="review"))
        answer = thread.get("answer")
        if answer:
            rows.append({"ts": thread.get("answer_ts") or thread.get("ts", ""), "text": answer,
                         "lane": thread.get("lane"), "activity_type": "answer"})
    return sorted(rows, key=lambda row: str(row.get("ts") or ""))


def render(record, runs, selected, events, pull_requests=(), review_threads=()):
    esc = _esc
    repo, number = record.get("repo", ""), record.get("number", "")
    body = str(record.get("body") or "")
    parts = ['<div class="ticket-header"><div class="label">%s#%s / %s</div><h2>%s</h2><p class="machine-fact">%s / %s / %s / %d attempts / rung %s</p><div class="progress" aria-label="Ticket states">' % (
        esc(repo), esc(number), esc(record.get("goal") or "goal missing"), esc(record.get("title")), esc(record.get("lane") or "lane missing"), esc(record.get("model") or "model missing"), esc(record.get("difficulty") or "difficulty missing"), len(runs), esc(record.get("rung") if record.get("rung") is not None else "missing"))]
    current = record.get("state") or "queued"
    current_index = STATES.index(current) if current in STATES else 0
    for i, state in enumerate(STATES):
        cls = "done" if i < current_index else "current" if i == current_index else ""
        parts.append('<span class="%s" title="%s"></span>' % (cls, esc(state)))
    parts.append('</div><div class="state-words">%s</div></div>' % " / ".join(esc(s) for s in STATES))
    parts.append('<nav class="ticket-segments" aria-label="Ticket panes">' + ''.join('<a href="?tab=%s" data-ticket-tab="%s">%s</a>' % (tab, tab, label) for tab, label in (("transcript", "Transcript"), ("path", "Path"), ("pr", "PR"))) + '</nav>')
    parts.append('<div class="ticket-columns"><section class="ticket-pane path-pane" data-pane="path"><h3>Path</h3><ul class="acceptance">')
    for item in checks(body):
        parts.append('<li class="%s">%s %s</li>' % ("checked" if item["checked"] else "unchecked", "&#10003;" if item["checked"] else "&#9633;", esc(item["text"])))
    if not checks(body): parts.append('<li class="missing">Acceptance checks missing: ticket body has no checklist.</li>')
    parts.append('</ul><p class="empty-state">Path timeline missing: the dashboard snapshot has no label events, ticket comments, or handoff markers.</p><ol class="ticket-timeline"><li>Next step <span>missing: no next-step source is available</span></li></ol></section>')
    parts.append('<section class="ticket-pane transcript-pane" data-pane="transcript"><h3>Transcript</h3><div class="attempts">')
    for run in runs:
        parts.append('<a class="attempt-chip" href="?run=%s&tab=transcript">Attempt %s</a>' % (esc(run.get("id")), esc(run.get("attempt"))))
    parts.append('</div><label><input type="checkbox" data-thought-toggle> Show thoughts</label>')
    parts.append('<div class="run-events" data-run="%s" data-running="%s">' % (esc(selected or ""), "true" if current == "running" else "false"))
    if not events: parts.append('<p class="empty-state">Transcript missing: no events were stored for this attempt.</p>')
    for event in interleave_activity(events, review_threads): parts.append(_event_html(event))
    parts.append('</div></section><section class="ticket-pane pr-pane" data-pane="pr"><h3>Pull request</h3>')
    if not pull_requests: parts.append('<p class="empty-state">Pull request data missing: no linked pull request was found.</p>')
    else:
        for pr in pull_requests: parts.append('<article class="card"><a href="%s">%s</a><p>%s</p></article>' % (esc(pr.get("html_url")), esc(pr.get("title")), esc(pr.get("state"))))
    if not review_threads: parts.append('<p class="empty-state">Review threads missing: the GitHub snapshot does not include review comments.</p>')
    parts.append('<h3>Across all attempts</h3><p>Missing: attempt outcome summary is unavailable.</p><article class="card">Proof<br>Missing: no live check was found.</article></section></div>')
    return ''.join(parts)
