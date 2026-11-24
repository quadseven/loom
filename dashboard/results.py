"""The Results view: what the factory finished and what failed, over the last
day, week and month.

Every number comes from the snapshot the other views read: pull requests
(read with state=all), ticket issues (state=all), and the issue comments of
the hot-spot window. A count the snapshot cannot cover says so ("not
looked", with the reason) instead of showing a number that looks complete:
comments reach back only HOT_SPOT_WINDOW x 2 days, so proofs and lane
failures older than that are not counted.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bus.invariants import ROLL_MARKER

WINDOWS = (("day", "Last day", timedelta(days=1)),
           ("week", "Last week", timedelta(days=7)),
           ("month", "Last month", timedelta(days=30)))
# How far back the snapshot reads issue comments (github.py, two hot-spot
# windows of 7 days).
COMMENT_REACH = timedelta(days=14)
PROOF_MARKER = "<!-- loom:proof v1 -->"
LANE_FAILED = "Lane "
FAILED_WORD = " failed: ["


def _when(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_lane_pr(pr: Dict[str, Any]) -> bool:
    return str((pr.get("head") or {}).get("ref") or "").startswith("loom/")


def _repo_of(pr: Dict[str, Any]) -> str:
    return str(((pr.get("base") or {}).get("repo") or {}).get("full_name") or pr.get("repo") or "")


def _work_failure(body: str) -> bool:
    """A station 'Lane X failed: [work] ...' comment; limits and transient
    failures say nothing about the work and are left out."""
    return body.startswith(LANE_FAILED) and (FAILED_WORD + "work]") in body


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _count_prs(prs: List[Dict[str, Any]], start: datetime, lane: bool, merged: bool) -> int:
    """Lane pull requests (lane True) or the others (lane False) merged, or with
    merged False closed unmerged, since start. The two are never added up: a
    Results page that mixes work people and sessions merged with the lanes'
    reads as the factory's work (2026-10-15)."""
    n = 0
    for pr in prs:
        if _is_lane_pr(pr) != lane:
            continue
        if merged:
            at = _when(pr.get("merged_at"))
        else:
            at = None if pr.get("merged_at") else _when(pr.get("closed_at"))
        n += 1 if at and at >= start else 0
    return n


def _count_closed(tickets: List[Dict[str, Any]], start: datetime, reason: str) -> int:
    return sum(1 for t in tickets
               if (_when(t.get("closed_at")) or _EPOCH) >= start
               and (t.get("state_reason") or "completed") == reason
               and ROLL_MARKER not in (t.get("body") or "").splitlines())


def _count_comments(comments: List[Dict[str, Any]], start: datetime, test) -> int:
    return sum(1 for c in comments
               if (_when(c.get("created_at")) or _EPOCH) >= start and test(str(c.get("body") or "")))


def _is_proof(body: str) -> bool:
    return body.startswith(PROOF_MARKER)


def _week_by_repo(prs, comments, week: datetime):
    by_repo: Dict[str, Dict[str, int]] = {}
    for pr in prs:
        at = _when(pr.get("merged_at"))
        if _repo_of(pr) and _is_lane_pr(pr) and at and at >= week:
            by_repo.setdefault(_repo_of(pr), {"merged": 0, "failed": 0})["merged"] += 1
    for c in comments:
        at = _when(c.get("created_at"))
        repo = str(c.get("repo") or "")
        if repo and at and at >= week and _work_failure(str(c.get("body") or "")):
            by_repo.setdefault(repo, {"merged": 0, "failed": 0})["failed"] += 1
    return sorted(by_repo.items(), key=lambda kv: (-kv[1]["merged"], kv[0]))


def _clock(stamp: Any) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%H:%M UTC")


def _read_note(payload: Dict[str, Any], read_at: Dict[str, Any]) -> str:
    """When the counted sources a pass carried forward were last read: a pass
    below the budget floor keeps the last read, and the page says how old it
    is instead of passing it off as current."""
    carried = payload.get("sources_carried") or []
    parts = ["%s not read since %s" % (label, _clock(read_at[source]))
             for source, label in (("pull_requests", "pull requests"), ("comments", "issue comments"))
             if source in carried and isinstance(read_at.get(source), (int, float))]
    if not parts:
        return ""
    parts[0] = parts[0][0].upper() + parts[0][1:]
    return "%s: %s." % ("; ".join(parts), payload.get("stale_reason") or "GitHub could not refresh them")


