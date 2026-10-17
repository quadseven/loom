"""Validation and in-memory storage for station usage readings."""
from __future__ import annotations

from datetime import datetime
from typing import Any


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
