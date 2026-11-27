"""Pace each subscription to its window (loom#3630).

A subscription's pace is its used share of a window minus the share of that
window already elapsed, in percentage points. Behind by more than the slack
(LOOM_PACE_SLACK, default 10 points) means allowance that will be lost at the
reset; ahead by more than the slack means it runs out before the reset.

The station builds a plan each pass from its Usage readings (bus/usage.py):
per provider with a current windowed reading, the used and elapsed shares,
the verdict, the provider's slots running now (a fresh heartbeat), its most
slots (the registry's slots of that provider, or LOOM_PACE_MAX_<PROVIDER>),
and the slots it asks for: one more than running when it is behind and a
ticket waits for a free lane, never more than the most; the running count
otherwise, so a provider on or ahead of pace opens none. The plan is data in
state/usage/pace.json. The lane status reports subscription usage. Opening a slot
is the pod's to do: the lane container starts the slots its deployment
names, so nothing here starts or stops a runner.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import factory, usage

SLACK_DEFAULT = 10.0
BEHIND, ON, AHEAD = "behind", "on", "ahead"
PLAN_FILE = "pace.json"


def slack(env: Mapping[str, str]) -> float:
    """LOOM_PACE_SLACK in percentage points; a missing, unreadable or
    negative value is the default."""
    try:
        value = float(str(env.get("LOOM_PACE_SLACK") or "").strip())
    except ValueError:
        return SLACK_DEFAULT
    return value if value >= 0 else SLACK_DEFAULT


def verdict(used_pct: float, elapsed: float, points: float) -> str:
    """BEHIND, ON or AHEAD: used share minus elapsed share against the slack."""
    delta = used_pct - elapsed
    if delta < -points:
        return BEHIND
    if delta > points:
        return AHEAD
    return ON


def elapsed_pct(resets_at: Optional[float], window_minutes: Any, now: float) -> Optional[float]:
    """The share of the window that ends at RESETS_AT already elapsed at NOW,
    in percent; None when the window length is unknown or NOW is outside it."""
    if (resets_at is None or isinstance(window_minutes, bool)
            or not isinstance(window_minutes, (int, float)) or window_minutes <= 0):
        return None
    length = float(window_minutes) * 60
    start = resets_at - length
    if not start <= now < resets_at:
        return None
    return (now - start) / length * 100


def wanted(state: str, queued: int, open_slots: int, maximum: int) -> int:
    """The slots a provider asks for: one more than open when it is behind
    with queued work and below its most; else the open count."""
    if state == BEHIND and queued > 0 and open_slots < maximum:
        return open_slots + 1
    return open_slots


def queued(waits: Any) -> int:
    """Tickets the factory left waiting for a free lane (state/station/
    ticket-waits.json): the work another slot would take."""
    if not isinstance(waits, dict):
        return 0
    return sum(1 for info in waits.values()
               if isinstance(info, dict) and factory.no_lane(str(info.get("reason") or "")))


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _reading(row: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
    """A row's window, used and elapsed shares, or None when it gives no
    current figure for a window of known length."""
    used = _number(row.get("used_pct"))
    if used is None or usage.stale_reason(row, now):
        return None
    elapsed = elapsed_pct(usage._parse(row.get("resets_at")), row.get("window_minutes"), now)
    if elapsed is None:
        return None
    return {"window": str(row.get("window")), "minutes": float(row["window_minutes"]),
            "used": used, "elapsed": elapsed}


def _pick(rows: List[Dict[str, Any]], now: float) -> Dict[str, Dict[str, Any]]:
    """Per provider, the reading of its longest current window, with the
    slots the reading covers."""
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        provider, lanes = row.get("provider"), row.get("lanes")
        if not isinstance(provider, str) or not isinstance(lanes, list):
            continue
        reading = _reading(row, now)
        if reading is None:
            continue
        if provider not in best or reading["minutes"] > best[provider]["minutes"]:
            reading["lanes"] = [lane for lane in lanes if isinstance(lane, str)]
            best[provider] = reading
    return best


def _open_slots(home: str, slots: List[str], now: float) -> int:
    """The slots with a heartbeat fresher than usage.HEARTBEAT_FRESH: the
    ones a lane runner is running."""
    count = 0
    for slot in slots:
        try:
            beat = json.loads((Path(home) / "state" / "usage" / (slot + ".json")).read_text())
        except (OSError, ValueError):
            continue
        at = _number(beat.get("at")) if isinstance(beat, dict) else None
        if at is not None and 0 <= now - at <= usage.HEARTBEAT_FRESH:
            count += 1
    return count


def _maximum(provider: str, slots: List[str], env: Mapping[str, str]) -> int:
    name = "LOOM_PACE_MAX_" + provider.upper().replace("-", "_")
    try:
        value = int(str(env.get(name) or "").strip())
    except ValueError:
        return len(slots)
    return value if value >= 0 else len(slots)


def plan(home: str, rows: List[Dict[str, Any]], queued: int, env: Mapping[str, str],
         now: float) -> Dict[str, Any]:
    """This pass's pace plan from the Usage readings ROWS."""
    points = slack(env)
    providers = {}
    for provider, reading in sorted(_pick(rows, now).items()):
        open_slots = _open_slots(home, reading["lanes"], now)
        maximum = _maximum(provider, reading["lanes"], env)
        state = verdict(reading["used"], reading["elapsed"], points)
        providers[provider] = {
            "window": reading["window"], "used": reading["used"],
            "elapsed": round(reading["elapsed"], 1), "verdict": state,
            "open": open_slots, "max": maximum, "queued": queued,
            "want": wanted(state, queued, open_slots, maximum)}
    return {"at": now, "slack": points, "providers": providers}


def path(home: str) -> str:
    return os.path.join(home, "state", "usage", PLAN_FILE)


def write(home: str, record: Dict[str, Any]) -> None:
    target = path(home)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(tmp, target)


def read(home: str) -> Dict[str, Any]:
    """The last plan written, or an empty one."""
    try:
        with open(path(home), encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _providers(record: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    providers = record.get("providers")
    if not isinstance(providers, dict):
        return {}
    return {name: info for name, info in sorted(providers.items()) if isinstance(info, dict)}