def _bump_windows(bucket: Dict[str, int], at: datetime, week_s: datetime, day_s: datetime) -> bool:
    """Increment month/week/day counts for a timestamp >= month_s; return True if in week."""
    bucket["month"] += 1
    if at < week_s:
        return False
    bucket["week"] += 1
    if at >= day_s:
        bucket["day"] += 1
    return True


def _tally_prs(prs: List[Dict[str, Any]], starts: Dict[str, datetime]):
    """Single-pass PR counts for all windows plus weekly lane merges by repo."""
    lane_merged = {key: 0 for key in starts}
    other_merged = {key: 0 for key in starts}
    lane_unmerged = {key: 0 for key in starts}
    by_repo: Dict[str, Dict[str, int]] = {}
    day_s, week_s, month_s = starts["day"], starts["week"], starts["month"]
    for pr in prs:
        merged_raw = pr.get("merged_at")
        lane = _is_lane_pr(pr)
        if merged_raw:
            at = _when(merged_raw)
            if at and at >= month_s:
                in_week = _bump_windows(lane_merged if lane else other_merged, at, week_s, day_s)
                if in_week and lane:
                    repo = _repo_of(pr)
                    if repo:
                        by_repo.setdefault(repo, {"merged": 0, "failed": 0})["merged"] += 1
        elif lane:
            at = _when(pr.get("closed_at"))
            if at and at >= month_s:
                _bump_windows(lane_unmerged, at, week_s, day_s)
    return lane_merged, other_merged, lane_unmerged, by_repo


def _tally_tickets(tickets: List[Dict[str, Any]], starts: Dict[str, datetime]):
    """Single-pass closed ticket counts for all windows."""
    completed = {key: 0 for key in starts}
    not_planned = {key: 0 for key in starts}
    day_s, week_s, month_s = starts["day"], starts["week"], starts["month"]
    for t in tickets:
        at = _when(t.get("closed_at"))
        if not at or at < month_s:
            continue
        reason = t.get("state_reason") or "completed"
        if reason not in ("completed", "not_planned"):
            continue
        body = t.get("body") or ""
        if ROLL_MARKER in body and ROLL_MARKER in body.splitlines():
            continue
        bucket = completed if reason == "completed" else not_planned
        bucket["month"] += 1
        if at >= week_s:
            bucket["week"] += 1
            if at >= day_s:
                bucket["day"] += 1
    return completed, not_planned


def _tally_comments(comments: List[Dict[str, Any]], starts: Dict[str, datetime], by_repo: Dict[str, Dict[str, int]]):
    """Single-pass proof and work-failure counts for day and week windows."""
    proofs = {"day": 0, "week": 0, "month": None}
    failures = {"day": 0, "week": 0, "month": None}
    day_s, week_s = starts["day"], starts["week"]
    for c in comments:
        body = str(c.get("body") or "")
        is_proof = _is_proof(body)
        is_fail = not is_proof and _work_failure(body)
        if not is_proof and not is_fail:
            continue
        at = _when(c.get("created_at"))
        if not at or at < week_s:
            continue
        if is_proof:
            proofs["week"] += 1
            if at >= day_s:
                proofs["day"] += 1
        else:
            failures["week"] += 1
            if at >= day_s:
                failures["day"] += 1
            repo = str(c.get("repo") or "")
            if repo:
                by_repo.setdefault(repo, {"merged": 0, "failed": 0})["failed"] += 1
    return proofs, failures


