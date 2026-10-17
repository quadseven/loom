"""Ticket path, transcript, and pull request rendering (docs/design/floor,
Ticket). Every value comes from the ticket row, its stored runs, and what
dashboard/ticket_data.py read from GitHub; a part that is missing says why."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode


STATES = ("queued", "briefed", "running", "pr", "merged", "shipped", "verified")
# The path keeps its first and last steps when a ticket has many rounds.
PATH_MAX = 14
RESULT_LINES = 12
RUNNER_LINES = 8
CLAIM_RE = re.compile(r"^Claimed for lane (\S+)")
OPENED_RE = re.compile(r"^Lane (\S+) (opened|pushed fixes to) a pull request: \S*/pull/(\d+)")
FAILED_RE = re.compile(r"^Lane (\S+) failed: (.+)", re.S)
RUNG_RE = re.compile(r"<!-- loom:rung (\d+) -->")
MOVED_RE = re.compile(r"^Not a failure \([^)]*\): the factory moved this ticket to lane (\S+?)[,.]")
SHIPPED_RE = re.compile(r"^Shipped\b")
PROOF_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
BAD_LINE = re.compile(r"FAIL|Error|failed|Traceback")
GOOD_LINE = re.compile(r"passed|^OK\b|\bOK$")
EDIT_TOOLS = ("edit", "multiedit", "write", "apply_patch", "edit_file", "write_file", "str_replace")
NEXT = {
    "queued": "Next: a free lane of its tier claims it.",
    "briefed": "Next: its lane claims it.",
    "running": "Next: the lane commits and the runner opens or updates the pull request.",
    "pr": "Next: checks and the review pass on the head, then the factory merges.",
    "merged": "Next: the change ships to the live system.",
    "shipped": "Next: the brain looks for the live proof.",
    "failed": "Next: the ladder climbs or the brain rules on it.",
}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _hm(ts: Any) -> str:
    """HH:MM UTC of an ISO time, or "" when there is none."""
    text = str(ts or "")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return ""


def _short(repo: str, number: Any) -> str:
    return "%s#%s" % (str(repo).split("/")[-1], number)


def checks(body):
    found = []
    for line in str(body or "").splitlines():
        match = re.match(r"\s*-\s*\[([ xX])\]\s*(.+)", line)
        if match:
            found.append({"text": match.group(2).strip(), "checked": match.group(1).lower() == "x"})
    return found[:3]


# -- path -----------------------------------------------------------------

def _comment_step(comment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    body = str(comment.get("body") or "").strip()
    ts = comment.get("created_at") or ""
    rung = RUNG_RE.search(body)
    if rung:
        model = re.search(r"^Model:\s*(\S+)", body, re.M)
        return {"who": "brain", "ts": ts, "text": "Climbed the model ladder to rung %s." % rung.group(1),
                "meta": model.group(1) if model else ""}
    for pattern, build in (
            (CLAIM_RE, lambda m: {"who": "lane", "text": "Lane %s claimed it." % m.group(1)}),
            (OPENED_RE, lambda m: {"who": "lane", "text": "Lane %s %s PR #%s." % (
                m.group(1), "opened" if m.group(2) == "opened" else "pushed fixes to", m.group(3))}),
            (FAILED_RE, lambda m: {"who": "fail", "text": "Lane %s failed: %s" % (
                m.group(1), " ".join(m.group(2).split())[:180])}),
            (MOVED_RE, lambda m: {"who": "brain", "text": "Moved to lane %s; it never ran there." % m.group(1)}),
            (SHIPPED_RE, lambda m: {"who": "brain", "text": " ".join(body.split())[:180]})):
        match = pattern.match(body)
        if match:
            return dict(build(match), ts=ts, meta="")
    if body.startswith("<!-- loom:fix"):
        threads = len(re.findall(r"^### Thread ", body, re.M))
        found = len(re.findall(r"^### Check: ", body, re.M))
        parts = [p for p in ("%d review thread%s" % (threads, "" if threads == 1 else "s") if threads else "",
                             "%d failing check%s" % (found, "" if found == 1 else "s") if found else "") if p]
        return {"who": "grug", "ts": ts, "meta": "",
                "text": "Sent back for a fix round: %s." % (" and ".join(parts) or "findings to answer")}
    if PROOF_RE.search(body) and '"criteria"' in body:
        return {"who": "ok", "ts": ts, "meta": "", "text": "Verified live with a proof record."}
    return None


def _label_step(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event.get("event") != "labeled":
        return None
    name = str((event.get("label") or {}).get("name") or "")
    words = {"loom:queued": ("brain", "Queued from its goal."),
             "loom:merged": ("brain", "Merged."),
             "loom:verified": ("ok", "Marked verified."),
             "loom:failed": ("fail", "Marked failed.")}
    if name not in words:
        return None
    who, text = words[name]
    return {"who": who, "ts": event.get("created_at") or "", "text": text, "meta": ""}


def path_steps(record: Dict[str, Any], comments: List[Dict[str, Any]],
               events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ticket's path, oldest first, from its comments and label events,
    ending in the next step (none once verified)."""
    steps = [s for s in (_comment_step(c) for c in comments) if s]
    steps += [s for s in (_label_step(e) for e in events) if s]
    steps.sort(key=lambda s: str(s.get("ts") or ""))
    if len(steps) > PATH_MAX:
        hidden = len(steps) - PATH_MAX + 1
        keep = PATH_MAX // 3
        steps = steps[:keep] + [{"who": "lane", "ts": "", "meta": "",
                                 "text": "%d more steps of fix rounds." % hidden}] + steps[keep + hidden:]
    nxt = NEXT.get(str(record.get("state") or ""))
    if nxt:
        steps.append({"who": "next", "ts": "", "text": nxt, "meta": ""})
    return steps


