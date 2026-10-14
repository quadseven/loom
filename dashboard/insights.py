"""Data preparation and rendering for the Brain and Hot spots views."""
from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from bus import metrics
from bus.github import GitHubError

STEPS = ("lock", "status", "merge", "verify", "failed", "asks", "brief", "triage", "journal", "unlock")
TAG_COLORS = {"regression": "bad", "operator": "accent", "merged": "good", "verifying": "accent", "deferred": "muted"}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _home() -> Path:
    return Path(os.environ.get("LOOM_HOME", str(Path.home() / "loom")))


def _step_data(home: Path) -> dict:
    path = Path(os.environ.get("LOOM_BRAIN_STEP_FILE", str(home / "state" / "brain-step.json")))
    return _read_json(path)


def _journals(state) -> tuple[list[dict], str | None]:
    result = []
    errors = []
    homes = set()
    for _, rows in state.data.items():
        for row in rows or []:
            goal = row.get("goal")
            if goal:
                match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)", goal)
                if match:
                    homes.add((match.group(1), match.group(2)))
    for repo, number in sorted(homes):
        try:
            comments = state.gh.paginate("/repos/%s/issues/%s/comments?per_page=100" % (repo, number))
        except (GitHubError, OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        for comment in comments:
            if isinstance(comment, dict) and str(comment.get("body") or "").startswith("<!-- loom:journal v1 -->"):
                result.append({"repo": repo, "number": number, "body": comment.get("body", ""),
                               "at": comment.get("created_at", "")})
    return sorted(result, key=lambda item: item["at"], reverse=True), (errors[0] if errors else None)


def _journal_tick(journal: dict) -> dict:
    body = str(journal.get("body") or "")
    head = re.search(r"^Tick\s+([^,]+),\s*(.+)$", body, re.M)
    summary = next((line.split(":", 1)[1].strip() for line in body.splitlines()
                    if line.startswith(("- Shipped:", "- Waiting:", "- Asks:"))), "Journal recorded; no action summary.")
    asks = next((line.partition(":")[2].strip() for line in body.splitlines() if line.startswith("- Asks:")), "")
    return {"at": head.group(1) if head else journal.get("at", "unknown time"),
            "holder": head.group(2) if head else "unknown holder",
            "outcome": "needs operator" if asks and asks.lower() != "none" else "journaled",
            "line": summary}


def _journal_lines(journals: list[dict]) -> list[dict]:
    rows = []
    for journal in journals:
        body = journal["body"]
        for line in body.splitlines():
            match = re.match(r"^- (Regression|Operator|Merged|Verifying|Deferred):\s*(.*)$", line, re.I)
            if not match:
                match = re.match(r"^\[(Regression|Operator|Merged|Verifying|Deferred)\]\s*(.*)$", line, re.I)
            if match:
                rows.append({"tag": match.group(1).lower(), "text": match.group(2), "at": journal["at"]})
    return rows


def brain_data(state) -> dict:
    cached = getattr(state, "_brain_view_cache", None)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    home = _home()
    step = _step_data(home)
    journals, journal_error = _journals(state)
    ticks = []
    for path in sorted((home / "state").glob("brain*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        record = _read_json(path)
        if record:
            ticks.append(record)
    record = _read_json(home / "state" / "brain.json")
    if record and record not in ticks:
        ticks.insert(0, record)
    journal_ticks = [_journal_tick(item) for item in journals]
    if journal_ticks:
        ticks = journal_ticks
    result = {"step": step, "journals": journals, "journal_error": journal_error,
              "thoughts": _journal_lines(journals), "ticks": ticks[:6]}
    state._brain_view_cache = (time.monotonic(), result)
    return result


def progress_html(step: dict) -> str:
    current = step.get("current", step.get("step"))
    if isinstance(current, int) and 1 <= current <= len(STEPS):
        current = STEPS[current - 1]
    raw_done = step.get("done", step.get("completed", []))
    completed = set(raw_done) if isinstance(raw_done, (list, tuple, set)) else set(STEPS[:raw_done]) if isinstance(raw_done, int) else set()
    if not step or not isinstance(current, str):
        return '<p class="missing">Tick progress: not recorded. The brain step record is missing.</p>'
    nodes = []
    for name in STEPS:
        cls = "done" if name in completed else "current" if name == current else "ahead"
        nodes.append(f'<li class="step {cls}" aria-current="{"step" if cls == "current" else "false"}"><span>{esc(name)}</span></li>')
    return '<ol class="tick-steps" aria-label="Tick progress">' + "".join(nodes) + "</ol>"


def _rank_html(rows: list[dict]) -> str:
    if not rows:
        return '<p class="missing">Ranking data is missing from the step record.</p>'
    out = ['<ol class="ranking">']
    for i, row in enumerate(rows, 1):
        affected, hours = row.get("members_affected"), row.get("hours_lost")
        arithmetic = (f"{affected} members affected times {hours} hours lost" if affected is not None and hours is not None
                      else str(row.get("arithmetic") or "Ranking arithmetic not recorded"))
        out.append(f'<li><b>{i}</b><div><strong>{esc(row.get("text", row.get("ticket", "Ranked item")))}</strong><small>{esc(arithmetic)}</small></div></li>')
    return "".join(out) + "</ol>"


def brain_page(state) -> str:
    data = brain_data(state)
    step = data["step"]
    thoughts = data["thoughts"]
    if not thoughts:
        reason = f"Journal comments are unavailable: {data['journal_error']}" if data["journal_error"] else "No tagged notes were found in the available journal comments."
        thoughts_markup = f'<p class="missing">Journal thinking notes are missing. {esc(reason)}</p>'
    else:
        thoughts_markup = "".join(f'<article class="thought"><b class="{TAG_COLORS.get(row["tag"], "muted")}">{esc(row["tag"])}</b><time>{esc(row["at"])}</time><p>{esc(row["text"])}</p></article>' for row in thoughts)
    markers = step.get("markers", step.get("handoffs", [])) or []
    markers_markup = "".join(f'<code>{esc(marker)}</code>' for marker in markers) or '<p class="missing">Handoff markers are missing from the step record.</p>'
    ticks = "".join(f'<li><b>{esc(item.get("holder", "unknown holder"))}</b><time>{esc(item.get("at", item.get("started", "unknown time")))}</time><span>{esc(item.get("outcome", "unknown outcome"))}</span><p>{esc(item.get("line", item.get("summary", "No journal summary recorded.")))}</p></li>' for item in data["ticks"])
    next_move = step.get("next_move", step.get("next"))
    next_markup = f'<p>{esc(next_move)}</p>' if next_move else '<p class="missing">Next move is missing from the step record.</p>'
    return f'<div class="insight-grid"><section class="panel wide"><h2>Tick progress</h2>{progress_html(step)}<h2>Next move</h2>{next_markup}<h2>Thinking, this tick</h2>{thoughts_markup}</section><section class="panel"><h2>Ranking</h2><p class="muted">Ranked by members affected times hours lost.</p>{_rank_html(step.get("ranking") or [])}</section><section class="panel"><h2>Handoff markers</h2><div class="markers">{markers_markup}</div><h2>Recent ticks</h2><ul class="recent">{ticks or "<li class=missing>Recent tick records are missing.</li>"}</ul></section></div>'


def lane_failure_rates(comments: list[dict], now: float) -> list[dict]:
    """Compute per-lane failure percentages from station outcome comments."""
    since = now - 7 * 86400
    totals: dict[str, list[int]] = {}
    for comment in comments:
        at = metrics.stamp(comment.get("created_at"))
        if not at or at < since:
            continue
        body = str(comment.get("body") or "")
        lane = re.search(r"(?:lane|slot)[:= ]+([A-Za-z0-9_-]+)", body, re.I)
        outcome = re.search(r"outcome[:= ]+(failed|failure|done|ok|success)", body, re.I)
        if not lane or not outcome:
            continue
        key = lane.group(1)
        totals.setdefault(key, [0, 0])[0] += 1
        totals[key][1] += outcome.group(1).lower() in ("failed", "failure")
    return [{"lane": lane, "total": counts[0], "failed": counts[1],
             "rate": counts[1] / counts[0] if counts[0] else 0.0,
             "high": counts[1] / counts[0] > 0.2}
            for lane, counts in sorted(totals.items())]


def hotspots_data(state, now: float | None = None) -> dict:
    cached = getattr(state, "_hotspots_view_cache", None)
    if now is None and cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    now = time.time() if now is None else now
    repos = list(state.repos)
    try:
        waits, bottleneck = metrics.waits_for_repos(state.gh, repos, now)
        wait_error = None
    except (GitHubError, OSError, ValueError) as exc:
        waits, bottleneck, wait_error = [], None, str(exc)
    comments = []
    comment_errors = []
    for repo, rows in state.data.items():
        for row in rows or []:
            if not row.get("number"):
                continue
            try:
                found = state.gh.paginate("/repos/%s/issues/%s/comments?per_page=100" % (repo, row["number"]))
                comments.extend(c for c in found if isinstance(c, dict))
            except (GitHubError, OSError, ValueError) as exc:
                comment_errors.append(str(exc))
    result = {"waits": waits, "bottleneck": bottleneck, "wait_error": wait_error,
              "lane_rates": lane_failure_rates(comments, now), "findings": finding_rules(comments, now),
              "stuck": stuck_tickets(state, comments), "comments": comments,
              "comment_error": comment_errors[0] if comment_errors else None}
    state._hotspots_view_cache = (time.monotonic(), result)
    return result



def lane_failure_html(rows: list[dict]) -> str:
    if not rows:
        return '<p class="missing">Lane failure data is missing. No station outcome comments were found in the last 7 days.</p>'
    cards = []
    for row in rows[:5]:
        height = min(100, int(row["rate"] * 100))
        label = f'{row["failed"]}/{row["total"]} failed ({row["rate"]:.0%})'
        high = ' - high failure rate' if row["high"] else ''
        cards.append(f'<li class="{"high" if row["high"] else ""}"><b>{esc(row["lane"])}</b><span class="rate-bar"><i style="height:{height}%"></i></span><span>{esc(label)}{high}</span></li>')
    return '<ul class="lane-rates">' + ''.join(cards) + '</ul>'


def hotspots_page(state) -> str:
    data = hotspots_data(state)
    waits = data["waits"]
    if not waits:
        wait_markup = f'<p class="missing">Wait data is missing: {esc(data["wait_error"] or "no eligible tickets or timeline data")}</p>'
    else:
        rows = []
        for row in waits:
            median = row["median_h"]
            if median is None:
                continue
            label = f'median {median:.1f}h; worst {row["worst_h"]:.1f}h'
            cls = "bottleneck" if row["state"] == data["bottleneck"] else ""
            rows.append(f'<li class="{cls}"><b>{esc(row["state"])}</b><span>{esc(label)}</span></li>')
        wait_markup = '<ul class="wait-list">' + "".join(rows) + '</ul>'
    lane_markup = lane_failure_html(data["lane_rates"])
    if not data["lane_rates"] and data["comment_error"]:
        lane_markup = f'<p class="missing">Lane failure data is missing: {esc(data["comment_error"])}</p>'
    findings = data["findings"]
    finding_markup = "".join(f'<li><b>{esc(row["rule"])}</b><span>{row["count"]} findings {esc(row["trend"])}</span></li>' for row in findings) or f'<p class="missing">Review finding data is missing: {esc(data["comment_error"] or "no finding records in issue comments")}</p>'
    stuck = data["stuck"]
    stuck_markup = "".join(f'<li><b>{esc(row["id"])}</b><span>{esc(row["reason"])}</span></li>' for row in stuck) or '<p class="missing">Repeatedly stuck ticket data is missing. No failed tickets or rung records were found.</p>'
    return f'<div class="insight-grid"><section class="panel"><h2>Where it slows</h2>{wait_markup}</section><section class="panel"><h2>Review findings / last 7 days</h2><ul class="wait-list">{finding_markup}</ul><h2>Lane failure rate / last 7 days</h2>{lane_markup}</section><section class="panel wide"><h2>Repeatedly stuck</h2><ul class="wait-list">{stuck_markup}</ul></section></div>'


def finding_rules(comments: list[dict], now: float) -> list[dict]:
    windows: dict[str, list[int]] = {}
    for comment in comments:
        at = metrics.stamp(comment.get("created_at"))
        if not at or at < now - 14 * 86400 or at > now:
            continue
        body = str(comment.get("body") or "")
        if "finding" not in body.lower() and "grug" not in body.lower():
            continue
        for line in body.splitlines():
            match = re.search(r"(?:rule|finding)[: ]+(.+)", line, re.I)
            if match:
                rule = match.group(1).strip(" -*`")
                bucket = windows.setdefault(rule, [0, 0])
                bucket[0 if at >= now - 7 * 86400 else 1] += 1
    rows = [{"rule": rule, "count": counts[0],
             "trend": "up" if counts[0] > counts[1] else "down" if counts[0] < counts[1] else "flat"}
            for rule, counts in windows.items() if counts[0]]
    return sorted(rows, key=lambda row: (-row["count"], row["rule"]))[:8]


def stuck_tickets(state, comments: list[dict]) -> list[dict]:
    rows = []
    for repo, issues in state.data.items():
        for issue in issues or []:
            rung = issue.get("rung")
            number = issue.get("number")
            ticket_comments = [comment for comment in comments if
                               comment.get("html_url", "").find(f"/{number}") >= 0]
            for comment in ticket_comments:
                match = re.search(r"\brung\s*[:#]?\s*(\d+)\b", str(comment.get("body") or ""), re.I)
                if match:
                    rung = max(int(rung or 0), int(match.group(1)))
            if issue.get("state") == "failed" or (isinstance(rung, int) and rung > 0):
                reason = "failed" if issue.get("state") == "failed" else f"rung {rung}"
                rows.append({"id": f"{repo}#{number}", "reason": reason})
    return rows[:20]
