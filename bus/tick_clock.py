"""One clock for the brain tick (loom#2105).

The tick record (bus/brain.py's step record on the lock commit) held only
`started_at`, the current step and the notes, and no command told the brain
how much time had passed, so the 20-minute budget line was judged by
guessing. This module is the single reading of that clock: how many whole
minutes of the 25-minute tick are used, the elapsed line every step prints
first, which step notes count as a deferral on the tick's time (refused
before 20 minutes), and the per-step times the journal footer lists.

`minutes_used(record, now)` has the same name and meaning as the function
in bus/sweep.py on the sweep branch, so both guards read the same clock;
after both merge, sweep can import it from here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

TICK_MINUTES = 25
BUDGET_MINUTES = 20

# A step or defer note defers on the tick's time only when it says one of
# these. Another budget is a count, not the clock: the per-repo read
# budget, the App installation budget, the GitHub API budget, a lane
# budget. Negations ("not budget", "no deferral") are no claim either.
_CLAIM_PHRASES = (
    "on budget",
    "on the 20-minute budget",
    "on the 20 minute budget",
    "tick budget",
    "time budget",
    "out of time",
    "time is up",
    "over the 20-minute limit",
    "over the 20 minute limit",
)
_OTHER_BUDGETS = ("per-repo", "per repo", "app installation", "github api", "lane budget")
_NEGATIONS = ("not budget", "no deferral", "not a budget")


def _stamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_used(record: Dict[str, Any], now: Any) -> int:
    """Whole seconds from the tick's start to `now`; 0 with no start."""
    started = _stamp((record or {}).get("started_at"))
    moment = _stamp(now)
    if started is None or moment is None:
        return 0
    return max(0, int((moment - started).total_seconds()))


def minutes_used(record: Dict[str, Any], now: Any) -> int:
    """Whole minutes of the tick used at `now`, rounded down, so a printed
    20 is a real 20."""
    return seconds_used(record, now) // 60


def elapsed_line(record: Dict[str, Any], now: Any) -> str:
    """The line every step prints first: `elapsed N of 25 minutes (S
    seconds left)`."""
    used = seconds_used(record, now)
    left = max(0, TICK_MINUTES * 60 - used)
    return "elapsed %d of %d minutes (%d seconds left)" % (used // 60, TICK_MINUTES, left)


def clock_line(record: Dict[str, Any], now: Any) -> str:
    """The elapsed line as status and resume print it, first."""
    return "Tick clock: " + elapsed_line(record, now)


def budget_deferral_allowed(record: Dict[str, Any], now: Any) -> bool:
    """A step may be deferred on budget only at 20 minutes or more."""
    return minutes_used(record, now) >= BUDGET_MINUTES


def is_budget_deferral(note: Optional[str]) -> bool:
    """Whether a step or defer note defers on the tick's time budget."""
    if not note:
        return False
    low = " ".join(str(note).lower().split())
    if any(neg in low for neg in _NEGATIONS):
        return False
    if any(other in low for other in _OTHER_BUDGETS):
        return False
    return any(phrase in low for phrase in _CLAIM_PHRASES)


def refused_note(note: str, record: Dict[str, Any], now: Any) -> str:
    """How a refused budget deferral is recorded on the step."""
    return "refused: budget deferral at elapsed %d of %d minutes: %s" % (
        minutes_used(record, now), TICK_MINUTES, note)


def allowed_note(note: str, record: Dict[str, Any], now: Any) -> str:
    """A budget deferral at 20 minutes or more, kept with its real elapsed."""
    return "%s [elapsed %d of %d minutes]" % (note, minutes_used(record, now), TICK_MINUTES)


def record_step_time(record: Dict[str, Any], step: str, at: Any) -> List[Dict[str, Any]]:
    """The tick record's `times` list after `step` runs at time `at`.

    Each entry is {step, started_at, ended_at}; the next step closes the
    previous one, `lock` starts the list over, and `unlock` ends at once.
    A record written before the clock has an empty list."""
    stamp = _iso(at) if isinstance(at, datetime) else str(at)
    if step == "lock":
        times: List[Dict[str, Any]] = []
    else:
        times = [dict(entry) for entry in (record or {}).get("times") or []]
    if times and times[-1].get("ended_at") is None:
        times[-1]["ended_at"] = stamp
    entry = {"step": step, "started_at": stamp,
             "ended_at": stamp if step == "unlock" else None}
    times.append(entry)
    return times


def _duration(entry: Dict[str, Any]) -> Optional[int]:
    started, ended = _stamp(entry.get("started_at")), _stamp(entry.get("ended_at"))
    if started is None or ended is None:
        return None
    return max(0, int((ended - started).total_seconds()))


def _span(seconds: int) -> str:
    return "%dm%02ds" % (seconds // 60, seconds % 60)


def step_times_line(record: Dict[str, Any]) -> Optional[str]:
    """The journal footer's Steps line for a tick record, or None when the
    record holds no step times: `- Steps: lock 0m10s, status 3m30s, ...;
    elapsed ...`."""
    times = (record or {}).get("times") or []
    parts, total = [], 0
    for entry in times:
        span = _duration(entry)
        if span is None:
            continue
        parts.append("%s %s" % (entry.get("step"), _span(span)))
        total += span
    if not parts:
        return None
    return "- Steps: %s; elapsed %s" % (", ".join(parts), _span(total))
