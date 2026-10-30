"""The Fleet timeline (/fleet): every engine as a row over the last 24 hours
and the next 2, from the snapshot the other pages already read.

Rows: one per brain (a journal holder on the goal homes; the brains the docs
give a cadence are drawn even when silent), the reports, the station, the
notes people leave on the goal homes, and one per lane. An event is a
journal tick, a report, a station comment or chain snapshot, a lane claim,
round, outcome, pull request or heartbeat; each carries a one-line gist. A
row with a cadence marks any silence longer than the cadence plus 50 percent
as a missed run. Nothing here reads GitHub: the page renders what the
background pass holds, so a row shows only what that pass reads.
"""
from __future__ import annotations

import collections
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from bus import brain
from . import brain_view, views
from .hot_spots import ISSUE_URL_RE, OUTCOME_RE

WINDOW = timedelta(hours=24)
AHEAD = timedelta(hours=2)
PX_PER_MIN = 1
# A silence longer than the cadence times this is a missed run.
MISSED_FACTOR = 1.5
# The brains the docs give a cadence (README: the brain runs every 30
# minutes; docs/BRAIN.md: the hourly lane-machine brain), by journal holder.
# They are drawn even when silent, so a brain that stopped is a row of missed
# runs and not a missing row. Any other holder appears when it journals.
CADENCES = (("muse", "Muse brain", 30), ("lane-machine", "Lane-machine brain", 60))
STATION_CADENCE = 10
# The operator's reading times (bus.brain.REPORT_TIMES) and the operators
# summary times, both New York (bus.brain.REPORT_ZONE).
REPORT_TIMES = brain.REPORT_TIMES
SUMMARY_TIMES = ((9, 5), (17, 5))
ZONE = brain.REPORT_ZONE
GIST_MAX = 160
# Chain snapshot times kept in memory: 400 at one every 10 minutes is 2.7 days.
CHAIN_SEEN_MAX = 400
JOURNAL_MARKER = brain_view.JOURNAL_MARKER
REPORT_MARKER = "<!-- loom:report v1 -->"
ROUND_MARKER = "<!-- loom:lane-round v1 -->"
# Comments only the station writes (bus/station.py): their author is the
# station's login, whatever that login is in a deployment.
STATION_SHAPES = ("Shipped: the station deployed", "<!-- loom:fix v1 -->", "<!-- loom:watch v1 -->",
                  "<!-- loom:adopted v1", "<!-- loom:digest v1 -->")
LANE_LINE_RE = re.compile(r"^Lane ([A-Za-z0-9_.-]+) (.+)$")
CLAIM_RE = re.compile(r"<!-- loom:remote-claim v1 ([A-Za-z0-9_.-]+) ([0-9]+) ")
# A lane round, as the lane's round comment writes it (edited in place, so
# each round is timed by its own heading, not the comment).
ROUND_RE = re.compile(r"^### Round ([0-9]+), (\S+)[ \t]*\n[ \t]*\n(Lane ([A-Za-z0-9_.-]+) .+)$", re.M)
EPOCH_RE = re.compile(r"[0-9]{9,}(?:\.[0-9]+)?")


