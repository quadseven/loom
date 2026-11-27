"""Read-only daily free allowances plotted against a UTC target."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from . import labels, pace, usage


PROVIDER_UNITS = {"openrouter": "requests", "workersai": "neurons"}
PROVIDER_LABELS = {"openrouter": "OpenRouter", "workersai": "Workers AI"}
DAY_SECONDS = 24 * 3600


def _target(hour_value: object, share_value: object) -> tuple[int, float] | None:
    try:
        hour = int(str(hour_value))
        share = float(str(share_value))
    except ValueError:
        return None
    return (hour, share) if 1 <= hour <= 24 and 0 <= share <= 100 else None


def _row(provider: str, unit: str, source: Mapping[str, Any], now: float,
         target: tuple[int, float] | None, points: float) -> dict[str, Any]:
    # Meter timestamps are stored as ISO datetimes with microsecond precision.
    now = round(now, 6)
    result: dict[str, Any] = {
        "provider": provider, "used": None, "limit": None, "unit": unit,
        "resets_at": None, "observed_at": None, "verdict": None, "expected_pct": None,
    }
    used = usage._daily_amount(source.get("used"))
    limit = usage._daily_amount(source.get("limit"))
    observed = usage._parse(source.get("observed_at"))
    reset = usage._parse(source.get("resets_at"))
    if used is None or limit is None or source.get("unit") != unit:
        return result
    if reset is None or observed is None or reset % DAY_SECONDS:
        return result
    start = reset - DAY_SECONDS
    if not start <= observed <= now < reset or now - observed > usage.STALE_AFTER:
        return result

    result.update(used=used, limit=limit, resets_at=usage._iso(reset),
                  observed_at=source["observed_at"])
    if target is not None and limit > 0:
        hour, share = target
        expected = share * min((now - start) / (hour * 3600), 1)
        used_pct = 100 * min(used / limit, 1)
        result.update(expected_pct=expected, verdict=pace.verdict(used_pct, expected, points))
    return result


def _rows(records: Sequence[Mapping[str, Any]], now: float,
          target: tuple[int, float] | None, points: float) -> list[dict[str, Any]]:
    providers = tuple(PROVIDER_UNITS)
    daily = {record["provider"]: record for record in records
             if record.get("provider") in providers and record.get("window") == "daily"}
    return [_row(provider, unit, daily.get(provider, {}), now, target, points)
            for provider, unit in PROVIDER_UNITS.items()]


def _settings(env: Mapping[str, str]) -> tuple[tuple[int, float] | None, float]:
    target = _target(env.get("LOOM_DAILY_PACE_TARGET_HOUR"), env.get("LOOM_DAILY_PACE_TARGET_SHARE"))
    return target, _points(pace.slack(env))


def rows(records: Sequence[Mapping[str, Any]], now: float,
         env: Mapping[str, str]) -> list[dict[str, Any]]:
    """Return the two provider rows, with empty readings when data is unavailable."""
    return _rows(records, now, *_settings(env))


def snapshot(records: Sequence[Mapping[str, Any]], now: float,
             env: Mapping[str, str]) -> list[dict[str, Any]]:
    """Pair each day's reading with the target the operator configured."""
    target, points = _settings(env)
    return [dict(row, **_target_fields(target), slack=points) for row in _rows(records, now, target, points)]


def _points(value: Any) -> float:
    points = usage._daily_amount(value)
    return pace.SLACK_DEFAULT if points is None else points


def _target_fields(target: tuple[int, float] | None) -> dict[str, Any]:
    hour, share = target if target is not None else (None, None)
    return {"target_hour": hour, "target_share": share}


def for_upload(records: Sequence[dict[str, Any]], allowances: Sequence[dict[str, Any]],
               now: float) -> list[dict[str, Any]]:
    """Add one summary per free provider to the existing provider-shaped upload."""
    pending = {row["provider"]: row for row in allowances}
    payload = []
    for record in records:
        allowance = pending.pop(record.get("provider"), None)
        payload.append(dict(record, daily_allowance=allowance) if allowance is not None else record)
    groups = usage._provider_groups(labels.LANES)
    for provider, allowance in pending.items():
        record = usage._record(provider, groups.get(provider, []), "all", None, None, None,
                               None, "daily allowance meter", now, "no reading")
        payload.append(dict(record, daily_allowance=allowance))
    return payload


def refresh(provider: str, source: Mapping[str, Any], now: float) -> dict[str, Any]:
    """Recheck a saved reading against the page clock and its published target."""
    target = _target(source.get("target_hour"), source.get("target_share"))
    current = _row(provider, PROVIDER_UNITS[provider], source, now, target, _points(source.get("slack")))
    return dict(current, **_target_fields(target))


def cells(row: Mapping[str, Any]) -> tuple[str, str, str | None, str | None]:
    """Shared provider, totals, UTC reset and pace text for both status surfaces."""
    provider = PROVIDER_LABELS[row["provider"]]
    if row["used"] is None:
        return provider, "no reading", None, None
    unit = "Neurons" if row["unit"] == "neurons" else row["unit"]
    amounts = "%s of %s %s" % (usage._amount(row["used"]), usage._amount(row["limit"]), unit)
    pace_text = None
    if row["verdict"] is not None:
        pace_text = "%s pace, target %s%% by %02d:00 UTC" % (
            row["verdict"], usage._amount(row["target_share"]), row["target_hour"])
    return provider, amounts, row["resets_at"], pace_text


def lines(allowances: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render the daily figures as a dedicated lane-status table."""
    return ["", "| daily free allowance | used | reset (UTC) | pace |", "|---|---|---|---|",
            *("| " + " | ".join(cell or "-" for cell in cells(row)) + " |" for row in allowances)]


def signature(allowances: Sequence[Mapping[str, Any]]) -> str:
    """Republish changed totals, reset or verdict, independent of clock drift."""
    keys = ("provider", "used", "limit", "resets_at", "verdict", "target_hour", "target_share")
    return json.dumps([[row[key] for key in keys] for row in allowances])
