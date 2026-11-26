"""A durable report queue and deadlines for one agent-driven brain tick."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Sequence

from . import tick_clock

WORK_STEPS = ("verify", "brief", "triage", "sweep")
_REF = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*")


def _read(path: Path) -> Dict[str, Any]:
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {"pending": []}
    if (not isinstance(state, dict) or not isinstance(state.get("pending"), list)
            or any(not isinstance(ref, str) or not _REF.fullmatch(ref)
                   for ref in state["pending"])):
        raise ValueError("invalid report queue; repair it without dropping pending reports")
    return state


def _save(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(state, out)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def plan(path: Path, record: Dict[str, Any], owed: Sequence[str], now: datetime,
         done: Optional[str] = None) -> Dict[str, Any]:
    """Offer one report until the cap, then timed turns for work.

    The lock holder calls this before drafting each report, and acknowledges
    it only after a successful post or a fresh read proving it no longer owed.
    An omitted goal (including a partial status read) never removes a report.
    Preserve the file across ticks; the lane wrapper saves its sandboxed
    working copy before cleaning the checkout and restores it afterward.
    """
    start = tick_clock.stamp(record.get("started_at"))
    if start is None or not record.get("holder") or now < start:
        raise ValueError("report planning requires the holder's started tick")
    if any(not isinstance(ref, str) or not _REF.fullmatch(ref)
           for ref in list(owed) + ([done] if done is not None else [])):
        raise ValueError("reports must name goal homes as OWNER/NAME#N")
    state = _read(path)
    pending = list(dict.fromkeys(state["pending"] + list(owed)))
    if done is not None:
        if done not in pending:
            raise ValueError("completed report is not in the pending queue")
        pending.remove(done)
    work_stop = start + timedelta(minutes=tick_clock.budget_minutes(record))
    tick = [record["holder"], record["started_at"]]
    if state.get("tick") != tick:
        seconds = min(tick_clock.tick_minutes(record) * 30,
                      max((work_stop - now).total_seconds(), 0) / 2)
        state.update(tick=tick, report_until=(now + timedelta(seconds=seconds)).isoformat())
    deadline = datetime.fromisoformat(state["report_until"])
    state["pending"] = pending
    _save(path, state)
    left = max(int((deadline - now).total_seconds()), 0)
    work_start = max(now, deadline) if pending and left else now
    seconds = max(int((work_stop - work_start).total_seconds()), 0) // len(WORK_STEPS)
    work = [{"step": step, "seconds": seconds,
             "until": (work_start + timedelta(seconds=seconds * (i + 1))).isoformat()}
            for i, step in enumerate(WORK_STEPS)]
    return {"report": pending[0] if pending and left else None,
            "report_until": deadline.isoformat(), "seconds_left": left,
            "pending": pending, "deferred": len(pending), "work": work,
            "journal_line": "- Reports deferred: %d" % len(pending)}
