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
    """Pull requests merged (or, with merged False, closed unmerged) since start."""
    n = 0
    for pr in prs:
        if lane and not _is_lane_pr(pr):
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
               and (t.get("state_reason") or "completed") == reason)


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


def build_results(payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Counts per window, and the week's counts per repo."""
    prs = [pr for pr in payload.get("pull_requests") or [] if isinstance(pr, dict)]
    tickets = [row for rows in (payload.get("tickets") or {}).values() if rows for row in rows]
    comments = [c for c in payload.get("issue_comments") or [] if isinstance(c, dict)]
    comments_read = "issue_comments" in payload and not payload.get("hot_spots_error")

    def comment_count(delta, test):
        # None is "not looked": unread, or older than the comment window.
        if not comments_read or delta > COMMENT_REACH:
            return None
        return _count_comments(comments, now - delta, test)

    rows = [
        ("Lane pull requests merged", lambda d: _count_prs(prs, now - d, True, True)),
        ("All pull requests merged", lambda d: _count_prs(prs, now - d, False, True)),
        ("Tickets closed as done", lambda d: _count_closed(tickets, now - d, "completed")),
        ("Tickets closed as not planned", lambda d: _count_closed(tickets, now - d, "not_planned")),
        ("Live proofs posted", lambda d: comment_count(d, _is_proof)),
        ("Lane runs failed at the work", lambda d: comment_count(d, _work_failure)),
        ("Lane pull requests closed unmerged", lambda d: _count_prs(prs, now - d, True, False)),
    ]
    table = [{"name": name, "values": {key: fn(delta) for key, _, delta in WINDOWS}}
             for name, fn in rows]
    reason = "" if comments_read else (payload.get("hot_spots_error") or "issue comments were not read")
    return {"rows": table, "repos": _week_by_repo(prs, comments, now - timedelta(days=7)),
            "comments_reason": reason, "comment_reach_days": COMMENT_REACH.days}


def _cell(value: Optional[int], reach_days: int, reason: str) -> str:
    if value is None:
        why = reason or "comments are read back %d days only" % reach_days
        return '<td class="not-looked" title="%s">not looked</td>' % html.escape(why, quote=True)
    return '<td class="machine-fact">%d</td>' % value


def render_results(data: Dict[str, Any]) -> str:
    esc = html.escape
    head = "".join("<th>%s</th>" % esc(label) for _, label, _ in WINDOWS)
    body = "".join(
        "<tr><th scope=\"row\">%s</th>%s</tr>" % (
            esc(row["name"]),
            "".join(_cell(row["values"][key], data["comment_reach_days"], data["comments_reason"])
                    for key, _, _ in WINDOWS))
        for row in data["rows"])
    parts = ['<p class="page-lede">What the factory finished and what failed. Proofs and lane '
             'failures come from issue comments, read back %d days; older ones say not looked.</p>'
             % data["comment_reach_days"],
             '<section><h2 class="label">Done and failed</h2>'
             '<table class="results-table"><thead><tr><th></th>%s</tr></thead><tbody>%s</tbody></table>'
             '</section>' % (head, body),
             '<section><h2 class="label">Last week by repo: lane pull requests merged, lane runs failed</h2>']
    if not data["repos"]:
        parts.append('<p class="empty-state">No lane pull request merged and no lane run failed this week.</p>')
    else:
        parts.append('<table class="results-table"><thead><tr><th>Repo</th><th>Merged</th><th>Failed</th>'
                     '</tr></thead><tbody>')
        parts.extend('<tr><th scope="row">%s</th><td class="machine-fact">%d</td>'
                     '<td class="machine-fact">%d</td></tr>' % (esc(repo), v["merged"], v["failed"])
                     for repo, v in data["repos"])
        parts.append('</tbody></table>')
    parts.append('</section>')
    return "".join(parts)
