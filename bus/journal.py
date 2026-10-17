"""The journal and report comment formats from docs/BRAIN.md."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Union

from . import text as _text
from .github import GitHub
from .handoff import _TICK

JOURNAL_MARKER = "<!-- loom:journal v1 -->"
REPORT_MARKER = "<!-- loom:report v1 -->"
# The brain's answer to an operator note; the station sends it to the
# Discord message it names (docs/LANES.md, "Talking to the brain").
REPLY_RE = re.compile(r"<!-- loom:reply v1 discord=([0-9]{1,20}) -->")


def _when(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _items(value: Union[str, Iterable[str], None]) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        return value.strip() or "none"
    parts = [str(v) for v in value]
    return ", ".join(parts) if parts else "none"


def render_journal(when, holder, shipped, briefed, waiting, asks,
                   requests, minutes, measure=None) -> str:
    head = [JOURNAL_MARKER, "Tick %s, %s" % (_when(when), holder)]
    if measure is not None:
        head.append("- Measure: %s" % _items(measure))
    return "\n".join(head + [
        "- Shipped: %s" % _items(shipped),
        "- Briefed: %s" % _items(briefed),
        "- Waiting: %s" % _items(waiting),
        "- Asks: %s" % _items(asks),
        "- Cost: %s requests, %s minutes" % (requests, minutes),
    ])


def render_report(when, holder, measure, shipped, in_flight, blocked,
                  next_step) -> str:
    return "\n".join([
        REPORT_MARKER,
        "Report %s, %s" % (_when(when), holder),
        "- Measure: %s" % _items(measure),
        "- Shipped: %s" % _items(shipped),
        "- In flight: %s" % _items(in_flight),
        "- Blocked: %s" % _items(blocked),
        "- Next: %s" % _items(next_step),
    ])


def with_tick(text: str, holder: Optional[str], now: Optional[datetime] = None) -> str:
    """A journal with its "Tick <UTC time>, <holder>" line, which is how a
    peer brain tells whose journal it is (handoff.journal_holder). A journal
    without one counts as another brain's, so each brain read its own last
    tick as a peer's and kept itself to resume items for hours (2026-10-16:
    no tick journaled that line, and no brain merged, briefed or triaged).
    With a holder the line is added after the marker when missing; a line
    naming a different holder, or no line and no holder, is refused."""
    found = _TICK.search(text)
    if found:
        if holder is not None and found.group(1) != holder:
            raise ValueError("the journal's Tick line names %s, not %s" % (found.group(1), holder))
        return text
    if holder is None:
        raise ValueError("a journal needs the line \"Tick <UTC time>, <holder>\" after the "
                         "marker, or pass --holder to add it (docs/BRAIN.md)")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", holder):
        raise ValueError("holder must be letters, digits, dot, dash or underscore")
    rest = text[len(JOURNAL_MARKER):].lstrip("\r\n")
    tick = "Tick %s, %s" % (_when(now or datetime.now(timezone.utc)), holder)
    return "%s\n%s\n%s" % (JOURNAL_MARKER, tick, rest)


def post(gh: GitHub, repo: str, issue_number: int, text: str,
         holder: Optional[str] = None):
    if not (text.startswith((JOURNAL_MARKER, REPORT_MARKER)) or REPLY_RE.match(text)):
        raise ValueError("comment must start with a loom journal, report or reply marker")
    if text.startswith(JOURNAL_MARKER):
        text = with_tick(text, holder)
    _text.check(text)
    return gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, int(issue_number)),
                   {"body": text})
