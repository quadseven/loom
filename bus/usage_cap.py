"""Cap a subscription slot at a share of its weekly allowance (loom#3660).

A claude slot reads its own meter (state/usage/<slot>.meter.json, which
lanes/agent_cli.sh writes from the CLI's rate limit events on every run)
before it takes a run, and holds when its weekly use is at its stop, or ahead
of an even pace toward that stop by the reset. The rule is the OpenCode Go
plan's (bus/model_pick.py plan_stop and pace_catches_up) with the slot's own
stop and slack: LOOM_<SLOT>_USAGE_STOP (percent, default 90) and
LOOM_<SLOT>_USAGE_SLACK (points, default 5), each falling back to the lane's
setting. A slot that shares its account with a person keeps a lower stop, so
the person keeps the rest.

No reading (no meter, an unreadable one, or one from a window that already
reset) holds nothing: the run goes and records a reading. Use within a window
never falls, so a reading older than STALE still counts as a floor: a stop it
shows holds to the reset, and a pace it is ahead of holds to the time the pace
allows that floor. Either hold ends by the reset, so no reading can keep the
slot idle for good. A stop or slack setting that is not a number holds the
slot, so a mistyped stop never runs the account past what was meant.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from typing import Any, NamedTuple, Optional, Sequence, Tuple

from . import model_pick

STOP_DEFAULT = 90.0
SLACK_DEFAULT = model_pick.PACE_SLACK
WINDOW = "weekly"
# A reading older than this is only a floor: the note says how old it is.
STALE = 1800


class Verdict(NamedTuple):
    """`until` is None while the slot may run; `note` says why either way."""
    until: Optional[float]
    note: str


def setting(value: str, default: float, low: float, high: float) -> Optional[float]:
    """The setting as a number in [low, high]; the default when unset; None
    when it is set to anything else."""
    text = (value or "").strip()
    if not text:
        return default
    try:
        number = float(text)
    except ValueError:
        return None
    return number if low <= number <= high else None


def _epoch(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def reading(record: Any, now: float) -> Optional[Tuple[float, float, float]]:
    """The weekly (percent, reset, read at) of a meter record, or None when it
    has none for the window running now."""
    if not isinstance(record, dict) or not isinstance(record.get("windows"), list):
        return None
    for window in record["windows"]:
        if not isinstance(window, dict) or window.get("window") != WINDOW:
            continue
        percent, reset = window.get("used_pct"), _epoch(window.get("resets_at"))
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or reset is None:
            return None
        if reset <= now:
            return None
        at = _epoch(window.get("at")) or _epoch(record.get("at")) or now
        return float(percent), reset, at
    return None


def check(record: Any, now: float, stop: Optional[float], slack: Optional[float]) -> Verdict:
    """Whether a slot with this meter record may run now, and until when not."""
    if stop is None or slack is None:
        return Verdict(now + model_pick.RETRY_NOTHING,
                       "the slot's usage stop or slack setting is not a number in range")
    got = reading(record, now)
    if got is None:
        return Verdict(None, "no current %s reading; this run records one" % WINDOW)
    percent, reset, at = got
    age = now - at
    aged = " (read %d min ago)" % (age // 60) if age > STALE else ""
    held = model_pick.plan_stop({WINDOW: (percent, reset)}, now, {WINDOW: stop}, slack)
    if held:
        return Verdict(held[0], held[1] + aged)
    gone = min(1.0, max(0.0, 1 - (reset - now) / model_pick.PACED[WINDOW]))
    return Verdict(None, "%s usage %.0f%% is within its %.0f%% pace to a %.0f%% stop%s"
                   % (WINDOW, percent, stop * gone + slack, stop, aged))


def read_meter(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def main(argv: Sequence[str], now: Optional[float] = None) -> int:
    """check <meter file> <stop setting> <slack setting>: prints the note and
    exits 0 when the slot may run; prints "<until>\\t<why>" and exits 3 when
    it holds."""
    if len(argv) != 4 or argv[0] != "check":
        print("usage: python3 -m bus.usage_cap check <meter file> <stop> <slack>", file=sys.stderr)
        return 2
    at = time.time() if now is None else now
    verdict = check(read_meter(argv[1]), at, setting(argv[2], STOP_DEFAULT, 1, 100),
                    setting(argv[3], SLACK_DEFAULT, 0, 100))
    if verdict.until is None:
        print(verdict.note)
        return 0
    print("%d\t%s" % (verdict.until, verdict.note))
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
