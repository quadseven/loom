"""Classify a lane heartbeat and share the station's published lane rows."""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import labels, spend, state_store

HEARTBEAT_MAX_AGE = 600
LIMIT = "limit:"
STATES = ("serving", "limited", "parked", "off")
RUNNER_UNAVAILABLE = "Runner reported unavailable"


def epoch(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e11:
        return float(value)
    return None


def fresh(at: float, now: float, window: float = HEARTBEAT_MAX_AGE) -> bool:
    # A runner can beat after the station pass started.
    return now - at <= window and at - max(now, time.time()) <= 60


@dataclass(frozen=True)
class State:
    state: str
    reason: str
    at: Optional[float]
    retry_at: Optional[float] = None

    @property
    def available(self) -> bool:
        return self.state == "serving"


def evaluate(beat: Any, now: float, *, window: float = HEARTBEAT_MAX_AGE,
             credit: Optional[spend.Summary] = None, parked: str = "",
             has_job: bool = True) -> State:
    """Return the lane state, a publishable reason and the heartbeat time."""
    beat = beat if isinstance(beat, dict) else {}
    at = epoch(beat.get("at"))
    if not has_job:
        return State("off", "No lane job", at)
    if credit is not None and credit.under_floor():
        why = "No Anthropic budget" if credit.budget.usd == 0 else "Anthropic credit below floor"
        return State("parked", why, at)
    if at is None:
        return State("off", "No heartbeat", at)
    if not fresh(at, now, window):
        return State("off", "Heartbeat is stale", at)
    if parked:
        return State("parked", parked, at)
    if beat.get("available") is True:
        return State("serving", "Accepting work", at)
    if beat.get("available") is not False:
        return State("off", "Heartbeat lacks availability", at)
    reason = str(beat.get("reason") or "")
    if reason.startswith(LIMIT):
        return _limit_state(reason, at, epoch(beat.get("retry_at")), now)
    return State("off", RUNNER_UNAVAILABLE, at)


def _limit_state(reason: str, at: float, until: Optional[float], now: float) -> State:
    if until is not None and until <= now:
        return State("off", "Limit ended, waiting for a heartbeat", at)
    why = "Out of credit" if "out of credit" in reason else "Runner reported a limit"
    return State("limited", why, at, until)


def heartbeat_fields(slot: str, beat: Dict[str, Any], home: str) -> Dict[str, Any]:
    """Add the shared state to a local or cluster runner's heartbeat."""
    now = beat["at"]
    credit = None
    parked = ""
    if labels.base_lane(slot) == "anthropic" and os.environ.get("LOOM_ANTHROPIC_BUDGET_USD"):
        try:
            budget = spend.Budget.from_env(os.environ)
        except ValueError:
            parked = "Invalid Anthropic budget"
        else:
            credit = spend.summarize(home, budget, now)
    state = evaluate(beat, now, credit=credit, parked=parked)
    if beat["available"] and not state.available:
        beat = {**beat, "available": False, "reason": state.reason}
    return {**beat, "state": state.state, "state_reason": state.reason}


ROW = re.compile(r"\|\s*([a-z0-9_.-]+)\s*\|\s*(serving|limited|parked|off|yes|no)\s*"
                 r"\|\s*([^|]+)\|(?:\s*([^|]+)\|)?", re.I)


def published_rows(body: str) -> list[dict[str, Any]]:
    """Read a station lane table, including older yes/no rows."""
    rows = []
    for line in (body or "").splitlines():
        match = ROW.fullmatch(line.rstrip())
        if match is None or match[1].lower() == "lane":
            continue
        lane, state, at, reason = match.groups()
        state = state.lower()
        row = {"lane": lane, "available": state in ("serving", "yes"), "heartbeat": at.strip()}
        if state in STATES:
            row.update(state=state, reason=(reason or "").strip())
        rows.append(row)
    return rows


def main() -> None:
    # The shell runner supplies its existing model-limit JSON and hold fields.
    import json

    slot, available, retry, models, from_limit_file = sys.argv[1:]
    home = os.environ["LOOM_HOME"]
    beat = {"lane": slot, "at": int(time.time()), "available": available == "true",
            "reason": sys.stdin.read(), "limited_models": json.loads("{" + models + "}")}
    if retry.isascii() and retry.isdigit():
        beat["retry_at"] = int(retry)
        if available == "false" and from_limit_file == "1":
            beat["from_limit_file"] = True
    path = os.path.join(home, "state", "usage", slot + ".json")
    state_store.FileStore().write_json(path, heartbeat_fields(slot, beat, home))


if __name__ == "__main__":
    main()
