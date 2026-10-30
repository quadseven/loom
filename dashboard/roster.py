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
cadence, LATE past that, DOWN past 2.5x. Ages are computed when the page
is built for its request, from the journal timestamps themselves, so the
page cannot present an old snapshot's silence as the present (the shell
stamps the snapshot's own freshness separately).
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import brain_view

# Each scheduled actor's expected tick cadence, in seconds, as this
# deployment schedules it: the primary brain every 30 minutes (its cron),
# the backup brain hourly (its launchd job), and the workhorse at most
# 2 hours between journal overwrites (its journal's own words). A
# deployment whose schedules differ names its own in LOOM_ROSTER_CADENCES
# ("holder=seconds" pairs, comma separated; the workhorse's key is
# "workhorse"). A holder with no entry still appears; it is shown without
# a lateness verdict.
DEFAULT_CADENCES = {"muse": 30 * 60, "lane-machine": 60 * 60, "workhorse": 2 * 3600}
ROLES = {"muse": "primary brain", "lane-machine": "backup brain",
         "operator-session": "operator session"}
LATE_FACTOR = 1.5
DOWN_FACTOR = 2.5
RECENT_TICKS = 8

TICK_RE = brain_view.TICK_RE
MOVED_RE = re.compile(r"^- Moved: (.+)$", re.M)
COUNT_RE = re.compile(r"\b(merged|briefed|closed|verified|deferred|escalated) (\d+)")
COUNT_ORDER = ("merged", "briefed", "closed", "verified", "deferred", "escalated")
UPDATED_RE = re.compile(r"^Updated: (\d{4}-\d\d-\d\d) (\d\d):(\d\d)Z", re.M)
COUNTED_RE = re.compile(r"counted (\d+)(?: \(closed (\d+), merged (\d+), rewritten (\d+)\))?")
PACE_RE = re.compile(r"pace (\d+) per hour")
VERDICT_RANK = {"down": 0, "late": 1, "ok": 2, "untracked": 3}


def _cadences() -> Dict[str, int]:
    merged = dict(DEFAULT_CADENCES)
    for pair in re.split(r"[,\s]+", os.environ.get("LOOM_ROSTER_CADENCES", "")):
        name, sep, seconds = pair.partition("=")
        if sep and name.strip() and seconds.strip().isdigit():
            merged[name.strip().lower()] = int(seconds.strip())
    return merged


def _parse_time(value) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _counts(section: str) -> Dict[str, int]:
    moved = MOVED_RE.search(section)
    if not moved:
        return {}
    return {name: int(count) for name, count in COUNT_RE.findall(moved.group(1))}


def _summary_line(section: str) -> str:
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
                          "counts": _counts(section), "line": _summary_line(section)})
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


def _verdict(age: Optional[float], cadence: Optional[int]) -> str:
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
            "verdict": _verdict(age, cadence),
            "ticks": [{"at": _iso(tick["moment"]), "counts": tick["counts"],
                       "line": tick["line"],
                       **({"counted": tick["counted"], "pace": tick["pace"]}
                          if "counted" in tick else {})}
                      for tick in ticks[:RECENT_TICKS]]}


def _workhorse_actor(workhorse, now, cadence):
    body = str((workhorse or {}).get("body") or "")
    updated = UPDATED_RE.search(body)
    moment = None
    if updated:
        moment = _parse_time("%s %s:%s" % (updated.group(1), updated.group(2), updated.group(3)))
    moment = moment or _parse_time((workhorse or {}).get("updated_at"))
    if moment is None:
        # The journal is configured but nothing in it could be read: the
        # row still stands, with no tick and no lateness verdict.
        return {"name": "workhorse", "role": "workhorse", "cadence": cadence,
                "last_at": None, "age_seconds": None, "verdict": "untracked", "ticks": []}
    counts, counted, pace = {}, None, None
    for text in body.splitlines():
        match = COUNTED_RE.search(text)
        if not match:
            continue
        # The latest counted line wins, read as a unit: its counts and its
        # pace come from that line alone, never mixed across lines.
        counted = int(match.group(1))
        counts = {name: int(value) for name, value in
                  zip(("closed", "merged", "rewritten"), match.groups()[1:]) if value is not None}
        found = PACE_RE.search(text)
        pace = int(found.group(1)) if found else None
    line = "journal updated"
    if counted is not None:
        line = "counted %d" % counted
        if pace is not None:
            line += ", pace %d per hour" % pace
    tick = {"moment": moment, "counts": counts, "line": line}
    if counted is not None:
        tick["counted"], tick["pace"] = counted, pace
    return _actor("workhorse", "workhorse", cadence, [tick], now)


def build_roster(journals, workhorse, now, cadences=None) -> Dict[str, Any]:
    """Every brain in the journals plus the workhorse, worst state first."""
    table = _cadences() if cadences is None else cadences
    actors = []
    for holder, ticks in _brain_ticks(journals).items():
        key = holder.lower()
        actors.append(_actor(holder, ROLES.get(key, "brain"),
                             table.get(key), ticks, now))
    if workhorse:
        actors.append(_workhorse_actor(workhorse, now, table.get("workhorse")))
    actors.sort(key=lambda a: (VERDICT_RANK[a["verdict"]],
                               1 if a["role"] == "workhorse" else 0, a["name"]))
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
    if cadence >= 3600 and cadence % 3600 == 0:
        hours = cadence // 3600
        return "ticks every hour" if hours == 1 else "ticks every %d hours" % hours
    return "ticks every %d minutes" % (cadence // 60)


def _counts_text(tick) -> str:
    parts = []
    if tick.get("counted") is not None:
        parts.append("counted %d" % tick["counted"])
    parts += ["%s %d" % (name, tick["counts"][name]) for name in COUNT_ORDER
              if name in tick.get("counts", {})]
    parts += ["%s %d" % (name, value) for name, value in tick.get("counts", {}).items()
              if name not in COUNT_ORDER]
    return ", ".join(parts)


def _tick_item(tick) -> str:
    counts = _counts_text(tick)
    return ('<li><span class="at machine-fact">%s</span><div><p>%s</p>%s</div></li>' % (
        html.escape(tick["at"][11:16]), html.escape(tick["line"]),
        '<p class="machine-fact">%s</p>' % html.escape(counts) if counts else ""))


def _render_actor(actor) -> str:
    esc = html.escape
    verdict = actor["verdict"]
    ticks = "".join(_tick_item(tick) for tick in actor["ticks"])
    if actor["last_at"]:
        last = "%s %sZ (%s)" % (actor["last_at"][:10], actor["last_at"][11:16],
                                _age_text(actor["age_seconds"]))
    else:
        last = _age_text(actor["age_seconds"])
    if actor["ticks"]:
        listing = '<ol class="recent-ticks">%s</ol>' % ticks
    else:
        listing = '<p class="empty-state">No tick could be read from its journal.</p>'
    return ('<article class="card%s"><h2>%s</h2>'
            '<p class="machine-fact"><span class="actor-status outcome-%s">%s</span> %s / %s</p>'
            '<p>last tick %s</p>'
            '%s</article>') % (
        " actor-down" if verdict == "down" else "", esc(actor["name"]),
        {"ok": "ok", "late": "degraded", "down": "failed"}.get(verdict, "none"),
        esc(verdict.upper() if verdict != "untracked" else "NO SET CADENCE"),
        esc(actor["role"]), esc(_cadence_text(actor)),
        esc(last), listing)


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
