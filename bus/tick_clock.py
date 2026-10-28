"""The brain tick's clock (loom#2104).

The tick budget was a rule the model judged for itself: docs/MUSE.md said
"Stop at 25 minutes" and nothing told the model how much time had passed.
Every Muse tick from 21:10 ET one evening deferred verify, failed, asks,
brief and triage "on budget", although the 23:10 ET tick finished in about
8 minutes, and untriaged rose to 102.

This module is the one clock every step reads: the tick record's
started_at (set by `bus.brain step lock`) against now. `bus.brain step`
prints elapsed_line first, `bus.brain step-budget` prints it alone,
status and resume print it at the top, and a note that defers on budget
before BUDGET_MINUTES is refused (budget_refusal). The per-step start and
end times live in the record's "times" list (bus.brain tick_step), and
footer turns them into the journal's "- Steps:" line.

minutes_used mirrors bus/sweep.py's function of the same name and meaning
(loom#2100), so the sweep's 20-of-25 guard and this one read one clock.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

TICK_MINUTES = 25
BUDGET_MINUTES = 20

# A note that defers work because the tick's time ran out. "budget" counts
# only in its tick-time sense ("on budget", "on the 20-minute budget",
# "tick budget", "time budget"): the API, App installation, per-repo read,
# lane and request budgets are counts, not the tick's clock, and a note
# naming one is never refused.
_TIME_BUDGET = re.compile(
    r"\b(?:on|over|past|tick(?:'s)?|time|minutes?|\d+[- ]?minutes?)[ \t]+(?:the[ \t]+)?"
    r"(?:(?:\d+[- ]?minutes?|tick(?:'s)?|time)[ \t]+)?budget\b", re.I)
_OUT_OF_TIME = re.compile(
    r"\b(?:out of time|no time left|ran out of time|time is up|time'?s up|"
    r"(?:over|past|at) the \d+[- ]?minute (?:limit|mark|line))\b", re.I)
_DEFERS = re.compile(r"\b(defer\w*|skip\w*|spent|ran out|next tick|postpon\w*|"
                     r"left for|not reached|no time)\b", re.I)
# A negated claim is no claim: "not budget", "no deferral", "without
# skipping". Taken out before the patterns above are read.
_NEGATED = re.compile(r"\b(?:not|no|never|without)[ \t]+(?:\w+[ \t]+){0,2}?"
                      r"(?:budget|defer\w*|skip\w*|postpon\w*)\b", re.I)


def stamp(value: Any) -> Optional[datetime]:
    """A tick record's UTC time ("2030-01-01T12:00:00Z"), or None."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def minutes_used(record: Dict[str, Any], now: datetime) -> Optional[float]:
    """Minutes since the tick's lock step, or None with no start recorded."""
    started = stamp(record.get("started_at"))
    if started is None:
        return None
    return (now - started).total_seconds() / 60.0


def elapsed_line(record: Dict[str, Any], now: datetime) -> str:
    """"elapsed N of 25 minutes (S seconds left)": N is whole minutes,
    rounded down, so a printed 20 is always a real 20 the budget guard
    agrees with."""
    used = minutes_used(record, now)
    if used is None:
        return ("elapsed unknown of %d minutes: no tick start recorded "
                "(run python3 -m bus.brain step lock first)" % TICK_MINUTES)
    used = max(used, 0.0)
    left = max(int(TICK_MINUTES * 60 - used * 60), 0)
    line = "elapsed %d of %d minutes (%d seconds left)" % (int(used), TICK_MINUTES, left)
    if used >= TICK_MINUTES:
        return line + ": the tick is over, journal and unlock now"
    if used >= BUDGET_MINUTES:
        return line + ": past %d, a step may be deferred on budget" % BUDGET_MINUTES
    return line


def budget_status(record: Dict[str, Any], now: datetime) -> str:
    """`bus.brain step-budget`: the elapsed line and whether a deferral on
    budget is allowed now."""
    used = minutes_used(record, now)
    if used is not None and used >= BUDGET_MINUTES:
        verdict = "budget: spent, a step may be deferred on budget"
    elif used is None:
        verdict = "budget: unknown, no deferral on budget without a tick start"
    else:
        verdict = ("budget: open, do the step; a deferral on budget is refused until "
                   "%d minutes" % BUDGET_MINUTES)
    return "%s\n%s" % (elapsed_line(record, now), verdict)


def claims_budget(note: Optional[str]) -> bool:
    """Whether `note` defers or skips work because the tick's time ran out.
    Another budget (API, App, per-repo read, lane) and a negation ("not
    budget", "no deferral") are no claim."""
    if not note:
        return False
    plain = _NEGATED.sub(" ", note)
    return bool((_TIME_BUDGET.search(plain) or _OUT_OF_TIME.search(plain))
                and _DEFERS.search(plain))


def budget_refusal(record: Dict[str, Any], note: Optional[str], now: datetime) -> Optional[str]:
    """Why a note that defers on budget is refused, else None. Refused while
    the tick has used under BUDGET_MINUTES, and with no start recorded."""
    if not claims_budget(note):
        return None
    used = minutes_used(record, now)
    if used is not None and used >= BUDGET_MINUTES:
        return None
    return ("refused: the note defers on budget, but %s; a step may be deferred on budget "
            "only at %d minutes or more, so do the step (python3 -m bus.brain step-budget "
            "reads the clock)" % (elapsed_line(record, now), BUDGET_MINUTES))


def with_elapsed(note: str, record: Dict[str, Any], now: datetime) -> str:
    """A budget deferral note with the real elapsed minutes on it."""
    used = minutes_used(record, now)
    if used is None or "[elapsed " in note:
        return note
    return "%s [elapsed %d of %d minutes]" % (note, int(used), TICK_MINUTES)


def durations(record: Dict[str, Any], now: datetime) -> List[Tuple[str, int, bool]]:
    """(step, seconds, still running) for each step in the record's times."""
    out: List[Tuple[str, int, bool]] = []
    for each in record.get("times") or []:
        if not isinstance(each, dict):
            continue
        start = stamp(each.get("started_at"))
        if start is None:
            continue
        end = stamp(each.get("ended_at"))
        out.append((str(each.get("step")), max(int(((end or now) - start).total_seconds()), 0),
                    end is None))
    return out


def _span(seconds: int) -> str:
    return "%dm%02ds" % (seconds // 60, seconds % 60)


def footer(record: Dict[str, Any], now: datetime) -> Optional[str]:
    """The journal's "- Steps:" line: each step's time and the elapsed line,
    so an audit sees where the tick's time went. None with no times."""
    spans = durations(record, now)
    if not spans:
        return None
    parts = ["%s %s%s" % (step, _span(sec), " (running)" if running else "")
             for step, sec, running in spans]
    return "- Steps: %s; %s" % (", ".join(parts), elapsed_line(record, now))
