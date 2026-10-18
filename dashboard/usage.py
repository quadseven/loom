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


def _valid_holds(holds: Any) -> bool:
    return isinstance(holds, dict) and all(
        isinstance(k, str) and isinstance(v, str) and len(k) <= 100 and len(v) <= 300
        for k, v in holds.items())


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
    if holds is not None and not _valid_holds(holds):
        raise ValueError("invalid holds")
    row["holds"] = dict(holds or {})
    return _optional_fields(row)


def _optional_fields(row: dict[str, Any]) -> dict[str, Any]:
    """When the provider wrote the reading, and its window length: a bad
    value is dropped, not refused, since neither is needed to show it."""
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
    """A lane's heartbeat hold reason as {kind: free|held|down, why, until}.
    A hold whose until-time has passed is free: it ended, whatever the last
    heartbeat still says."""
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
    if until and until <= now:
        # The hold ran out and the lane has not written a heartbeat since (a
        # runner busy on a long run writes none): it no longer holds the lane.
        return {"kind": "free", "until": until,
                "why": "free: its hold (%s) ended %s (%s)" % (text or kind, _clock(until), _ago(until, now))}
    why = text or kind
    if until:
        why += " until %s (%s)" % (_clock(until), _until(until, now))
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


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def _staleness(used: float | None, reset: datetime | None, observed: datetime | None,
               now: datetime) -> str | None:
    """Why a reading cannot be taken as current, or None."""
    if observed is None:
        return None
    if used is not None and reset is not None and reset <= now:
        # The window rolled over after the reading: its figure is history.
        return "the window reset %s, after this reading" % _until(reset, now)
    if (now - observed).total_seconds() > STALE_AFTER:
        return "read %s ago" % _span((now - observed).total_seconds())
    return None


def _projection(used: float, reset: datetime, observed: datetime, minutes: float) -> dict[str, Any]:
    """The average pace since the window opened, and where it lands: the
    run-out time when that comes before the reset, else the use at reset."""
    length = float(minutes) * 60
    left = (reset - observed).total_seconds()
    elapsed = length - left
    if not 0 < left <= length or elapsed < length * MIN_PACE_FRACTION:
        return {}
    rate = used / (elapsed / 3600)
    if used >= 100:
        return {"pace": rate}
    if rate <= 0:
        return {"pace": rate, "at_reset": used}
    runs_out = observed + timedelta(hours=(100 - used) / rate)
    if runs_out < reset:
        return {"pace": rate, "runs_out": runs_out}
    return {"pace": rate, "at_reset": min(100.0, used + rate * left / 3600)}