# -- transcript -------------------------------------------------------------

def interleave_activity(events, review_threads):
    """Transcript events and review threads in time order; an event with no
    time of its own (a runner log) goes after everything timed."""
    rows = [dict(event, activity_type="transcript", _i=i) for i, event in enumerate(events)]
    for thread in review_threads:
        rows.append(dict(thread, activity_type="review", _i=len(rows)))
        answer = thread.get("answer")
        if answer:
            rows.append({"ts": thread.get("answer_ts") or thread.get("ts", ""), "text": answer,
                         "lane": thread.get("lane"), "activity_type": "answer", "_i": len(rows)})
    rows.sort(key=lambda row: (not row.get("ts"), str(row.get("ts") or ""), row["_i"]))
    return [{k: v for k, v in row.items() if k != "_i"} for row in rows]


def _tool_fields(lines: List[str]) -> Dict[str, str]:
    """A tool call's inputs: bus/transcripts.py writes each as "key: value",
    and a multi-line value runs on over the following lines."""
    fields: Dict[str, str] = {}
    key = None
    for line in lines:
        match = re.match(r"^([a-z_]+): (.*)$", line)
        if match:
            key = match.group(1)
            fields[key] = match.group(2)
        elif key is not None:
            fields[key] += "\n" + line
    return fields


def _tool_head(text: str) -> Dict[str, Any]:
    """A tool call's label (ran or edited), its head line, and edit lines."""
    lines = str(text).split("\n")
    tool = lines[0].split(":")[0].strip()
    fields = _tool_fields(lines[1:])
    if tool.lower() in EDIT_TOOLS or tool.lower().endswith("_file"):
        edits = [("- " + line, "bad") for line in fields.get("old_string", "").split("\n")[:20] if line.strip()]
        edits += [("+ " + line, "good") for line in
                  fields.get("new_string", fields.get("content", "")).split("\n")[:20] if line.strip()]
        for value in (fields.get("patch", ""), fields.get("input", "")):
            edits += [(line, "good" if line[0] == "+" else "bad") for line in value.split("\n")
                      if re.match(r"^[+-](?![+-])", line)]
        patched = re.search(r"\*\*\* (?:Update|Add|Delete) File: (\S+)", text)
        target = (fields.get("file_path") or fields.get("path") or fields.get("file")
                  or (patched.group(1) if patched else tool))
        return {"label": "edited", "head": target, "lines": edits[:40]}
    command = fields.get("command") or fields.get("cmd") or ""
    inner = re.search(r'cmd:\s*"((?:[^"\\]|\\.)*)"', command)
    if inner:
        try:
            command = json.loads('"%s"' % inner.group(1))
        except ValueError:
            command = inner.group(1)
    if not command:
        command = " ".join(line for line in lines[1:] if line.strip()) or tool
    return {"label": "ran", "head": "$ " + command.strip(), "lines": []}