def _stamp(value: Any) -> Optional[datetime]:
    """A UTC datetime from an ISO stamp, a table stamp (2030-01-08 11:55:00Z)
    or an epoch number; None for anything else."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if EPOCH_RE.fullmatch(text):
            return datetime.fromtimestamp(float(text), timezone.utc)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except (ValueError, OverflowError, OSError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _login(record: Dict[str, Any]) -> str:
    return str((record.get("user") or {}).get("login") or "")


def _is_person(record: Dict[str, Any]) -> bool:
    user = record.get("user") or {}
    login = str(user.get("login") or "")
    return bool(login) and user.get("type") != "Bot" and not login.endswith("[bot]")


def _gist(body: str) -> str:
    """The first line of prose in a body, markers skipped, one line, cut."""
    for line in str(body or "").splitlines():
        line = " ".join(line.split())
        if line and not line.startswith("<!--"):
            return line if len(line) <= GIST_MAX else line[:GIST_MAX].rsplit(" ", 1)[0] + " ..."
    return "no text"


def _event(at: datetime, kind: str, gist: str, beat: bool = False, href: Optional[str] = None) -> Dict[str, Any]:
    return {"at": at, "kind": kind, "gist": gist, "beat": beat, "href": href}


def _row(key: str, label: str, kind: str, cadence: Optional[int] = None, schedule=(), word: str = "tick",
         meta: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "kind": kind, "cadence": cadence, "schedule": tuple(schedule),
            "word": word, "meta": meta, "events": [], "missed": [], "due": [], "next": None, "note": "",
            # Silences are judged from the newest beat before the window, else
            # from its start: the goal homes are read in full. A row read from
            # memory only (the station's chain posts) sets this False and is
            # judged from its first beat, or not at all.
            "judged_from_start": True, "observed_from": None, "_all": []}


def _add(row: Dict[str, Any], event: Dict[str, Any]) -> None:
    row["_all"].append(event)


def _issue_key(comment: Dict[str, Any]):
    match = ISSUE_URL_RE.search(str(comment.get("issue_url") or ""))
    return (str(comment.get("repo") or ""), int(match.group(1))) if match else None


def _journal_rows(journals, start: datetime, now: datetime):
    """One row per journal holder, the documented brains first, and the
    goal homes the journals were read from. A holder outside CADENCES has
    a row only when it ticked inside the window: the journals reach back
    weeks, and a holder that last ticked days ago is not part of today's
    fleet."""
    rows = {key: _row("brain:" + key, label, "brain", cadence=minutes) for key, label, minutes in CADENCES}
    seen, homes = set(), set()
    for journal in journals or []:
        if not isinstance(journal, dict):
            continue
        body = str(journal.get("body") or "")
        if not body.startswith(JOURNAL_MARKER) or body in seen:
            continue
        seen.add(body)
        if journal.get("repo") and journal.get("issue_number"):
            homes.add((str(journal["repo"]), int(journal["issue_number"])))
        tick = brain_view._tick(journal)
        at = _stamp(journal.get("created_at")) or _stamp(tick["at"])
        if at is None:
            continue
        holder = str(tick["holder"]).lower()
        row = rows.setdefault(holder, _row("brain:" + holder, holder, "brain"))
        kind = "tick-failed" if tick["outcome"] == "failed" else "tick"
        _add(row, _event(at, kind, "%s: %s" % (tick["outcome"], tick["line"]), beat=True, href=journal.get("html_url")))
    documented = [rows[key] for key, _, _ in CADENCES]
    others = sorted((row for key, row in rows.items() if key not in {c[0] for c in CADENCES}
                     and any(start <= e["at"] <= now for e in row["_all"])), key=lambda row: row["key"])
    return documented + others, homes


def _station_login(comments) -> Optional[str]:
    counts = collections.Counter()
    for comment in comments:
        body = str(comment.get("body") or "")
        if body.startswith(STATION_SHAPES) or OUTCOME_RE.match(body):
            login = _login(comment)
            if login:
                counts[login] += 1
    return counts.most_common(1)[0][0] if counts else None


def _lane_kind(first_line: str) -> str:
    match = LANE_LINE_RE.match(first_line)
    if not match:
        return "note"
    return "failed" if match.group(2).startswith("failed") else "outcome"


def _station_row(comments, lanes, chain_seen) -> Dict[str, Any]:
    row = _row("station", "Station", "station", cadence=STATION_CADENCE, word="run")
    row["judged_from_start"] = False
    login = _station_login(comments)
    for comment in comments:
        body = str(comment.get("body") or "")
        at = _stamp(comment.get("created_at"))
        if at is None or not login or _login(comment) != login:
            continue
        if body.startswith((JOURNAL_MARKER, REPORT_MARKER, ROUND_MARKER)):
            continue
        first = body.split("\n", 1)[0].strip()
        kind = "shipped" if first.startswith("Shipped:") else _lane_kind(first)
        _add(row, _event(at, kind, _gist(body), href=comment.get("html_url")))
    status = [c for c in lanes or [] if isinstance(c, dict) and str(c.get("body") or "").startswith(views.LANES_MARKER)]
    newest = max(status, key=lambda c: str(c.get("updated_at") or c.get("created_at") or ""), default=None)
    if newest:
        at = _stamp(newest.get("updated_at") or newest.get("created_at"))
        if at is not None:
            _add(row, _event(at, "status", "lane status table updated", href=newest.get("html_url")))
    beats = sorted(_stamp(value) for value in chain_seen or () if _stamp(value) is not None)
    for at in beats:
        _add(row, _event(at, "beat", "chain snapshot received", beat=True))
    if beats:
        row["observed_from"] = beats[0]
    else:
        row["note"] = "no chain snapshot since the dashboard started, so silences are not judged"
    return row


def _reports_row(comments) -> Dict[str, Any]:
    row = _row("reports", "Reports", "reports", schedule=REPORT_TIMES, meta="due 10:00 and 21:00 New York")
    for comment in comments:
        body = str(comment.get("body") or "")
        at = _stamp(comment.get("created_at"))
        if at is not None and body.startswith(REPORT_MARKER):
            _add(row, _event(at, "report", _gist(body), href=comment.get("html_url")))
    return row


def _notes_row(comments, homes) -> Dict[str, Any]:
    row = _row("notes", "Operator notes", "notes", schedule=SUMMARY_TIMES,
               meta="people's comments on the goal homes; summary due 09:05 and 17:05 New York")
    for comment in comments:
        body = str(comment.get("body") or "").lstrip()
        at = _stamp(comment.get("created_at"))
        if (at is None or not _is_person(comment) or body.startswith("<!--")
                or _issue_key(comment) not in homes):
            continue
        _add(row, _event(at, "note", _gist(body), href=comment.get("html_url")))
    return row


def _lane_rows(comments, pull_requests, lane_rows) -> List[Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}

    def lane(name: str) -> Dict[str, Any]:
        return rows.setdefault(name, _row("lane:" + name, name, "lane", meta="no status row"))

    for comment in comments:
        body = str(comment.get("body") or "")
        at = _stamp(comment.get("created_at"))
        href = comment.get("html_url")
        first = body.split("\n", 1)[0].strip()
        named = LANE_LINE_RE.match(first)
        if named and at is not None:
            _add(lane(named.group(1)), _event(at, _lane_kind(first), first, href=href))
        claim = CLAIM_RE.search(body)
        if claim and at is not None:
            _add(lane(claim.group(1)), _event(at, "claim", "claimed %s#%s" % (
                comment.get("repo") or "repo missing", claim.group(2)), href=href))
        if body.startswith(ROUND_MARKER):
            for number, when, text, name in ROUND_RE.findall(body):
                round_at = _stamp(when)
                if round_at is not None:
                    _add(lane(name), _event(round_at, "round", "round %s: %s" % (number, " ".join(text.split())), href=href))
    for pr in pull_requests:
        if not isinstance(pr, dict):
            continue
        parsed = views.footer(pr.get("body"))
        if not parsed:
            continue
        repo, ticket, name, model = parsed
        opened, merged = _stamp(pr.get("created_at")), _stamp(pr.get("merged_at"))
        number, href = pr.get("number"), pr.get("html_url")
        if opened is not None:
            _add(lane(name), _event(opened, "opened", "opened pull request #%s for %s#%s (%s)" % (
                number, repo, ticket, model), href=href))
        if merged is not None:
            _add(lane(name), _event(merged, "merged", "pull request #%s merged" % number, href=href))
    for item in lane_rows or []:
        name = str(item.get("lane") or "")
        if not name:
            continue
        row = lane(name)
        row["meta"] = views.LANE_STATE_WORD[views.lane_state(item)]
        at = _stamp(item.get("heartbeat"))
        if at is not None:
            _add(row, _event(at, "heartbeat", "newest heartbeat"))
    return [rows[name] for name in sorted(rows, key=views._lane_order)]


def _schedule(times, start: datetime, end: datetime) -> List[datetime]:
    """The wall-clock times (hour, minute) in ZONE that fall in [start, end]."""
    zone = ZoneInfo(ZONE)
    day = start.astimezone(zone).date() - timedelta(days=1)
    out = []
    for offset in range((end - start).days + 3):
        date = day + timedelta(days=offset)
        for hour, minute in times:
            at = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone).astimezone(timezone.utc)
            if start <= at <= end:
                out.append(at)
    return sorted(out)


def _cadence_due(row: Dict[str, Any], start: datetime, end: datetime) -> List[datetime]:
    beats = [e["at"] for e in row["_all"] if e["beat"] and start <= e["at"] <= end]
    at, step, out = (min(beats) if beats else start), timedelta(minutes=row["cadence"]), []
    while at <= end:
        if at >= start:
            out.append(at)
        at += step
    return out


def _finish(row: Dict[str, Any], start: datetime, now: datetime, end: datetime) -> Dict[str, Any]:
    """Events in the window, due marks, missed runs and the next time."""
    row["_all"].sort(key=lambda e: e["at"])
    row["events"] = [e for e in row["_all"] if start <= e["at"] <= now]
    beats = [e["at"] for e in row["_all"] if e["beat"] and e["at"] <= now]
    if row["cadence"]:
        step = timedelta(minutes=row["cadence"])
        row["due"] = _cadence_due(row, start, end)
        row["next"] = beats[-1] + step if beats else None
        if row["observed_from"] is None and row["judged_from_start"]:
            before = [b for b in beats if b < start]
            row["observed_from"] = before[-1] if before else start
        anchor, limit = row["observed_from"], step * MISSED_FACTOR
        points = ([anchor] + [b for b in beats if b > anchor] + [now]) if anchor is not None else []
        row["missed"] = [{"from": a, "to": b, "minutes": int((b - a).total_seconds() // 60)}
                         for a, b in zip(points, points[1:]) if b - a > limit]
        if not [b for b in beats if b >= start] and not row["note"]:
            row["note"] = "no %s in the last 24 hours" % row["word"]
    elif row["schedule"]:
        row["due"] = _schedule(row["schedule"], start, end)
        ahead = _schedule(row["schedule"], now, now + timedelta(days=2))
        row["next"] = ahead[0] if ahead else None
    if not row["events"] and not row["note"]:
        row["note"] = "no event in the last 24 hours"
    del row["_all"]
    return row


def build_fleet(payload: Dict[str, Any], lane_rows, chain_seen, now: datetime) -> Dict[str, Any]:
    """Everything /fleet shows: the window and one row per engine."""
    payload = payload or {}
    start, end = now - WINDOW, now + AHEAD
    comments = [c for c in payload.get("issue_comments") or [] if isinstance(c, dict)]
    brains, homes = _journal_rows((payload.get("improving") or {}).get("journals"), start, now)
    rows = brains + [_reports_row(comments), _station_row(comments, payload.get("lanes"), chain_seen),
                     _notes_row(comments, homes)]
    rows += _lane_rows(comments, payload.get("pull_requests") or [], lane_rows)
    return {"start": start, "end": end, "now": now, "rows": [_finish(row, start, now, end) for row in rows],
            "lanes_reason": "" if any(row["kind"] == "lane" for row in rows)
            else "No lane has a status row or an event in the last 24 hours."}


def remember_beat(state: Any, now: float) -> None:
    """Keep the time of a station chain snapshot on the dashboard state, the
    station's heartbeat for this page (POST /api/chain, every 10 minutes)."""
    seen = getattr(state, "chain_seen", None)
    if seen is None:
        seen = state.chain_seen = collections.deque(maxlen=CHAIN_SEEN_MAX)
    seen.append(float(now))


