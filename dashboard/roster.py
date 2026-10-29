"""The roster: every brain and the workhorse, their recent ticks, and who
is late or down.

Brain actors are derived from the journal comments the snapshot already
reads: each day journal's `Tick <time>, <holder>` sections are that
holder's ticks, newest first, with the counts the tick's own Moved line
states. A holder the code has never seen appears on its own; it simply has
no cadence until one is named below, and so no lateness verdict.

The workhorse keeps no comments: its journal is one issue body it
overwrites in place (read by the snapshot when the deployment names it in
LOOM_WORKHORSE_JOURNAL). Its row reads that body's Updated line and its
latest counted line.

Lateness is cadence-relative, never a fixed wall clock: OK up to 1.5x the
cadence, LATE past that, DOWN past 2.5x. Ages are computed here, at build
time, from the journal timestamps themselves, so the page cannot present
an old snapshot's silence as the present (the shell stamps the snapshot's
own freshness separately).
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import brain_view

# Each scheduled actor's expected tick cadence, in seconds. The primary
# brain runs every 30 minutes (its cron), the backup brain hourly (its
# launchd job), and the workhorse overwrites its journal at most 2 hours
# apart (its journal's own words). A holder with no entry still appears;
# it is shown without a lateness verdict.
BRAIN_CADENCE = {"muse": 30 * 60, "lane-machine": 60 * 60}
WORKHORSE_CADENCE = 2 * 3600
ROLES = {"muse": "primary brain", "lane-machine": "backup brain"}
OPERATOR_HOLDERS = {"operator-session", "operator", "mastermind"}
LATE_FACTOR = 1.5
DOWN_FACTOR = 2.5
RECENT_TICKS = 8

TICK_RE = brain_view.TICK_RE
MOVED_RE = re.compile(r"^- Moved: (.+)$", re.M)
COUNT_RE = re.compile(r"\b(merged|briefed|closed|verified|deferred|escalated|swept) (\d+)")
COUNT_ORDER = ("merged", "briefed", "closed", "verified", "deferred", "escalated", "swept")
UPDATED_RE = re.compile(r"^Updated: (\d{4}-\d\d-\d\d) (\d\d):(\d\d)Z", re.M)
COUNTED_RE = re.compile(r"counted (\d+)(?: \(closed (\d+), merged (\d+), rewritten (\d+)\))?")
PACE_RE = re.compile(r"pace (\d+) per hour")
STATUS_RANK = {"down": 0, "late": 1, "ok": 2, "untracked": 3}


def _parse_time(value) -> Optional[datetime]:
    text = str(value or "").strip()
    match = re.match(r"(\d{4}-\d\d-\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?", text)
    if not match:
        return None
    year, month, day = (int(part) for part in text[:10].split("-"))
    hour, minute, second = int(match.group(2)), int(match.group(3)), int(match.group(4) or 0)
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _counts(section: str) -> Dict[str, int]:
    moved = MOVED_RE.search(section)
    if not moved:
        return {}
    return {name: int(count) for name, count in COUNT_RE.findall(moved.group(1))}


def _line(section: str) -> str:
    for raw in section.splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        if line.startswith("Moved:") or MOVED_RE.match(line):
            continue
        return line.lstrip("- ").strip()[:200]
    return "nothing changed"


def _brain_ticks(journals) -> Dict[str, List[Dict[str, Any]]]:
    """holder -> its ticks (newest first), from every journal comment."""
    by_holder: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for journal in journals or []:
        body = str(journal.get("body") or "")
        if not body.startswith(brain_view.JOURNAL_MARKER):
            continue
        matches = list(TICK_RE.finditer(body))
        ticks = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            moment = _parse_time(match.group(1))
            if moment is None:
                continue
            section = body[match.end():end]
            ticks.append({"holder": match.group(2), "moment": moment,
                          "counts": _counts(section), "line": _line(section)})
        if not ticks:
            # A journal without Tick sections (the older one-tick shapes):
            # brain_view's own reader names its holder and summary line.
            found = brain_view._tick(journal)
            moment = _parse_time(found.get("at")) or _parse_time(
                journal.get("updated_at")) or _parse_time(journal.get("created_at"))
            if moment is not None and found.get("holder") not in (None, "holder missing"):
                ticks.append({"holder": found["holder"], "moment": moment,
                              "counts": {}, "line": found.get("line") or "nothing changed"})
        for tick in ticks:
            slot = by_holder.setdefault(tick["holder"], {})
            key = _iso(tick["moment"])
            if key not in slot or (not slot[key]["counts"] and tick["counts"]):
                slot[key] = tick
    return {holder: sorted(found.values(), key=lambda t: t["moment"], reverse=True)
            for holder, found in by_holder.items()}


def _status(age: Optional[float], cadence: Optional[int]) -> str:
    if age is None or cadence is None:
        return "untracked"
    if age > DOWN_FACTOR * cadence:
        return "down"
    if age > LATE_FACTOR * cadence:
        return "late"
    return "ok"


def _actor(name, role, cadence, ticks, now):
    last = ticks[0]["moment"] if ticks else None
    age = (now - last).total_seconds() if last else None
    return {"name": name, "role": role, "cadence": cadence,
            "last_at": _iso(last) if last else None,
            "age_seconds": int(age) if age is not None else None,
            "status": _status(age, cadence),
            "ticks": [{"at": _iso(tick["moment"]), "counts": tick["counts"],
                       "line": tick["line"],
                       **({"counted": tick["counted"], "pace": tick["pace"]}
                          if "counted" in tick else {})}
                      for tick in ticks[:RECENT_TICKS]]}


def _workhorse_actor(workhorse, now):
    body = str((workhorse or {}).get("body") or "")
    updated = UPDATED_RE.search(body)
    moment = None
    if updated:
        moment = _parse_time("%s %s:%s" % (updated.group(1), updated.group(2), updated.group(3)))
    moment = moment or _parse_time((workhorse or {}).get("updated_at"))
    if moment is None:
        return None
    counts = {}
    counted = pace = None
    for match in COUNTED_RE.finditer(body):
        counted = int(match.group(1))
        counts = {name: int(value) for name, value in
                  zip(("closed", "merged", "rewritten"), match.groups()[1:]) if value is not None}
    for match in PACE_RE.finditer(body):
        pace = int(match.group(1))
    tick = {"moment": moment, "counts": counts,
            "line": "counted %d, pace %d per hour" % (counted, pace) if counted is not None
                    else "journal updated"}
    if counted is not None:
        tick["counted"], tick["pace"] = counted, pace
    return _actor("workhorse", "workhorse", WORKHORSE_CADENCE, [tick], now)


def build_roster(journals, workhorse, now) -> Dict[str, Any]:
    """Every brain in the journals plus the workhorse, worst state first."""
    actors = []
    for holder, ticks in _brain_ticks(journals).items():
        key = holder.lower()
        name = "operator session" if key in OPERATOR_HOLDERS else holder
        role = "operator session" if key in OPERATOR_HOLDERS else ROLES.get(key, "brain")
        actors.append(_actor(name, role, BRAIN_CADENCE.get(key), ticks, now))
    if workhorse:
        actor = _workhorse_actor(workhorse, now)
        if actor:
            actors.append(actor)
    actors.sort(key=lambda a: (STATUS_RANK[a["status"]], 1 if a["role"] == "workhorse" else 0,
                               a["name"]))
    reason = "" if actors else (
        "no brain journals were read and no workhorse journal is configured")
    return {"actors": actors, "reason": reason, "now": now}


def _age_text(seconds) -> str:
    if seconds is None:
        return "time unavailable"
    seconds = max(0, int(seconds))
    if seconds < 3600:
        count, unit = seconds // 60, "minute"
    elif seconds < 86400:
        count, unit = seconds // 3600, "hour"
    else:
        count, unit = seconds // 86400, "day"
    return "%d %s%s ago" % (count, unit, "" if count == 1 else "s")


def _cadence_text(actor) -> str:
    cadence = actor.get("cadence")
    if cadence is None:
        return "no set cadence"
    if actor["role"] == "workhorse":
        return "updates at most every %d hours" % (cadence // 3600)
    if cadence >= 3600:
        return "ticks every %d minutes" % (cadence // 60) if cadence % 3600 else \
            "ticks every hour"
    return "ticks every %d minutes" % (cadence // 60)


def _counts_text(tick) -> str:
    parts = []
    if tick.get("counted") is not None:
        parts.append("counted %d" % tick["counted"])
    parts += ["%s %d" % (name, tick["counts"][name]) for name in COUNT_ORDER
              if name in tick.get("counts", {})]
    parts += ["%s %d" % (name, value) for name, value in tick.get("counts", {}).items()
              if name not in COUNT_ORDER]
    return " · ".join(parts)


def _render_actor(actor) -> str:
    esc = html.escape
    status = actor["status"]
    ticks = "".join(
        '<li><span class="at machine-fact">%s</span><div><p>%s</p>%s</div></li>' % (
            esc(tick["at"][11:16]), esc(tick["line"]),
            '<p class="machine-fact">%s</p>' % esc(_counts_text(tick)) if _counts_text(tick) else "")
        for tick in actor["ticks"])
    return ('<article class="card%s"><h2>%s</h2>'
            '<p class="machine-fact"><span class="actor-status outcome-%s">%s</span> %s / %s</p>'
            '<p>last tick %s</p>'
            '<ol class="recent-ticks">%s</ol></article>') % (
        " actor-down" if status == "down" else "", esc(actor["name"]),
        {"ok": "ok", "late": "degraded", "down": "failed"}.get(status, "none"),
        esc(status.upper() if status != "untracked" else "NO SET CADENCE"),
        esc(actor["role"]), esc(_cadence_text(actor)),
        esc(_age_text(actor["age_seconds"])), ticks)


def render_roster(data) -> str:
    if not data["actors"]:
        return ('<p class="page-lede">Every brain and the workhorse, their ticks, '
                'and who is down.</p><p class="empty-state">No roster can be built: '
                '%s.</p>' % html.escape(data["reason"]))
    cards = "".join(_render_actor(actor) for actor in data["actors"])
    return ('<p class="page-lede">Every brain and the workhorse, their recent ticks, '
            'and who is late or down. Lateness is measured against each actor\'s own '
            'cadence: late past one and a half cycles, down past two and a half.</p>'
            '<section class="section"><div class="roster-grid">%s</div></section>' % cards)