def _result_lines(text: str) -> str:
    lines = [line for line in str(text).split("\n") if line.strip()]
    tone = lambda line: "bad" if BAD_LINE.search(line) else "good" if GOOD_LINE.search(line) else ""
    def render(items):
        return "".join('<span class="ln %s">%s</span>' % (tone(line), _esc(line)) for line in items)
    whole = "bad" if any(BAD_LINE.search(line) for line in lines) else (
        "good" if any(GOOD_LINE.search(line) for line in lines) else "")
    if len(lines) <= RESULT_LINES:
        return '<pre class="tool-result %s">%s</pre>' % (whole, render(lines))
    return ('<pre class="tool-result %s">%s</pre><details class="more"><summary>%d earlier lines</summary>'
            '<pre class="tool-result">%s</pre></details>') % (
                whole, render(lines[-RESULT_LINES:]), len(lines) - RESULT_LINES, render(lines[:-RESULT_LINES]))


def _row(label: str, ts: Any, inner: str, cls: str = "", extra: str = "") -> str:
    return ('<div class="transcript-event %s"%s><div class="tx-head"><span class="tx-label">%s</span>'
            '<span class="tx-at">%s</span></div>%s</div>') % (cls, extra, _esc(label), _esc(_hm(ts)), inner)


def _event_type(event: Dict[str, Any]) -> str:
    """Which renderer an event takes."""
    if event.get("activity_type") in ("review", "answer"):
        return event["activity_type"]
    role, kind = event.get("role", ""), event.get("kind", "")
    if role == "user":
        return "brief"
    if role == "thinking" or kind == "thinking":
        return "thought"
    if kind in ("tool_use", "tool_result"):
        return kind
    if role == "runner" or kind == "log":
        return "runner"
    return "said"


def _review_html(event: Dict[str, Any]) -> str:
    return _row("grug", event.get("ts"), '<blockquote class="review-thread">%s / %s<br>%s</blockquote>' % (
        _esc(event.get("severity") or "severity missing"),
        _esc("resolved" if event.get("resolved") else "open"), _esc(event.get("text"))), "grug")


def _brief_html(event: Dict[str, Any]) -> str:
    text = str(event.get("text", ""))
    first = next((line for line in text.split("\n") if line.strip()), "Brief")
    return _row("brief", event.get("ts"), '<details class="transcript-brief"><summary>%s <span>show the full brief</span>'
                                          '</summary><pre>%s</pre></details>' % (_esc(first), _esc(text)), "brief")


def _tool_html(event: Dict[str, Any], result: Optional[Dict[str, Any]]) -> str:
    tool = _tool_head(str(event.get("text", "")))
    body = '<pre class="tool-head">%s</pre>' % _esc(tool["head"])
    body += "".join('<span class="ln %s">%s</span>' % (tone, _esc(line)) for line, tone in tool["lines"])
    if result is not None:
        body += _result_lines(result.get("text", ""))
    return _row(tool["label"], event.get("ts"), '<div class="tool-block">%s</div>' % body, tool["label"])


def _runner_html(event: Dict[str, Any]) -> str:
    lines = [line for line in str(event.get("text", "")).split("\n") if line.strip()]
    inner = '<pre class="runner">%s</pre>' % _esc("\n".join(lines[-RUNNER_LINES:]))
    if len(lines) > RUNNER_LINES:
        inner += ('<details class="more"><summary>show the full runner log (%d lines)</summary>'
                  '<pre class="runner">%s</pre></details>') % (len(lines), _esc("\n".join(lines)))
    return _row("runner", event.get("ts"), inner, "runner")


SIMPLE = {
    "review": _review_html,
    "answer": lambda e: _row("answered", e.get("ts"), '<blockquote class="review-answer">%s</blockquote>'
                             % _esc(e.get("text")), "answered"),
    "brief": _brief_html,
    "thought": lambda e: _row("thought", e.get("ts"), "<em>%s</em>" % _esc(e.get("text")), "thought", " data-thought"),
    "tool_result": lambda e: _row("output", e.get("ts"), '<div class="tool-block">%s</div>'
                                  % _result_lines(e.get("text", "")), "output"),
    "runner": _runner_html,
    "said": lambda e: _row("said", e.get("ts"), "<p>%s</p>" % _esc(e.get("text")), "said"),
}


