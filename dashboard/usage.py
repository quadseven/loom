"""Validation, in-memory storage and the /usage view for station usage readings."""
from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 200:
        raise ValueError("body must be a list of usage readings")
    return [_validate_row(raw_row) for raw_row in raw]


def _validate_row(raw_row: Any) -> dict[str, Any]:
    if not isinstance(raw_row, dict):
        raise ValueError("reading must be an object")
    row = dict(raw_row)
    for key in ("provider", "window", "source", "as_of"):
        if not isinstance(row.get(key), str) or not row[key] or len(row[key]) > 200:
            raise ValueError("invalid " + key)
    try:
        stamp = datetime.fromisoformat(row["as_of"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid as_of") from None
    if stamp.tzinfo is None:
        raise ValueError("invalid as_of")
    lanes = row.get("lanes")
    if not isinstance(lanes, list) or any(not isinstance(lane, str) or len(lane) > 100 for lane in lanes):
        raise ValueError("invalid lanes")
    pct = row.get("used_pct")
    if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100):
        raise ValueError("invalid used_pct")
    reset = row.get("resets_at")
    if reset is not None and not isinstance(reset, str):
        raise ValueError("invalid resets_at")
    row["note"] = str(row.get("note") or "")[:1000]
    holds = row.get("holds")
    if holds is not None and (not isinstance(holds, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or len(k) > 100 or len(v) > 300
            for k, v in holds.items())):
        raise ValueError("invalid holds")
    row["holds"] = dict(holds or {})
    # Optional: when the provider wrote the reading, and its window length.
    if row.get("observed_at") is not None and _parse_time(row["observed_at"]) is None:
        row["observed_at"] = None
    minutes = row.get("window_minutes")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
                                or not 0 < minutes <= 60 * 24 * 366):
        row["window_minutes"] = None
    return row


class Store:
    def __init__(self):
        self.providers: dict[str, list[dict[str, Any]]] = {}

    def upload(self, raw: Any) -> dict[str, Any]:
        rows = validate(raw)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["provider"], []).append(row)
        self.providers.update(grouped)
        return {"providers": len(grouped), "readings": len(rows)}


# ---------------------------------------------------------------------------
# The Usage view (/usage): one row per provider, worst first. Every number
# carries its unit and the time it was read; a reading the dashboard cannot
# vouch for says so instead of showing a number.

STALE_AFTER = 2 * 3600
NEAR_LIMIT_PCT = 80.0
# A window this young has too little use behind it to project from.
MIN_PACE_FRACTION = 0.05
WINDOW_MINUTES = {"5-hour": 300, "weekly": 10080}
WINDOW_NAMES = {"5-hour": "5-hour", "weekly": "Weekly", "credit": "Credit",
                "all": "Usage", "last 429": "Last 429", "thermal hold": "Thermal hold"}
HOLD_UNTIL = re.compile(r";\s*(?:lane waits|unavailable) until (\S+)")
DOWN_WORDS = ("endpoint", "503", "unreachable", "down")
RECENT_PR_DAYS = 7


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else None


