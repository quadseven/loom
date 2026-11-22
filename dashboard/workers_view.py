"""The /workers view: cluster workers and goal bots, one row each, from the
snapshot a feed POSTs to /api/workers with the upload token. The dashboard
reads nothing itself: what runs the workers and where the bots are recorded
is the feed's business, so the feed can live beside them.

A row is {kind, name, state, started, last_action, url}: kind is `worker` or
`bot`, state is `running`, `done` or `failed`, started is epoch seconds,
last_action is one line of text and url is an https link to the run or pull
request ("" when there is none).
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Dict

KINDS = ("worker", "bot")
STATES = ("running", "done", "failed")
WORKERS_MAX = 200
TEXT_MAX = 200
# A feed that stops posting is shown as stale after this long.
MAX_AGE = 30 * 60
_NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,80}")
_URL_RE = re.compile(r"https://[^\s<>\"']{1,300}")


def workers(body: Any, now: float) -> Dict[str, Any]:
    """The stored form of a POST /api/workers body: each row checked and
    trimmed, and when it was received; ValueError for a body that is not
    {rows: [...]} of the shape in the module docstring."""
    rows = body.get("rows") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) > WORKERS_MAX:
        raise ValueError("rows must be a list of at most %d entries" % WORKERS_MAX)
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each row must be an object")
        if row.get("kind") not in KINDS or row.get("state") not in STATES:
            raise ValueError("kind must be one of %s and state one of %s" % (KINDS, STATES))
        if not _NAME_RE.fullmatch(str(row.get("name") or "")):
            raise ValueError("each row needs a name")
        started = row.get("started")
        if isinstance(started, bool) or not isinstance(started, (int, float)) or not 0 <= started < 1e12:
            raise ValueError("started must be epoch seconds")
        url = str(row.get("url") or "")
        if url and not _URL_RE.fullmatch(url):
            raise ValueError("url must be an https link or empty")
        action = " ".join(str(row.get("last_action") or "").split())[:TEXT_MAX]
        kept.append({"kind": row["kind"], "name": row["name"], "state": row["state"],
                     "started": float(started), "last_action": action, "url": url})
    return {"rows": kept, "received_at": now}


def _ago(then: float, now: float) -> str:
    seconds = max(0, int(now - then))
    if seconds < 90:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return "%d %s ago" % (seconds // size, unit)
    return "just now"


def _row_html(row: Dict[str, Any], now: float) -> str:
    esc = html.escape
    started = datetime.fromtimestamp(row["started"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    link = ('<a href="%s" rel="noopener">open</a>' % esc(row["url"], quote=True)) if row["url"] else ""
    return ('<tr data-kind="%s" data-state="%s"><td>%s</td><td>%s</td><td>%s</td>'
            '<td title="%s">%s</td><td>%s</td><td>%s</td></tr>') % (
        esc(row["kind"]), esc(row["state"]), esc(row["name"]), esc(row["kind"]), esc(row["state"]),
        esc(started), esc(_ago(row["started"], now)), esc(row["last_action"]) or "&mdash;", link)


def page_html(stored: Any, now: float) -> str:
    """The page body: a table, running first then newest first, or a plain
    line when no feed has posted."""
    if not isinstance(stored, dict) or not isinstance(stored.get("received_at"), (int, float)):
        return '<p class="muted">No cluster worker or goal bot feed has posted yet.</p>'
    rows = sorted(stored.get("rows") or [], key=lambda r: (r["state"] != "running", -r["started"]))
    note = ""
    if now - stored["received_at"] > MAX_AGE:
        note = '<p class="muted">The feed last posted %s; these rows may be out of date.</p>' % _ago(
            stored["received_at"], now)
    if not rows:
        return note + '<p class="muted">No cluster workers or goal bots in the feed.</p>'
    head = "".join("<th>%s</th>" % h for h in ("Name", "Kind", "State", "Started", "Last action", "Link"))
    return note + ('<div class="table-wrap"><table class="workers"><thead><tr>%s</tr></thead>'
                   '<tbody>%s</tbody></table></div>') % (head, "".join(_row_html(r, now) for r in rows))