def _x(at: datetime, start: datetime) -> int:
    return int(round((at - start).total_seconds() / 60 * PX_PER_MIN))


def _hhmm(at: datetime, zone) -> str:
    return at.astimezone(zone).strftime("%H:%M")


def _meta(row: Dict[str, Any], zone) -> str:
    parts = []
    if row["cadence"]:
        parts.append("every %d min" % row["cadence"])
    if row["meta"]:
        parts.append(row["meta"])
    last = [e for e in row["events"] if e["beat"]] if row["cadence"] else row["events"]
    if last:
        parts.append("last %s" % _hhmm(last[-1]["at"], zone))
    if row["next"]:
        parts.append("next %s" % _hhmm(row["next"], zone))
    if row["note"]:
        parts.append(row["note"])
    return ", ".join(parts)


def _render_row(row: Dict[str, Any], data: Dict[str, Any], zone) -> str:
    esc = html.escape
    start, now, end = data["start"], data["now"], data["end"]
    marks = ['<i class="fleet-due" style="left:%dpx"></i>' % _x(at, start) for at in row["due"]]
    if row["next"] and start <= row["next"] <= end:
        marks.append('<i class="fleet-next" style="left:%dpx"></i>' % _x(row["next"], start))
    marks.append('<i class="fleet-now" style="left:%dpx"></i>' % _x(now, start))
    for miss in row["missed"]:
        a, b = max(miss["from"], start), min(miss["to"], now)
        text = "no %s for %d minutes, cadence %d" % (row["word"], miss["minutes"], row["cadence"])
        marks.append('<span class="fleet-miss" style="left:%dpx;width:%dpx" tabindex="0" aria-label="%s">'
                     '<span class="fleet-gist">%s</span></span>' % (
                         _x(a, start), max(2, _x(b, start) - _x(a, start)), esc(text, quote=True), esc(text)))
    for event in row["events"]:
        gist = "%s %s" % (_hhmm(event["at"], zone), event["gist"])
        tag = "a" if event["href"] else "span"
        href = ' href="%s"' % esc(str(event["href"]), quote=True) if event["href"] else ""
        marks.append('<%s class="fleet-ev ev-%s" style="left:%dpx"%s tabindex="0" aria-label="%s">'
                     '<span class="fleet-gist">%s</span></%s>' % (
                         tag, esc(event["kind"]), _x(event["at"], start), href, esc(gist, quote=True), esc(gist), tag))
    return ('<section class="fleet-row fleet-%s"><div class="fleet-head"><strong>%s</strong>'
            '<span class="machine-fact">%s</span></div><div class="fleet-track">%s</div></section>' % (
                esc(row["kind"]), esc(row["label"]), esc(_meta(row, zone)), "".join(marks)))


