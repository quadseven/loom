"""When an awaiting-live-check ticket's live check is due (loom#2920).

Nothing gave the `awaiting-live-check` label a due date, so no reader could
tell which of those tickets are overdue. The due date is a body line

    Live check due: YYYY-MM-DD

or, with no such line, the time the label was put on plus LIVE_CHECK_DAYS.
Pure functions: no reads, no clock of their own.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

LIVE_CHECK_DAYS = 7
SHOWN = 5
DUE_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?Live check due:[ \t]*(\d{4}-\d{2}-\d{2})[ \t]*$",
                    re.M | re.I)


def due_date(body: Optional[str], labelled_at: datetime) -> datetime:
    """The check's due date: the body's `Live check due:` line, else the time
    the label was put on plus LIVE_CHECK_DAYS."""
    found = DUE_RE.search(body or "")
    if found:
        try:
            return datetime.strptime(found.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return labelled_at + timedelta(days=LIVE_CHECK_DAYS)


def overdue(rows: Sequence[Tuple[datetime, Dict[str, Any]]], now: datetime
            ) -> List[Tuple[datetime, Dict[str, Any]]]:
    """The (due, issue) rows past their due date, oldest due first."""
    late = [row for row in rows if row[0] < now]
    return sorted(late, key=lambda row: (row[0], int(row[1]["number"])))


def section_lines(rows: Sequence[Tuple[datetime, Dict[str, Any]]], now: datetime) -> List[str]:
    """The `### Live checks overdue` section: the count past due and the
    SHOWN oldest by due date."""
    late = overdue(rows, now)
    lines = ["### Live checks overdue", "- %d overdue" % len(late)]
    lines += ["- #%s %s (due %s)" % (issue["number"], issue.get("title", ""),
                                     due.strftime("%Y-%m-%d"))
              for due, issue in late[:SHOWN]]
    return lines
