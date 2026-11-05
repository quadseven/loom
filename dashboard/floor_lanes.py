"""The Floor's lane strip: each lane with the ticket it works and what it is
doing now (loom#2157). The lane rows, heartbeats and holds come from the same
reads as the Lanes page (server._lane_view_data); this module only attaches
tickets to them and words the activity."""
from __future__ import annotations

import html
import re
from typing import Any

from bus import tickets as bus_tickets
from . import goals, views

# The states of a ticket a lane still has a hand in, in the order the strip lists them.
ATTACHED = ("running", "pr", "briefed", "merged", "shipped")
ACTIVITY = {"briefed": "briefed, waiting for the station", "running": "implementing", "pr": "in review",
            "merged": "merged, waiting to ship", "shipped": "verifying"}
ISSUE_REF = re.compile(r"/repos/([^/]+/[^/]+)/issues/(\d+)$")


def fix_round_refs(comments: list[dict[str, Any]]) -> set[tuple[str, int]]:
    """The (repo, number) of every ticket that has a loom:fix comment."""
    found = set()
    for comment in comments or []:
        if not isinstance(comment, dict) or not str(comment.get("body") or "").startswith(bus_tickets.FIX_MARKER):
            continue
        match = ISSUE_REF.search(str(comment.get("issue_url") or ""))
        if match:
            found.add((match.group(1), int(match.group(2))))
    return found


def activity(row: dict[str, Any], fixing: set[tuple[str, int]]) -> str:
    """What the ticket's lane is doing with it now, from its state label."""
    state = row.get("state")
    ref = (row.get("repo"), int(row.get("number") or 0))
    if state == "running" and ref in fixing:
        return "fix round"
    if state == "pr" and {"loom:ready", "loom:landing"} & set(row.get("labels") or []):
        return "landing"
    return ACTIVITY.get(state, str(state or "unlabeled"))


def attached(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The open tickets in a state a lane works, most advanced first."""
    order = {name: index for index, name in enumerate(ATTACHED)}
    return sorted((row for row in rows if row.get("open", True) and row.get("state") in order),
                  key=lambda row: (order[row["state"]], int(row.get("number") or 0)))


def _ticket_line(row: dict[str, Any], fixing: set[tuple[str, int]]) -> str:
    esc = html.escape
    href = "/t/%s/%d" % (esc(str(row.get("repo") or ""), quote=True), int(row.get("number") or 0))
    return '<li><span class="lane-activity">%s</span> <a href="%s">%s %s</a></li>' % (
        esc(activity(row, fixing)), href, esc("#%s" % row.get("number")),
        esc(goals.short(str(row.get("title") or "ticket"), 90)))


def lane_card(lane: dict[str, Any], own: list[dict[str, Any]], fixing: set[tuple[str, int]]) -> str:
    """One lane: its tickets with their activity, or idle (able or held)."""
    esc = html.escape
    state = "busy" if own else views.lane_state(lane)
    if own:
        body = '<ul class="lane-tickets">%s</ul>' % "".join(_ticket_line(row, fixing) for row in own)
    elif state == "held":
        body = '<p class="lane-doing">idle, held: %s</p>' % esc(views._hold_text(lane))
    else:
        body = '<p class="lane-doing">idle, able to take work</p>'
    return ('<article class="card lane-card lane-%s"><div class="lane-head"><i class="lane-dot"></i>'
            '<strong>%s</strong><span class="lane-state">%s</span></div>%s'
            '<div class="lane-facts"><span class="lane-beat">%s</span></div></article>') % (
        state, esc(str(lane["lane"])), views.LANE_STATE_WORD[state], body,
        esc(views.format_heartbeat(lane.get("heartbeat"))))


def strip(lanes: list[dict[str, Any]], rows: list[dict[str, Any]], fixing: set[tuple[str, int]]) -> str:
    """The lane cards in lane order and, under them, the tickets no shown lane holds."""
    work = attached(rows)
    names = {str(lane["lane"]) for lane in lanes}
    cards = [lane_card(lane, [row for row in work if row.get("lane") == lane["lane"]], fixing)
             for lane in sorted(lanes, key=lambda lane: views._lane_order(lane["lane"]))]
    loose = [row for row in work if row.get("lane") not in names]
    out = '<div class="lane-grid">%s</div>' % "".join(cards) if cards else (
        '<p class="empty-state">Lane status is missing: no loom:lanes comment was found.</p>')
    if loose:
        out += '<p class="muted">On no shown lane:</p><ul class="lane-tickets">%s</ul>' % "".join(
            _ticket_line(row, fixing) for row in loose)
    return out