def _axis(data: Dict[str, Any], zone) -> str:
    start, end = data["start"], data["end"]
    at = start.replace(minute=0, second=0, microsecond=0)
    if at < start:
        at += timedelta(hours=1)
    hours = []
    while at <= end:
        hours.append('<span class="fleet-hour" style="left:%dpx">%s</span>' % (_x(at, start), _hhmm(at, zone)))
        at += timedelta(hours=1)
    return ('<div class="fleet-row fleet-axis" aria-hidden="true"><div class="fleet-head machine-fact">%s</div>'
            '<div class="fleet-track">%s<i class="fleet-now" style="left:%dpx"></i></div></div>' % (
                html.escape(data["now"].astimezone(zone).tzname() or "UTC"), "".join(hours), _x(data["now"], start)))


def render_fleet(data: Dict[str, Any]) -> str:
    esc = html.escape
    zone = views._dashboard_zone()
    width = _x(data["end"], data["start"])
    rows = "".join(_render_row(row, data, zone) for row in data["rows"])
    lanes = '<p class="empty-state">%s</p>' % esc(data["lanes_reason"]) if data["lanes_reason"] else ""
    return ('<p class="page-lede">One row per engine over the last 24 hours and the next 2: what ran, when it '
            'was due, what was missed and what comes next. Hover or tap a mark for its gist. The axis scrolls '
            'inside its own box.</p>'
            '<p class="machine-fact fleet-span">%s to %s %s, now %s</p>'
            '<div class="fleet-scroll"><div class="fleet-grid" style="--fleet-w:%dpx">%s%s</div></div>%s'
            '<ul class="fleet-legend machine-fact"><li><i class="fleet-ev ev-tick"></i> ran, with its gist</li>'
            '<li><i class="fleet-due"></i> due (cadence or reading time)</li><li><i class="fleet-next"></i> next</li>'
            '<li><i class="fleet-miss"></i> missed: silent for longer than the cadence plus 50 percent</li>'
            '<li><i class="fleet-now"></i> now</li></ul>'
            '<p class="note">Brain rows are journal holders on the goal homes; the station row is its comments '
            'and its chain snapshots; lane rows are claims, rounds, outcomes, pull requests and the newest '
            'heartbeat from the lane status table. A lane has no cadence, so a quiet lane is not a missed run.</p>') % (
        esc(data["start"].astimezone(zone).strftime("%Y-%m-%d %H:%M")), esc(data["end"].astimezone(zone).strftime("%Y-%m-%d %H:%M")),
        esc(data["now"].astimezone(zone).tzname() or "UTC"), esc(_hhmm(data["now"], zone)),
        width, _axis(data, zone), rows, lanes)