def _event_rows(rows: List[Dict[str, Any]]) -> List[str]:
    """Each event as its block; a tool call takes the output that follows it."""
    out = []
    i = 0
    while i < len(rows):
        kind = _event_type(rows[i])
        if kind == "tool_use":
            result = rows[i + 1] if i + 1 < len(rows) and _event_type(rows[i + 1]) == "tool_result" else None
            out.append(_tool_html(rows[i], result))
            i += 2 if result is not None else 1
            continue
        out.append(SIMPLE[kind](rows[i]))
        i += 1
    return out


def _run_line(run: Optional[Dict[str, Any]]) -> str:
    if not run:
        return "No run yet. Waiting for a free lane."
    started = _hm(run.get("started_at"))
    return "attempt %s / %s / %s / %s%s" % (run.get("attempt"), run.get("slot") or "slot missing",
                                           run.get("model") or "model missing", run.get("outcome") or "outcome missing",
                                           " / started %s UTC" % started if started else "")


def _chip(run: Dict[str, Any], selected: Optional[Dict[str, Any]]) -> str:
    outcome = str(run.get("outcome") or "")
    tone = "done" if outcome == "done" else "failed" if outcome == "failed" else "other"
    current = selected is not None and run.get("id") == selected.get("id")
    href = "?" + urlencode({"run": run.get("attempt"), "tab": "transcript"})
    return '<a class="attempt-chip %s%s" href="%s"%s title="attempt %s, %s">%s</a>' % (
        tone, " selected" if current else "", _esc(href), ' aria-current="true"' if current else "",
        _esc(run.get("attempt")), _esc(outcome or "outcome missing"), _esc(run.get("attempt")))


# -- panes ----------------------------------------------------------------

STATE_TONE = {"queued": "dim", "briefed": "body", "running": "accent", "pr": "accent", "merged": "good",
              "shipped": "info", "verified": "good", "failed": "bad"}


def _parentage(epic, goal):
    """" / epic Y / goal X": where the ticket sits (decision 0003)."""
    out = ""
    for word, ref in (("epic", epic), ("goal", goal)):
        if ref and "#" in str(ref):
            out += " / %s %s" % (word, _esc(_short(*str(ref).split("#"))))
    return out


def _header(record, runs):
    repo, number = record.get("repo", ""), record.get("number", "")
    state = str(record.get("state") or "queued")
    flag = state if len(runs) <= 1 else "attempt %d / %s" % (len(runs), state)
    goal = record.get("goal")
    meta = [p for p in ("%s / %s" % (record.get("lane"), record.get("model") or "model missing") if record.get("lane") else "",
                        record.get("difficulty") or "", "%d attempt%s" % (len(runs), "" if len(runs) == 1 else "s"),
                        "rung %s" % record["rung"] if record.get("rung") else "") if p]
    current_index = STATES.index(state) if state in STATES else 3
    strip = []
    for i, name in enumerate(STATES):
        cls = "done" if i < current_index else ("current failed" if state == "failed" else "current") if i == current_index else ""
        strip.append('<div class="strip-step %s"><span></span><b>%s</b></div>' % (cls, _esc(name)))
    return ('<div class="ticket-header"><div class="ticket-topline"><a href="/tickets">&larr; tickets</a>'
            '<span class="tone-%s">%s</span></div><div class="ticket-ids">%s%s</div><h2>%s</h2>'
            '<p class="machine-fact">%s</p><div class="state-strip" aria-label="Ticket states">%s</div></div>') % (
                STATE_TONE.get(state, "dim"), _esc(flag), _esc(_short(repo, number)),
                _parentage(record.get("epic"), goal),
                _esc(record.get("title")), _esc(" / ".join(meta)), "".join(strip))