def build_results(payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Counts per window, and the week's counts per repo."""
    prs = [pr for pr in payload.get("pull_requests") or [] if isinstance(pr, dict)]
    tickets = [row for rows in (payload.get("tickets") or {}).values() if rows for row in rows]
    comments = [c for c in payload.get("issue_comments") or [] if isinstance(c, dict)]
    # source_read_at (dashboard/github.py) stamps each source a pass read; a
    # source with no stamp was never read, so its count is unknown, not 0
    # (loom#779). A payload without the stamps predates them.
    stamps = payload.get("source_read_at")
    read_at = stamps if isinstance(stamps, dict) else {}

    def unread(source):
        return isinstance(stamps, dict) and not read_at.get(source)

    prs_read = not unread("pull_requests")
    comments_read = ("issue_comments" in payload and not payload.get("hot_spots_error")
                     and not unread("comments"))
    reason = "" if comments_read else (payload.get("hot_spots_error") or "issue comments were not read")
    pr_reason = "" if prs_read else "the dashboard has not read them since it started%s" % (
        "; " + str((payload.get("dashboard_errors") or [""])[0]) if payload.get("dashboard_errors") else "")
    starts = {key: now - delta for key, _, delta in WINDOWS}
    none_vals = {key: None for key, _, _ in WINDOWS}
    lane_merged, other_merged, lane_unmerged, by_repo = _tally_prs(prs, starts)
    completed, not_planned = _tally_tickets(tickets, starts)
    proofs, failures = _tally_comments(comments, starts, by_repo)
    reach = "issue comments are read back %d days only" % COMMENT_REACH.days
    pr_row = {"source": "pull requests", "reason": pr_reason, "unread": not prs_read}
    comment_row = {"source": "issue comments", "reason": reason or reach, "unread": False}
    ticket_row = {"source": "tickets", "reason": "", "unread": False}
    rows = [
        ("Lane pull requests merged", pr_row, lane_merged if prs_read else none_vals),
        ("Pull requests merged outside the lanes", pr_row, other_merged if prs_read else none_vals),
        ("Tickets closed as done", ticket_row, completed),
        ("Tickets closed as not planned", ticket_row, not_planned),
        ("Live proofs posted", comment_row, proofs if comments_read else none_vals),
        ("Lane runs failed at the work", comment_row, failures if comments_read else none_vals),
        ("Lane pull requests closed unmerged", pr_row, lane_unmerged if prs_read else none_vals),
    ]
    table = [dict(meta, name=name, values=dict(vals)) for name, meta, vals in rows]
    repos_sorted = sorted(by_repo.items(), key=lambda kv: (-kv[1]["merged"], kv[0]))
    return {"rows": table, "repos": repos_sorted,
            "comments_reason": reason, "comment_reach_days": COMMENT_REACH.days,
            "prs_read": prs_read, "pr_reason": pr_reason, "comments_read": comments_read,
            "read_note": _read_note(payload, read_at)}


def _cell(value: Optional[int], row: Dict[str, Any], window: str) -> str:
    if value is None:
        message = "%s window was not read: %s; %s. Check again at the next dashboard refresh." % (
            window, row.get("source", "issue comments"), row.get("reason") or "it was not read")
        return '<td class="not-looked" title="%s">%s %s</td>' % (
            html.escape(message, quote=True), html.escape(window.capitalize()),
            "not read" if row.get("unread") else "not looked")
    return '<td class="machine-fact">%d</td>' % value


def render_results(data: Dict[str, Any]) -> str:
    esc = html.escape
    head = "".join("<th>%s</th>" % esc(label) for _, label, _ in WINDOWS)
    body = "".join(
        "<tr><th scope=\"row\">%s</th>%s</tr>" % (
            esc(row["name"]),
            "".join(_cell(row["values"][key], row, key) for key, _, _ in WINDOWS))
        for row in data["rows"])
    parts = ['<p class="page-lede">What the factory finished and what failed. Proofs and lane '
             'failures come from issue comments, read back %d days; older ones say not looked.</p>'
             % data["comment_reach_days"],
             '<p class="stale-note">%s</p>' % esc(data["read_note"]) if data.get("read_note") else "",
             '<section><h2 class="label">Done and failed</h2>'
             '<table class="results-table"><thead><tr><th></th>%s</tr></thead><tbody>%s</tbody></table>'
             '</section>' % (head, body),
             '<section><h2 class="label">Last week by repo: lane pull requests merged, lane runs failed</h2>']
    prs_read, comments_read = data.get("prs_read", True), data.get("comments_read", True)
    unread = ["%s were not read (%s)" % (source, why) for source, read, why in (
        ("pull requests", prs_read, data.get("pr_reason")),
        ("issue comments", comments_read, data.get("comments_reason"))) if not read]
    if not data["repos"]:
        parts.append('<p class="empty-state">%s</p>' % (
            esc("Not looked: %s." % "; ".join(unread)) if unread else
            "No lane pull request merged and no lane run failed this week."))
    else:
        def count(value, read, source):
            if read:
                return '<td class="machine-fact">%d</td>' % value
            return '<td class="not-looked" title="%s were not read">not read</td>' % source
        parts.append('<table class="results-table"><thead><tr><th>Repo</th><th>Merged</th><th>Failed</th>'
                     '</tr></thead><tbody>')
        parts.extend('<tr><th scope="row">%s</th>%s%s</tr>' % (
                         esc(repo), count(v["merged"], prs_read, "pull requests"),
                         count(v["failed"], comments_read, "issue comments"))
                     for repo, v in data["repos"])
        parts.append('</tbody></table>')
    parts.append('</section>')
    return "".join(parts)
