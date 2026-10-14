"""Collect locally available provider usage limits for the Usage view."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from . import labels, spend


def _record(provider: str, lanes: List[str], window: str, used_pct: Optional[float],
            remaining: Optional[float], unit: Optional[str], resets_at: Optional[str],
            source: str, now: float, note: Optional[str] = None) -> Dict[str, Any]:
    return {"provider": provider, "lanes": lanes, "window": window,
            "used_pct": used_pct, "remaining": remaining, "unit": unit,
            "resets_at": resets_at, "source": source,
            "as_of": datetime.fromtimestamp(now, timezone.utc).isoformat(), "note": note}


def _utc_epoch(value: Any, now: float, relative: bool = True) -> Optional[str]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(now + value if relative else value, timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except ValueError:
                try:
                    return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError):
                    return None
    return None


def _codex_limits(item: Any, now: float) -> Dict[str, Dict[str, Any]]:
    payload = item.get("payload", item) if isinstance(item, dict) else {}
    # The current codex CLI puts rate_limits beside payload.info (token
    # counts); older ones put it inside info. Look in both.
    limits = None
    for holder in (payload, payload.get("info") if isinstance(payload, dict) else None):
        if isinstance(holder, dict) and isinstance(holder.get("rate_limits"), dict):
            limits = holder["rate_limits"]
            break
    if not isinstance(limits, dict):
        return {}
    found = {}
    for key, label in (("primary", "5-hour"), ("secondary", "weekly")):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            reset = window.get("resets_at")
            relative = reset is None
            if reset is None:
                reset = window.get("resets_in_seconds")
            if reset is None and isinstance(window.get("window_minutes"), (int, float)):
                reset = window["window_minutes"] * 60
            found[label] = {"used": float(used), "reset": _utc_epoch(reset, now, relative)}
    return found


# The newest reading is at the end of the newest session file. Reading every
# session on every station pass parsed 4.4 GB (770 files) on the lane machine
# on 2026-10-14, so only the newest files are opened, and only their tails.
SESSION_FILES = 8
SESSION_TAIL = 512 * 1024


def _newest_sessions(sessions: Path) -> List[Path]:
    dated = []
    for path in sessions.glob("**/*.jsonl"):
        try:
            dated.append((path.stat().st_mtime, str(path), path))
        except OSError:
            continue
    dated.sort(reverse=True)
    return [path for _, _, path in dated[:SESSION_FILES]]


def _tail_lines(path: Path) -> List[str]:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - SESSION_TAIL))
        lines = stream.read().decode("utf-8", "replace").splitlines()
    # A tail that starts mid-file starts mid-line.
    return lines[1:] if size > SESSION_TAIL else lines


def _read_codex_sessions(sessions: Path, now: float) -> Dict[str, Dict[str, Any]]:
    """The rate-limit windows of the newest line that carries any, looking at
    the newest SESSION_FILES files, newest first, and only their tails."""
    for path in _newest_sessions(sessions):
        try:
            lines = _tail_lines(path)
        except OSError:
            continue
        for line in reversed(lines):
            try:
                found = _codex_limits(json.loads(line), now)
            except ValueError:
                continue
            if found:
                return found
    return {}


def codex_records(lanes: List[str], sessions: Path, now: float) -> List[Dict[str, Any]]:
    found = _read_codex_sessions(sessions, now)
    return [_record("codex", lanes, window, data["used"], None, "percent", data["reset"],
                    "codex session logs", now) for window, data in found.items()] or [
        _record("codex", lanes, "all", None, None, None, None, "codex session logs", now,
                "rate-limit fields not found")]


def anthropic_records(home: str, lanes: List[str], env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    budget = spend.Budget.from_env(env)
    summary = spend.summarize(home, budget, now)
    if budget.usd is None:
        return [_record("anthropic", lanes, "credit", None, None, "USD", None,
                        "bus/spend.py", now, "budget is not configured")]
    pct = min(100.0, max(0.0, summary.spent / budget.usd * 100)) if budget.usd else 100.0
    return [_record("anthropic", lanes, "credit", pct, summary.remaining, "USD",
                    _utc_epoch(budget.expires, now) if budget.expires else None,
                    "bus/spend.py", now, "floor USD %.2f" % budget.floor)]


def _not_exposed(provider: str, lanes: List[str], now: float) -> List[Dict[str, Any]]:
    return [_record(provider, lanes, "all", None, None, None, None,
                    "not exposed", now, "not exposed yet")]


def _provider_groups(lane_names: List[str]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for lane in lane_names:
        if lane in labels.REMOTE_LANES:
            provider = lane
        elif lane.startswith("anthropic"):
            provider = "anthropic"
        elif lane.startswith("codex"):
            provider = "codex"
        elif lane.startswith("opencode-go"):
            provider = "opencode-go"
        elif lane.startswith("spark"):
            provider = "spark"
        elif lane.startswith("claude"):
            provider = "claude"
        else:
            provider = lane
        grouped.setdefault(provider, []).append(lane)
    return grouped


def _collect_provider(provider: str, slots: List[str], registry: Dict[str, Any],
                      home: str, env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    adapter = registry.get(provider)
    if adapter:
        return adapter(home, slots, env, now)
    return _not_exposed(provider, slots, now)


def collect(home: str, env: Optional[Dict[str, str]] = None, now: Optional[float] = None,
            lane_names: Optional[List[str]] = None,
            sessions: Optional[Path] = None,
            adapters: Optional[Dict[str, Callable[[str, List[str], Dict[str, str], float], List[Dict[str, Any]]]]] = None) -> List[Dict[str, Any]]:
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    lane_names = labels.LANES if lane_names is None else lane_names
    grouped = _provider_groups(lane_names)
    records = []
    registry = {
        "anthropic": lambda _home, slots, settings, stamp: anthropic_records(_home, slots, settings, stamp),
        "codex": lambda _home, slots, settings, stamp: codex_records(
            slots, sessions or Path(settings.get("LOOM_CODEX_SESSIONS")
                                    or Path.home() / ".codex" / "sessions"), stamp),
        "opencode-go": _opencode_records,
        "spark": _spark_records,
    }
    registry.update(adapters or {})
    for provider, slots in grouped.items():
        try:
            records.extend(_collect_provider(provider, slots, registry, home, env, now))
        except Exception as err:
            records.append(_record(provider, slots, "all", None, None, None, None,
                                   "adapter", now, "adapter error: %s" % type(err).__name__))
    return records


def _opencode_records(home: str, slots: List[str], _env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    p = Path(home) / "state" / "usage"
    samples = []
    for slot in slots:
        try:
            row = json.loads((p / (slot + ".provider-limit.json")).read_text())
            samples.append(row)
        except (OSError, ValueError):
            pass
    if not samples:
        return [_record("opencode-go", slots, "all", None, None, None, None,
                        "lane probe", now, "not exposed")]
    row = max(samples, key=lambda x: x.get("at", 0))
    return [_record("opencode-go", slots, "last 429", None, None, None,
                    _utc_epoch(row.get("retry_after"), now), "lane probe", now,
                    "limitName: %s" % row.get("limit_name", "unknown"))]


def _spark_records(home: str, slots: List[str], _env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    path = Path(home) / "state" / "usage" / "spark.limit"
    try:
        until = float(path.read_text().split()[0])
        return [_record("spark", slots, "thermal hold", None, None, "seconds",
                        _utc_epoch(until, now), "state/usage/spark.limit", now)]
    except (OSError, ValueError, IndexError):
        return [_record("spark", slots, "thermal hold", None, None, None, None,
                        "state/usage/spark.limit", now, "no active hold")]


def write(home: str, records: List[Dict[str, Any]]) -> str:
    path = os.path.join(home, "state", "usage", "limits.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(records, stream, indent=2)
        stream.write("\n")
    os.replace(tmp, path)
    return path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.usage")
    parser.parse_args(argv)
    home = os.environ.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")
    records = collect(home)
    write(home, records)
    print("provider\tlanes\twindow\tused\tremaining\tresets_at\tnote")
    for row in records:
        print("%s\t%s\t%s\t%s\t%s\t%s\t%s" % (
            row["provider"], ",".join(row["lanes"]), row["window"],
            "unknown" if row["used_pct"] is None else "%.1f%%" % row["used_pct"],
            "unknown" if row["remaining"] is None else row["remaining"],
            row["resets_at"] or "unknown", row["note"] or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