def _path_pane(record, detail):
    found = checks(record.get("body"))
    parts = ['<section class="ticket-pane path-pane" data-pane="path"><div class="card checks-card">'
             '<div class="label">%d checks</div>' % len(found)]
    for item in found:
        parts.append('<div class="check %s"><span>%s</span><span>%s</span></div>' % (
            "checked" if item["checked"] else "unchecked", "&#10003;" if item["checked"] else "&#9675;", _esc(item["text"])))
    if not found:
        parts.append('<p class="empty-state">Acceptance checks missing: the ticket body has no checklist.</p>')
    parts.append('</div><div class="label">Path</div>')
    if detail is None:
        parts.append('<p class="empty-state">Path not looked: GitHub is not reachable from the dashboard.</p>')
    else:
        missing = [detail["errors"][k] for k in ("comments", "label events") if k in detail.get("errors", {})]
        steps = path_steps(record, detail.get("comments") or [], detail.get("events") or [])
        parts.append('<ol class="ticket-timeline">')
        for step in steps:
            parts.append('<li class="who-%s"><span class="dot"></span><div><div class="step-head"><span>%s</span><span>%s</span></div>'
                         '<p>%s</p>%s</div></li>' % (
                             _esc(step["who"]), _esc("clean" if step["who"] == "ok" else step["who"]),
                             _esc(_hm(step.get("ts"))), _esc(step["text"]),
                             '<div class="step-meta">%s</div>' % _esc(step["meta"]) if step.get("meta") else ""))
        parts.append('</ol>')
        parts.extend('<p class="empty-state">%s</p>' % _esc(reason) for reason in missing)
    parts.append('</section>')
    return "".join(parts)


def _transcript_pane(runs, selected, events, review_threads, running):
    parts = ['<section class="ticket-pane transcript-pane" data-pane="transcript">']
    if len(runs) > 1:
        parts.append('<div class="attempts">%s</div>' % "".join(_chip(run, selected) for run in runs))
    parts.append('<div class="run-line"><span>%s</span><label class="thought-toggle"><input type="checkbox" checked data-thought-toggle> thoughts</label></div>'
                 % _esc(_run_line(selected)))
    parts.append('<div class="run-events" data-attempt="%s" data-running="%s">' % (
        _esc((selected or {}).get("attempt") or ""), "true" if running else "false"))
    if selected is None:
        parts.append('<p class="empty-state">No transcript yet: no attempt has finished and been stored for this ticket.</p>')
    elif not events:
        parts.append('<p class="empty-state">Transcript missing: no events were stored for this attempt.</p>')
    elif all(event.get("role") == "runner" or event.get("kind") == "log" for event in events):
        parts.append('<p class="empty-state">This attempt kept only the runner log: the lane\'s own session record was not captured.</p>')
    parts.extend(_event_rows(interleave_activity(events, review_threads)))
    if running:
        parts.append(_row("live", "", '<p class="live-row"><span class="dot"></span>working... streaming from %s</p>'
                          % _esc((selected or {}).get("slot") or "its lane"), "live"))
    parts.append('</div></section>')
    return "".join(parts)


def _summary(runs: List[Dict[str, Any]], comments: List[Dict[str, Any]]) -> str:
    if len(runs) < 2:
        return ""
    done = sum(1 for run in runs if run.get("outcome") == "done")
    failed = sum(1 for run in runs if run.get("outcome") == "failed")
    reasons: Dict[str, int] = {}
    for comment in comments:
        match = FAILED_RE.match(str(comment.get("body") or "").strip())
        if match:
            reason = re.sub(r"\[\w+\]\s*", "", match.group(2)).split(":")[0].strip()[:60]
            reasons[reason] = reasons.get(reason, 0) + 1
    text = "%d attempts: %d done, %d failed." % (len(runs), done, failed)
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
        text += " What the failures had in common: %s." % "; ".join("%s (%d)" % kv for kv in top)
    return text


def _proof(record: Dict[str, Any], comments: List[Dict[str, Any]]) -> str:
    found = checks(record.get("body"))
    if record.get("state") == "verified":
        record_text = ""
        for comment in reversed(comments):
            match = PROOF_RE.search(str(comment.get("body") or ""))
            if match:
                try:
                    measure = json.loads(match.group(1)).get("measure") or {}
                    record_text = "%s: %s" % (measure.get("name", "measure"), measure.get("after", ""))
                except ValueError:
                    pass
                break
        return ('<div class="card proof good"><div class="label">Proof / seen live</div><p>%s</p></div>'
                % _esc(record_text or "Verified with a proof record on the ticket."))
    live = found[-1]["text"] if found else "the ticket's live criterion"
    return '<div class="card proof waiting"><div class="label">Proof / waiting</div><p>%s</p></div>' % _esc(live)


