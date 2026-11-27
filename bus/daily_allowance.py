"""Read-only daily free allowances plotted against a UTC target."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import pace, usage


PROVIDER_UNITS = {"openrouter": "requests", "workersai": "neurons"}
DAY_SECONDS = 24 * 3600


def _target(env: Mapping[str, str]) -> tuple[int, float] | None:
    try:
        hour = int(str(env.get("LOOM_DAILY_PACE_TARGET_HOUR", "")))
        share = float(str(env.get("LOOM_DAILY_PACE_TARGET_SHARE", "")))
    except ValueError:
        return None
    return (hour, share) if 1 <= hour <= 24 and 0 <= share <= 100 else None


def _row(provider: str, unit: str, source: Mapping[str, Any], now: float,
         target: tuple[int, float] | None, points: float) -> dict[str, Any]:
    result = {"provider": provider, "used": None, "limit": None, "unit": unit,
              "resets_at": None, "observed_at": None, "verdict": None,
              "expected_pct": None}
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

    result.update(used=used, limit=limit, resets_at=source["resets_at"],
                  observed_at=source["observed_at"])
    if target is not None and limit > 0:
        hour, share = target
        expected = share * min((now - start) / (hour * 3600), 1)
        used_pct = 100 * min(used / limit, 1)
        result.update(expected_pct=expected, verdict=pace.verdict(used_pct, expected, points))
    return result


def rows(records: Sequence[Mapping[str, Any]], now: float,
         env: Mapping[str, str]) -> list[dict[str, Any]]:
    """Return the two provider rows, with empty readings when data is unavailable."""
    providers = tuple(PROVIDER_UNITS)
    daily = {record["provider"]: record for record in records
             if record.get("provider") in providers and record.get("window") == "daily"}
    target, points = _target(env), pace.slack(env)
    return [_row(provider, unit, daily.get(provider, {}), now, target, points)
            for provider, unit in PROVIDER_UNITS.items()]