def _zone():
    name = os.environ.get("LOOM_DASHBOARD_TZ", "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc


def _clock(moment: datetime) -> str:
    local = moment.astimezone(_zone())
    return local.strftime("%Y-%m-%d %H:%M ") + (local.tzname() or "UTC")


def _span(seconds: float) -> str:
    """A duration in its two largest units: 6d 6h, 3h 12m, 8m."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes if minutes else "under 1m"


def _ago(moment: datetime, now: datetime) -> str:
    seconds = (now - moment).total_seconds()
    return "just now" if seconds < 60 else _span(seconds) + " ago"


def _until(moment: datetime, now: datetime) -> str:
    seconds = (moment - now).total_seconds()
    return "in " + _span(seconds) if seconds >= 60 else ("now" if seconds >= 0 else _span(-seconds) + " ago")


def _pct(value: float) -> str:
    return ("%.1f" % value).rstrip("0").rstrip(".") + "%"


def lane_state(reason: str, now: datetime) -> dict[str, Any]:
    """A lane's heartbeat hold reason as {kind: free|held|down, why, until}."""
    if not reason:
        return {"kind": "free", "why": "free", "until": None}
    text = re.sub(r"^limit:\s*", "", reason).strip()
    until = None
    match = HOLD_UNTIL.search(text)
    if match:
        until = _parse_time(match.group(1).rstrip(";"))
        if until:
            text = text[:match.start()].strip()
    kind = "down" if any(word in reason.lower() for word in DOWN_WORDS) else "held"
    why = text or kind
    if until:
        why += " until %s (%s)" % (_clock(until), _until(until, now) if until > now
                                   else "ended " + _ago(until, now))
    return {"kind": kind, "why": why, "until": until}


def lane_activity(prs: list[dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    """Each lane's newest model and its lane pull requests opened in the last
    RECENT_PR_DAYS days, from the runner's pull request footers."""
    from .views import footer
    out: dict[str, dict[str, Any]] = {}
    for pr in prs or []:
        parsed = footer(pr.get("body")) if isinstance(pr, dict) else None
        if not parsed:
            continue
        lane, model = parsed[2], parsed[3]
        when = _parse_time(str(pr.get("created_at") or pr.get("updated_at") or ""))
        info = out.setdefault(lane, {"model": None, "at": None, "recent": 0})
        if when and (info["at"] is None or when >= info["at"]):
            info["model"], info["at"] = model, when
        elif info["model"] is None:
            info["model"] = model
        if when and (now - when).total_seconds() <= RECENT_PR_DAYS * 86400:
            info["recent"] += 1
    return out


def _window(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One reading, judged: its figures, reset, pace and freshness."""
    window = str(row.get("window") or "")
    collected = _parse_time(row.get("as_of"))
    observed = _parse_time(row.get("observed_at")) or collected
    reset = _parse_time(row.get("resets_at"))
    used = row.get("used_pct")
    used = None if used is None or isinstance(used, bool) else max(0.0, min(100.0, float(used)))
    note = str(row.get("note") or "").strip()
    w = {"name": WINDOW_NAMES.get(window, window[:1].upper() + window[1:]), "kind": window,
         "lanes": [lane for lane in row.get("lanes") or [] if isinstance(lane, str)],
         "used": used, "reset": reset, "note": note, "observed": observed, "collected": collected,
         "stale": None, "pace": None, "runs_out": None, "at_reset": None, "error": note.startswith("adapter error")}
    if observed is not None and (now - observed).total_seconds() > STALE_AFTER:
        w["stale"] = "read %s ago" % _span((now - observed).total_seconds())
    if used is not None and reset is not None and observed is not None and reset <= now:
        # The window rolled over after the reading: its figure is history.
        w["stale"] = "the window reset %s, after this reading" % _until(reset, now)
    minutes = row.get("window_minutes") or WINDOW_MINUTES.get(window)
    if used is not None and reset is not None and observed is not None and minutes and not w["stale"]:
        length = float(minutes) * 60
        left = (reset - observed).total_seconds()
        elapsed = length - left
        if 0 < left <= length and elapsed >= length * MIN_PACE_FRACTION:
            rate = used / (elapsed / 3600)
            w["pace"] = rate
            if rate > 0 and used < 100:
                hours_to_full = (100 - used) / rate
                runs_out = observed + timedelta(hours=hours_to_full)
                if runs_out < reset:
                    w["runs_out"] = runs_out
                else:
                    w["at_reset"] = min(100.0, used + rate * left / 3600)
            elif used < 100:
                w["at_reset"] = used
    if row.get("unit") == "USD" and isinstance(row.get("remaining"), (int, float)):
        w["usd_left"] = float(row["remaining"])
    return w


def _provider(name: str, rows: list[dict[str, Any]], holds: dict[str, str],
              activity: dict[str, dict[str, Any]], models: dict[str, str], now: datetime) -> dict[str, Any]:
    lanes: list[str] = []
    for row in rows:
        for lane in row.get("lanes") or []:
            if lane not in lanes:
                lanes.append(lane)
    windows = [_window(row, now) for row in rows]
    states = {lane: lane_state(holds.get(lane, ""), now) for lane in lanes}
    groups: list[dict[str, Any]] = []
    for lane in lanes:
        model = (activity.get(lane) or {}).get("model") or models.get(lane) or ""
        key = (states[lane]["kind"], states[lane]["why"])
        group = next((g for g in groups if g["key"] == key), None)
        if group is None:
            group = {"key": key, "kind": key[0], "why": key[1], "models": [], "lanes": [], "recent": 0}
            groups.append(group)
        group["lanes"].append(lane)
        if model and model not in group["models"]:
            group["models"].append(model)
        group["recent"] += (activity.get(lane) or {}).get("recent", 0)
    free = sum(1 for s in states.values() if s["kind"] == "free")
    metered = [w for w in windows if w["used"] is not None and not w["stale"]]
    blocked = [s["kind"] for s in states.values() if s["kind"] != "free"]
    if lanes and not free:
        status, rank = ("down" if all(kind == "down" for kind in blocked) else "held"), 0
    elif any(w["used"] >= NEAR_LIMIT_PCT for w in metered):
        status, rank = "near limit", 0
    elif any(w["runs_out"] for w in metered):
        status, rank = "runs out early", 1
    elif any(w["error"] or (w["stale"] and w["used"] is not None) for w in windows):
        status, rank = "error" if any(w["error"] for w in windows) else "stale", 1
    elif blocked:
        status, rank = ("partly down" if all(kind == "down" for kind in blocked) else "partly held"), 2
    elif metered:
        status, rank = "ok", 3
    else:
        status, rank = "no meter", 4
    read = [w["collected"] for w in windows if w["collected"]]
    sources = []
    for row in rows:
        source = str(row.get("source") or "")
        if source and source not in sources:
            sources.append(source)
    return {"name": name, "lanes": lanes, "free": free, "groups": groups, "windows": windows,
            "status": status, "rank": rank, "top": max((w["used"] for w in metered), default=-1.0),
            "read": max(read) if read else None, "sources": sources}


def build(rows: list[dict[str, Any]], now: datetime, prs: list[dict[str, Any]] | None = None,
          models: dict[str, str] | None = None) -> dict[str, Any]:
    """The Usage view's data from the stored readings: providers worst first,
    the asks for the operator, and how fresh the snapshot is."""
    rows = [row for row in rows if isinstance(row, dict)]
    holds: dict[str, str] = {}
    for row in rows:
        if isinstance(row.get("holds"), dict):
            holds.update({k: v for k, v in row["holds"].items() if isinstance(v, str)})
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(str(row.get("provider") or "unknown"), []).append(row)
    activity = lane_activity(prs or [], now)
    providers = [_provider(name, items, holds, activity, models or {}, now)
                 for name, items in by_name.items()]
    providers.sort(key=lambda p: (p["rank"], -p["top"], p["name"]))
    read = [p["read"] for p in providers if p["read"]]
    newest = max(read) if read else None
    snapshot_stale = newest is not None and (now - newest).total_seconds() > STALE_AFTER
    asks: list[tuple[str, str]] = []
    if snapshot_stale:
        asks.append(("bad", "The usage snapshot is %s old (read %s): no station upload since, so "
                            "every figure here may be out of date." % (_span((now - newest).total_seconds()),
                                                                     _clock(newest))))
    for p in providers:
        stale = [w for w in p["windows"] if w["stale"] and w["used"] is not None]
        if stale and not snapshot_stale:
            asks.append(("warn", "%s %s: %s stale (%s)." % (
                p["name"], " and ".join(w["name"].lower() for w in stale),
                "reading is" if len(stale) == 1 else "readings are", stale[0]["stale"])))
        for g in p["groups"]:
            if g["kind"] != "free":
                asks.append(("bad", "%s: %s %s." % (p["name"], _count(len(g["lanes"]), "lane"),
                                                     g["why"] if g["kind"] == "held" else "down: " + g["why"])))
        for w in p["windows"]:
            label = "%s %s" % (p["name"], w["name"].lower())
            if w["stale"]:
                pass
            elif w["runs_out"] and w["used"] < NEAR_LIMIT_PCT:
                asks.append(("warn", "%s: at this pace it runs out %s, %s before its reset." % (
                    label, _until(w["runs_out"], now), _span((w["reset"] - w["runs_out"]).total_seconds()))))
            elif w["used"] is not None and w["used"] >= NEAR_LIMIT_PCT:
                asks.append(("bad", "%s is %s used%s." % (label, _pct(w["used"]), (
                    "; at this pace it runs out %s" % _until(w["runs_out"], now)) if w["runs_out"] else "")))
            if w["error"]:
                asks.append(("warn", "%s: the usage adapter failed (%s)." % (p["name"], w["note"])))
            elif w["kind"] == "credit" and w["used"] is None and "not configured" in w["note"]:
                asks.append(("info", "%s credit has no meter: the spend budget is not configured "
                                     "(LOOM_ANTHROPIC_BUDGET_USD)." % p["name"]))
    asks.sort(key=lambda a: ("bad", "warn", "info").index(a[0]))
    lanes = [lane for p in providers for lane in p["lanes"]]
    return {"providers": providers, "asks": asks, "now": now,
            "newest": newest,
            "lanes": len(lanes), "free": sum(p["free"] for p in providers),
            "held": sum(len(g["lanes"]) for p in providers for g in p["groups"] if g["kind"] == "held"),
            "down": sum(len(g["lanes"]) for p in providers for g in p["groups"] if g["kind"] == "down")}


def _count(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


STATUS_TONE = {"held": "bad", "down": "bad", "near limit": "bad", "runs out early": "warn",
               "stale": "warn", "error": "warn", "partly held": "warn", "partly down": "warn",
               "ok": "good", "no meter": "muted"}


def _meter(w: dict[str, Any], now: datetime) -> str:
    esc = html.escape
    name = '<span class="um-name">%s</span>' % esc(w["name"])
    detail: list[str] = []
    if w["used"] is None:
        if w["kind"] == "last 429" and w["reset"] is not None:
            limit = w["note"].replace("limitName:", "").strip() or "a"
            figure = "Unknown"
            detail.append("Last hit its %s limit; the provider said retry after %s (%s)."
                          % (limit, _clock(w["reset"]), _until(w["reset"], now)))
        else:
            figure = "Unknown"
            why = w["note"] or "not exposed"
            detail.append(why[:1].upper() + why[1:] + ("" if why.endswith(".") else "."))
        bar = '<div class="um-bar um-unknown" aria-hidden="true"></div>'
        return ('<div class="um um-none">%s%s<p class="um-fig">%s</p><p class="um-detail">%s</p></div>'
                % (name, bar, figure, esc(" ".join(detail))))
    used = w["used"]
    stale = bool(w["stale"])
    tone = "" if stale else " um-bad" if used >= NEAR_LIMIT_PCT else " um-warn" if w["runs_out"] else ""
    marker = ""
    if w["at_reset"] is not None and w["at_reset"] > used:
        marker = '<i class="um-proj" style="left:%.1f%%"></i>' % w["at_reset"]
    label = "%s used, %s left" % (_pct(used), _pct(100 - used))
    if stale:
        label += ", stale"
    bar = ('<div class="um-bar%s" role="img" aria-label="%s"><span style="width:%.1f%%"></span>%s</div>'
           % (" um-stale" if stale else "", esc(label), used, marker))
    figure = '<strong>%s</strong> used <span class="um-sep">/</span> <strong>%s</strong> left' % (
        _pct(used), _pct(100 - used))
    if "usd_left" in w:
        figure += ' <span class="um-usd">(USD %.2f left)</span>' % w["usd_left"]
    if w["reset"] is not None:
        verb = "Expires" if w["kind"] == "credit" else "Resets"
        detail.append("%s %s (%s)." % (verb, _clock(w["reset"]), _until(w["reset"], now)))
    elif w["kind"] != "credit":
        detail.append("Reset time unknown.")
    if stale:
        detail.append("Stale: %s." % w["stale"])
    elif w["runs_out"]:
        detail.append("Pace %s/h since the window opened: runs out %s (%s), before the reset."
                      % (_pct(w["pace"]), _clock(w["runs_out"]), _until(w["runs_out"], now)))
    elif w["pace"] is not None and w["at_reset"] is not None:
        detail.append("Pace %s/h since the window opened: lasts to the reset, about %s used by then."
                      % (_pct(w["pace"]), _pct(w["at_reset"])))
    elif w["used"] is not None and w["reset"] is not None and (w["kind"] in WINDOW_MINUTES):
        detail.append("Too early in the window to project a pace.")
    return ('<div class="um%s%s">%s%s<p class="um-fig">%s</p><p class="um-detail">%s</p></div>'
            % (tone, " um-is-stale" if stale else "", name, bar, figure, esc(" ".join(detail))))


def _holds(windows: list[dict[str, Any]], now: datetime) -> str:
    """Every thermal hold window of a provider as one line: one window per
    lane would repeat "none active" once per slot."""
    held = []
    for w in windows:
        if w["reset"] is not None and w["reset"] > now:
            held.append("%s until %s (%s)%s" % (", ".join(w["lanes"]), _clock(w["reset"]),
                                               _until(w["reset"], now), ": " + w["note"] if w["note"] else ""))
    figure = "; ".join(held) if held else "none active on %s" % _count(len(windows), "lane")
    return ('<div class="um um-hold%s"><span class="um-name">Thermal hold</span><p class="um-fig">%s</p></div>'
            % (" um-bad" if held else "", html.escape(figure)))


def _lane_groups(p: dict[str, Any]) -> str:
    esc = html.escape
    items = []
    for g in p["groups"]:
        lanes = ", ".join(g["lanes"])
        bits = [g["why"] if g["kind"] != "down" else "down: " + g["why"]]
        if g["models"]:
            bits.append(", ".join(g["models"]))
        bits.append("%s in %dd" % (_count(g["recent"], "PR") if g["recent"] else "no PRs", RECENT_PR_DAYS))
        items.append('<li class="ul ul-%s"><i class="ul-dot" aria-hidden="true"></i>'
                     '<span class="ul-names">%s</span><span class="ul-why">%s</span></li>'
                     % (g["kind"], esc(lanes), esc(" / ".join(bits))))
    if not items:
        return '<p class="ul-none">No lanes listed.</p>'
    return '<ul class="ul-list">%s</ul>' % "".join(items)


def _read_line(p: dict[str, Any], newest: datetime | None, now: datetime) -> str:
    """Where a provider's figures come from, and their read time when it is
    not the snapshot's (the page header carries that one)."""
    sources = ", ".join(p["sources"]) or "no source"
    if len(p["sources"]) > 3:
        sources = "%s and %d more" % (", ".join(p["sources"][:2]), len(p["sources"]) - 2)
    parts = ["Source: " + sources] if p["sources"] != ["not exposed"] else []
    if p["read"] is None:
        parts.append("read time unknown")
    elif newest is None or abs((newest - p["read"]).total_seconds()) >= 60:
        parts.append("read %s (%s)" % (_clock(p["read"]), _ago(p["read"], now)))
    observed = [w["observed"] for w in p["windows"] if w["observed"] and w["collected"]
                and (w["collected"] - w["observed"]).total_seconds() >= 60]
    if observed:
        parts.append("%s last reported %s (%s)" % (p["name"], _clock(min(observed)), _ago(min(observed), now)))
    return ("; ".join(parts) + ".") if parts else ""


def _provider_html(p: dict[str, Any], newest: datetime | None, now: datetime) -> str:
    esc = html.escape
    tone = STATUS_TONE.get(p["status"], "muted")
    lanes = "%d of %s free" % (p["free"], _count(len(p["lanes"]), "lane")) if p["lanes"] else "no lanes"
    head = ('<div class="up-head"><h3 class="up-name">%s</h3>'
            '<span class="up-status up-%s">%s</span>'
            '<span class="up-lanes">%s</span></div>'
            % (esc(p["name"]), tone, esc(p["status"]), esc(lanes)))
    holds = [w for w in p["windows"] if w["kind"] == "thermal hold"]
    meters = "".join(_meter(w, now) for w in p["windows"] if w["kind"] != "thermal hold")
    if holds:
        meters += _holds(holds, now)
    return ('<li class="up">%s<div class="up-body"><div class="up-meters">%s</div>%s'
            '<p class="up-source">%s</p></div></li>'
            % (head, meters, _lane_groups(p), esc(_read_line(p, newest, now))))


def _quiet_html(p: dict[str, Any], newest: datetime | None, now: datetime) -> str:
    """A provider with no usage figure and every lane free: one compact row."""
    esc = html.escape
    notes = []
    for w in p["windows"]:
        if w["kind"] == "last 429" and w["reset"] is not None:
            limit = w["note"].replace("limitName:", "").strip() or "a"
            notes.append("Last hit its %s limit; the provider said retry after %s (%s)."
                         % (limit, _clock(w["reset"]), _until(w["reset"], now)))
        elif w["kind"] == "thermal hold":
            continue
        else:
            why = w["note"] or "not exposed"
            note = why[:1].upper() + why[1:] + ("" if why.endswith(".") else ".")
            if note not in notes:
                notes.append(note)
    holds = [w for w in p["windows"] if w["kind"] == "thermal hold"]
    hold = _holds(holds, now) if holds else ""
    return ('<li class="uq"><div class="uq-head"><h3 class="up-name">%s</h3><span class="up-lanes">%s</span></div>'
            '<div class="uq-body">%s%s<p class="up-source">%s %s</p></div></li>'
            % (esc(p["name"]), esc("%d of %s free" % (p["free"], _count(len(p["lanes"]), "lane"))),
               hold, _lane_groups(p), esc(" ".join(notes)), esc(_read_line(p, newest, now))))


def render(data: dict[str, Any]) -> str:
    """The Usage view body."""
    esc = html.escape
    now = data["now"]
    providers = data["providers"]
    if not providers:
        return ('<p class="page-lede">How much of each provider limit is used and left, when it '
                'resets, and which lanes spend it.</p>'
                '<p class="empty-state">No usage snapshot has arrived since the dashboard started. '
                'The station uploads one on each pass (POST /api/usage); this page fills in after '
                'its next pass. If it stays empty, the station is not running or its upload fails.</p>')
    newest = data["newest"]
    parts = []
    if newest is None or (now - newest).total_seconds() > STALE_AFTER:
        when = "%s (%s)" % (_clock(newest), _ago(newest, now)) if newest else "an unknown time"
        parts.append('<p class="stale-note usage-fresh" role="status"><strong>Stale snapshot.</strong> '
                     'The newest usage reading is from %s. Every figure below is as of its own read '
                     'time; the station uploads a snapshot each pass, so it has stopped running or '
                     'uploading.</p>' % esc(when))
    else:
        parts.append('<p class="usage-fresh machine-fact"><i class="status-dot" aria-hidden="true"></i>'
                     'Snapshot read %s (%s). Readings older than %dh are marked stale.</p>'
                     % (esc(_clock(newest)), esc(_ago(newest, now)), STALE_AFTER // 3600))
    summary = "%d of %s can take work" % (data["free"], _count(data["lanes"], "lane"))
    held = []
    if data["held"]:
        held.append("%d held at a limit" % data["held"])
    if data["down"]:
        held.append("%d down" % data["down"])
    summary += (": " + " and ".join(held) + ".") if held else "."
    top = max((p for p in providers if p["top"] >= 0), key=lambda p: p["top"], default=None)
    if top:
        window = max((w for w in top["windows"] if w["used"] is not None and not w["stale"]),
                     key=lambda w: w["used"])
        summary += " Tightest meter: %s %s at %s used." % (top["name"], window["name"].lower(), _pct(window["used"]))
    parts.append('<p class="usage-summary">%s</p>' % esc(summary))
    if data["asks"]:
        items = "".join('<li class="ua ua-%s">%s</li>' % (tone, esc(text)) for tone, text in data["asks"])
        parts.append('<section class="usage-section" aria-labelledby="usage-asks"><h2 class="label" id="usage-asks">'
                     'Needs attention</h2><ul class="ua-list">%s</ul></section>' % items)
    else:
        parts.append('<section class="usage-section" aria-labelledby="usage-asks"><h2 class="label" id="usage-asks">'
                     'Needs attention</h2><p class="ua-clear">Nothing: no lane is held, no meter is near its '
                     'limit, and every reading is fresh.</p></section>')
    loud = [p for p in providers if p["status"] != "no meter"]
    quiet = [p for p in providers if p["status"] == "no meter"]
    if loud:
        parts.append('<section class="usage-section" aria-labelledby="usage-providers">'
                     '<div class="usage-section-head"><h2 class="label" id="usage-providers">Limits</h2>'
                     '<p class="usage-key machine-fact"><span class="key-used"></span>used '
                     '<span class="key-left"></span>left <span class="key-proj"></span>projected at reset</p></div>'
                     '<ul class="up-list">%s</ul></section>'
                     % "".join(_provider_html(p, newest, now) for p in loud))
    if quiet:
        parts.append('<section class="usage-section" aria-labelledby="usage-quiet">'
                     '<h2 class="label" id="usage-quiet">No usage meter</h2>'
                     '<p class="usage-note">These providers expose no usage figure, so how much is left is '
                     'unknown. Their lanes are free; a limit shows here when a lane hits one.</p>'
                     '<ul class="uq-list">%s</ul></section>'
                     % "".join(_quiet_html(p, newest, now) for p in quiet))
    return "".join(parts)