def _pr_pane(record, runs, detail):
    parts = ['<section class="ticket-pane pr-pane" data-pane="pr">']
    pr = (detail or {}).get("pull_request")
    errors = (detail or {}).get("errors", {})
    if detail is None:
        parts.append('<p class="empty-state">Pull request not looked: GitHub is not reachable from the dashboard.</p>')
    elif pr:
        parts.append('<div class="label"><a href="%s">PR #%s</a> / %s / checks</div>' % (
            _esc(pr.get("html_url")), _esc(pr.get("number")), _esc(pr.get("state"))))
        for check in pr.get("checks") or []:
            tone = "good" if check.get("state") in ("success", "neutral", "skipped") else (
                "bad" if check.get("state") in ("failure", "timed_out", "cancelled", "action_required") else "accent")
            parts.append('<div class="pr-line"><span>%s</span><span class="tone-%s">%s</span></div>' % (
                _esc(check.get("name")), tone, _esc(check.get("state"))))
        if not pr.get("checks"):
            parts.append('<p class="empty-state">%s</p>' % _esc(errors.get("checks", "No checks ran on the head.")))
        parts.append('<div class="label">Files</div>')
        for item in pr.get("files") or []:
            parts.append('<div class="pr-line file"><span>%s</span><span class="tone-good">+%s</span><span class="tone-bad">-%s</span></div>' % (
                _esc(item.get("name")), _esc(item.get("add")), _esc(item.get("del"))))
        parts.append('<div class="label">Review threads</div>')
        threads = pr.get("threads") or []
        for thread in threads:
            parts.append('<div class="pr-thread"><span class="tone-%s">%s / %s</span><p>%s</p></div>' % (
                "good" if thread.get("resolved") else "bad", _esc(thread.get("severity") or "severity missing"),
                "resolved" if thread.get("resolved") else "open", _esc(thread.get("text"))))
        if not threads:
            parts.append('<p class="empty-state">%s</p>' % _esc(errors.get("review threads", "No review threads.")))
    elif "comments" in errors:
        parts.append('<p class="empty-state">%s</p>' % _esc(errors["comments"]))
    elif "pull request" in errors:
        parts.append('<p class="empty-state">%s</p>' % _esc(errors["pull request"]))
    else:
        parts.append('<p class="empty-state">No pull request yet. The runner opens one when the lane\'s commit passes the validation contract.</p>')
    summary = _summary(runs, (detail or {}).get("comments") or [])
    if summary:
        parts.append('<div class="label">Across all attempts</div><p>%s</p>' % _esc(summary))
    parts.append(_proof(record, (detail or {}).get("comments") or []))
    parts.append('</section>')
    return "".join(parts)


def render(record, runs, selected, events, pull_requests=(), review_threads=(), detail=None):
    """The Ticket page body. `selected` is the chosen run (a run dict, or its
    id or attempt number); `detail` is what ticket_data read from GitHub."""
    if not isinstance(selected, dict):
        selected = next((run for run in runs if str(run.get("id")) == str(selected)
                         or str(run.get("attempt")) == str(selected)), runs[-1] if runs else None)
    threads = list(review_threads) or list(((detail or {}).get("pull_request") or {}).get("threads") or [])
    last = bool(runs) and selected is not None and selected.get("id") == runs[-1].get("id")
    parts = [_header(record, runs)]
    parts.append('<nav class="ticket-segments" aria-label="Ticket panes">' + ''.join(
        '<a href="?tab=%s" data-ticket-tab="%s">%s</a>' % (tab, tab, tab) for tab in ("transcript", "path", "pr")) + '</nav>')
    parts.append('<div class="ticket-columns">')
    parts.append(_path_pane(record, detail))
    parts.append(_transcript_pane(runs, selected, events, threads if last else [], record.get("state") == "running"))
    parts.append(_pr_pane(record, runs, detail))
    parts.append('</div>')
    return ''.join(parts)