def _window(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One reading, judged: its figures, reset, pace and freshness."""
    window = str(row.get("window") or "")
    collected = _parse_time(row.get("as_of"))
    observed = _parse_time(row.get("observed_at")) or collected
    reset = _parse_time(row.get("resets_at"))
    used = _number(row.get("used_pct"))
    note = str(row.get("note") or "").strip()
    w = {"name": WINDOW_NAMES.get(window, window[:1].upper() + window[1:]), "kind": window,
         "lanes": [lane for lane in row.get("lanes") or [] if isinstance(lane, str)],
         "used": used, "reset": reset, "note": note, "observed": observed, "collected": collected,
         "stale": _staleness(used, reset, observed, now), "pace": None, "runs_out": None,
         "at_reset": None, "error": note.startswith("adapter error")}
    minutes = row.get("window_minutes") or WINDOW_MINUTES.get(window)
    if used is not None and reset is not None and observed is not None and minutes and not w["stale"]:
        w.update(_projection(used, reset, observed, minutes))
    if row.get("unit") == "USD" and isinstance(row.get("remaining"), (int, float)):
        w["usd_left"] = float(row["remaining"])
    return w


def _unique(values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _group_lanes(lanes: list[str], states: dict[str, dict[str, Any]],
                 activity: dict[str, dict[str, Any]], models: dict[str, str]) -> list[dict[str, Any]]:
    """Lanes in the same state, with the models they ran and their recent
    pull requests: eight lanes out of credit read as one line."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for lane in lanes:
        key = (states[lane]["kind"], states[lane]["why"])
        group = groups.setdefault(key, {"kind": key[0], "why": key[1], "models": [], "lanes": [], "recent": 0})
        seen = activity.get(lane) or {}
        group["lanes"].append(lane)
        group["models"] = _unique(group["models"] + [seen.get("model") or models.get(lane) or ""])
        group["recent"] += seen.get("recent", 0)
    return list(groups.values())


def _status(lanes: list[str], kinds: list[str], windows: list[dict[str, Any]]) -> tuple[str, int]:
    """A provider's one-word status and its sort rank (worst first)."""
    blocked = [kind for kind in kinds if kind != "free"]
    metered = [w for w in windows if w["used"] is not None and not w["stale"]]
    all_down = all(kind == "down" for kind in blocked)
    checks = (
        (lanes and len(blocked) == len(lanes), "down" if all_down else "held", 0),
        (any(w["used"] >= NEAR_LIMIT_PCT for w in metered), "near limit", 0),
        (any(w["runs_out"] for w in metered), "runs out early", 1),
        (any(w["error"] for w in windows), "error", 1),
        (any(w["stale"] and w["used"] is not None for w in windows), "stale", 1),
        (blocked, "partly down" if all_down else "partly held", 2),
        (metered, "ok", 3),
    )
    return next(((status, rank) for hit, status, rank in checks if hit), ("no meter", 4))


def _provider(name: str, rows: list[dict[str, Any]], holds: dict[str, str],
              activity: dict[str, dict[str, Any]], models: dict[str, str], now: datetime) -> dict[str, Any]:
    lanes = _unique(lane for row in rows for lane in row.get("lanes") or [])
    windows = [_window(row, now) for row in rows]
    states = {lane: lane_state(holds.get(lane, ""), now) for lane in lanes}
    status, rank = _status(lanes, [s["kind"] for s in states.values()], windows)
    read = [w["collected"] for w in windows if w["collected"]]
    return {"name": name, "lanes": lanes, "windows": windows, "status": status, "rank": rank,
            "free": sum(1 for s in states.values() if s["kind"] == "free"),
            "groups": _group_lanes(lanes, states, activity, models),
            "top": max((w["used"] for w in windows if w["used"] is not None and not w["stale"]), default=-1.0),
            "read": max(read) if read else None,
            "sources": _unique(str(row.get("source") or "") for row in rows)}


def _lane_asks(p: dict[str, Any]) -> list[tuple[str, str]]:
    return [("bad", "%s: %s %s." % (p["name"], _count(len(g["lanes"]), "lane"),
                                    g["why"] if g["kind"] == "held" else "down: " + g["why"]))
            for g in p["groups"] if g["kind"] != "free"]


def _window_ask(name: str, w: dict[str, Any], now: datetime) -> list[tuple[str, str]]:
    """What one fresh reading asks of the operator: a full or fast meter, a
    failed adapter, a credit meter with no budget."""
    label = "%s %s" % (name, w["name"].lower())
    asks = []
    if w["stale"] or w["used"] is None:
        pass
    elif w["used"] >= NEAR_LIMIT_PCT:
        pace = "; at this pace it runs out %s" % _until(w["runs_out"], now) if w["runs_out"] else ""
        asks.append(("bad", "%s is %s used%s." % (label, _pct(w["used"]), pace)))
    elif w["runs_out"]:
        asks.append(("warn", "%s: at this pace it runs out %s, %s before its reset." % (
            label, _until(w["runs_out"], now), _span((w["reset"] - w["runs_out"]).total_seconds()))))
    if w["error"]:
        asks.append(("warn", "%s: the usage adapter failed (%s)." % (name, w["note"])))
    elif w["kind"] == "credit" and w["used"] is None and "not configured" in w["note"]:
        asks.append(("info", "%s credit has no meter: the spend budget is not configured "
                             "(LOOM_ANTHROPIC_BUDGET_USD)." % name))
    return asks


def _stale_ask(p: dict[str, Any]) -> list[tuple[str, str]]:
    stale = [w for w in p["windows"] if w["stale"] and w["used"] is not None]
    if not stale:
        return []
    return [("warn", "%s %s: %s stale (%s)." % (
        p["name"], " and ".join(w["name"].lower() for w in stale),
        "reading is" if len(stale) == 1 else "readings are", stale[0]["stale"]))]


def _asks(providers: list[dict[str, Any]], newest: datetime | None, now: datetime) -> list[tuple[str, str]]:
    """Everything that needs the operator, worst first. A stale snapshot is
    one ask, not one per reading."""
    snapshot_stale = newest is not None and (now - newest).total_seconds() > STALE_AFTER
    asks: list[tuple[str, str]] = []
    if snapshot_stale:
        asks.append(("bad", "The usage snapshot is %s old (read %s): no station upload since, so "
                            "every figure here may be out of date."
                     % (_span((now - newest).total_seconds()), _clock(newest))))
    for p in providers:
        if not snapshot_stale:
            asks += _stale_ask(p)
        asks += _lane_asks(p)
        for w in p["windows"]:
            asks += _window_ask(p["name"], w, now)
    return sorted(asks, key=lambda a: ("bad", "warn", "info").index(a[0]))


def _all_holds(rows: list[dict[str, Any]]) -> dict[str, str]:
    holds: dict[str, str] = {}
    for row in rows:
        if isinstance(row.get("holds"), dict):
            holds.update({k: v for k, v in row["holds"].items() if isinstance(v, str)})
    return holds


def build(rows: list[dict[str, Any]], now: datetime, prs: list[dict[str, Any]] | None = None,
          models: dict[str, str] | None = None) -> dict[str, Any]:
    """The Usage view's data from the stored readings: providers worst first,
    the asks for the operator, and how fresh the snapshot is."""
    rows = [row for row in rows if isinstance(row, dict)]
    holds = _all_holds(rows)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(str(row.get("provider") or "unknown"), []).append(row)
    activity = lane_activity(prs or [], now)
    providers = sorted((_provider(name, items, holds, activity, models or {}, now)
                        for name, items in by_name.items()),
                       key=lambda p: (p["rank"], -p["top"], p["name"]))
    newest = max((p["read"] for p in providers if p["read"]), default=None)
    groups = [g for p in providers for g in p["groups"]]
    return {"providers": providers, "asks": _asks(providers, newest, now), "now": now, "newest": newest,
            "lanes": sum(len(p["lanes"]) for p in providers), "free": sum(p["free"] for p in providers),
            "held": sum(len(g["lanes"]) for g in groups if g["kind"] == "held"),
            "down": sum(len(g["lanes"]) for g in groups if g["kind"] == "down")}


def _count(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


STATUS_TONE = {"held": "bad", "down": "bad", "near limit": "bad", "runs out early": "warn",
               "stale": "warn", "error": "warn", "partly held": "warn", "partly down": "warn",
               "ok": "good", "no meter": "muted"}


def _sentence(text: str) -> str:
    text = text or "not exposed"
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def _unknown_note(w: dict[str, Any], now: datetime) -> str:
    """Why a reading has no figure, in words."""
    if w["kind"] == "last 429" and w["reset"] is not None:
        limit = w["note"].replace("limitName:", "").strip() or "a"
        return ("Last hit its %s limit; the provider said retry after %s (%s)."
                % (limit, _clock(w["reset"]), _until(w["reset"], now)))
    return _sentence(w["note"])


def _unknown_meter(w: dict[str, Any], now: datetime) -> str:
    return ('<div class="um um-none"><span class="um-name">%s</span>'
            '<div class="um-bar um-unknown" aria-hidden="true"></div>'
            '<p class="um-fig">Unknown</p><p class="um-detail">%s</p></div>'
            % (html.escape(w["name"]), html.escape(_unknown_note(w, now))))


def _meter_detail(w: dict[str, Any], now: datetime) -> str:
    """The reset, then the reading's staleness or its pace and projection."""
    detail = []
    if w["reset"] is not None:
        verb = "Expires" if w["kind"] == "credit" else "Resets"
        detail.append("%s %s (%s)." % (verb, _clock(w["reset"]), _until(w["reset"], now)))
    elif w["kind"] != "credit":
        detail.append("Reset time unknown.")
    if w["stale"]:
        detail.append("Stale: %s." % w["stale"])
    elif w["runs_out"]:
        detail.append("Pace %s/h since the window opened: runs out %s (%s), before the reset."
                      % (_pct(w["pace"]), _clock(w["runs_out"]), _until(w["runs_out"], now)))
    elif w["pace"] is not None and w["at_reset"] is not None:
        detail.append("Pace %s/h since the window opened: lasts to the reset, about %s used by then."
                      % (_pct(w["pace"]), _pct(w["at_reset"])))
    elif w["reset"] is not None and w["kind"] in WINDOW_MINUTES:
        detail.append("Too early in the window to project a pace.")
    return " ".join(detail)


def _meter(w: dict[str, Any], now: datetime) -> str:
    """One window: a used/left bar with the projected use at reset, its
    figures with units, and the reset and pace in words."""
    if w["used"] is None:
        return _unknown_meter(w, now)
    esc = html.escape
    used, stale = w["used"], bool(w["stale"])
    tone = "" if stale else " um-bad" if used >= NEAR_LIMIT_PCT else " um-warn" if w["runs_out"] else ""
    marker = ""
    if w["at_reset"] is not None and w["at_reset"] > used:
        marker = '<i class="um-proj" style="left:%.1f%%"></i>' % w["at_reset"]
    label = "%s used, %s left%s" % (_pct(used), _pct(100 - used), ", stale" if stale else "")
    figure = '<strong>%s</strong> used <span class="um-sep">/</span> <strong>%s</strong> left' % (
        _pct(used), _pct(100 - used))
    if "usd_left" in w:
        figure += ' <span class="um-usd">(USD %.2f left)</span>' % w["usd_left"]
    return ('<div class="um%s%s"><span class="um-name">%s</span>'
            '<div class="um-bar%s" role="img" aria-label="%s"><span style="width:%.1f%%"></span>%s</div>'
            '<p class="um-fig">%s</p><p class="um-detail">%s</p></div>'
            % (tone, " um-is-stale" if stale else "", esc(w["name"]), " um-stale" if stale else "",
               esc(label), used, marker, figure, esc(_meter_detail(w, now))))


def _holds(windows: list[dict[str, Any]], now: datetime) -> str:
    """Every thermal hold window of a provider as one line: one window per
    lane would repeat "none active" once per slot."""
    held = ["%s until %s (%s)%s" % (", ".join(w["lanes"]), _clock(w["reset"]), _until(w["reset"], now),
                                    ": " + w["note"] if w["note"] else "")
            for w in windows if w["reset"] is not None and w["reset"] > now]
    figure = "; ".join(held) if held else "none active on %s" % _count(len(windows), "lane")
    return ('<div class="um um-hold%s"><span class="um-name">Thermal hold</span><p class="um-fig">%s</p></div>'
            % (" um-bad" if held else "", html.escape(figure)))


def _lane_groups(p: dict[str, Any]) -> str:
    esc = html.escape
    items = []
    for g in p["groups"]:
        bits = [g["why"] if g["kind"] != "down" else "down: " + g["why"]]
        if g["models"]:
            bits.append(", ".join(g["models"]))
        bits.append("%s in %dd" % (_count(g["recent"], "PR") if g["recent"] else "no PRs", RECENT_PR_DAYS))
        items.append('<li class="ul ul-%s"><i class="ul-dot" aria-hidden="true"></i>'
                     '<span class="ul-names">%s</span><span class="ul-why">%s</span></li>'
                     % (g["kind"], esc(", ".join(g["lanes"])), esc(" / ".join(bits))))
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


def _free_line(p: dict[str, Any]) -> str:
    return "%d of %s free" % (p["free"], _count(len(p["lanes"]), "lane")) if p["lanes"] else "no lanes"


def _provider_html(p: dict[str, Any], newest: datetime | None, now: datetime) -> str:
    esc = html.escape
    head = ('<div class="up-head"><h3 class="up-name">%s</h3>'
            '<span class="up-status up-%s">%s</span>'
            '<span class="up-lanes">%s</span></div>'
            % (esc(p["name"]), STATUS_TONE.get(p["status"], "muted"), esc(p["status"]), esc(_free_line(p))))
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
    holds = [w for w in p["windows"] if w["kind"] == "thermal hold"]
    notes = _unique(_unknown_note(w, now) for w in p["windows"] if w["kind"] != "thermal hold")
    return ('<li class="uq"><div class="uq-head"><h3 class="up-name">%s</h3><span class="up-lanes">%s</span></div>'
            '<div class="uq-body">%s%s<p class="up-source">%s %s</p></div></li>'
            % (esc(p["name"]), esc(_free_line(p)), _holds(holds, now) if holds else "",
               _lane_groups(p), esc(" ".join(notes)), esc(_read_line(p, newest, now))))


def _freshness_html(newest: datetime | None, now: datetime) -> str:
    esc = html.escape
    if newest is None or (now - newest).total_seconds() > STALE_AFTER:
        when = "%s (%s)" % (_clock(newest), _ago(newest, now)) if newest else "an unknown time"
        return ('<p class="stale-note usage-fresh" role="status"><strong>Stale snapshot.</strong> '
                'The newest usage reading is from %s. Every figure below is as of its own read '
                'time; the station uploads a snapshot each pass, so it has stopped running or '
                'uploading.</p>' % esc(when))
    return ('<p class="usage-fresh machine-fact"><i class="status-dot" aria-hidden="true"></i>'
            'Snapshot read %s (%s). Readings older than %dh are marked stale.</p>'
            % (esc(_clock(newest)), esc(_ago(newest, now)), STALE_AFTER // 3600))


def _summary(data: dict[str, Any]) -> str:
    """One sentence: lanes that can work, why the rest cannot, and the
    fullest fresh meter."""
    held = (["%d held at a limit" % data["held"]] if data["held"] else []) + \
           (["%d down" % data["down"]] if data["down"] else [])
    text = "%d of %s can take work%s" % (data["free"], _count(data["lanes"], "lane"),
                                         (": " + " and ".join(held) + ".") if held else ".")
    fresh = [(w["used"], p["name"], w["name"]) for p in data["providers"] for w in p["windows"]
             if w["used"] is not None and not w["stale"]]
    if fresh:
        used, name, window = max(fresh, key=lambda item: item[0])
        text += " Tightest meter: %s %s at %s used." % (name, window.lower(), _pct(used))
    return '<p class="usage-summary">%s</p>' % html.escape(text)


def _asks_html(asks: list[tuple[str, str]]) -> str:
    head = '<section class="usage-section" aria-labelledby="usage-asks"><h2 class="label" id="usage-asks">Needs attention</h2>'
    if not asks:
        return head + ('<p class="ua-clear">Nothing: no lane is held, no meter is near its limit, '
                       'and every reading is fresh.</p></section>')
    items = "".join('<li class="ua ua-%s">%s</li>' % (tone, html.escape(text)) for tone, text in asks)
    return head + '<ul class="ua-list">%s</ul></section>' % items


def render(data: dict[str, Any]) -> str:
    """The Usage view body."""
    now, newest, providers = data["now"], data["newest"], data["providers"]
    if not providers:
        return ('<p class="page-lede">How much of each provider limit is used and left, when it '
                'resets, and which lanes spend it.</p>'
                '<p class="empty-state">No usage snapshot has arrived since the dashboard started. '
                'The station uploads one on each pass (POST /api/usage); this page fills in after '
                'its next pass. If it stays empty, the station is not running or its upload fails.</p>')
    parts = [_freshness_html(newest, now), _summary(data), _asks_html(data["asks"])]
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
