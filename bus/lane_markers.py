"""The remote lane's claim and heartbeat comments: how each is written and read.

Each comment is a marker line Loom reads back, then a line a reader sees. A
marker-only comment renders empty on GitHub and still emails the operator a
blank notification (#3200), so every body built here carries both lines.
A reader matches the first line only, so a comment from before the visible
line (the marker alone) still reads as a claim or a heartbeat.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# One claim comment per ticket claim: the first time is the claim, and each
# heartbeat edits the same comment to add the newest beat as the third field
# and keep the claim time as the fourth. A comment from before that has the
# one time and no fourth field.
CLAIM_RE = re.compile(r"<!-- loom:remote-claim v1 ([^ ]+) ([0-9]+) ([0-9.]+)(?: ([0-9.]+))? -->")
HEARTBEAT_RE = re.compile(r"<!-- loom:remote-heartbeat v1 ([a-z0-9-]+) ([0-9.]+) -->")


def _first_line(body: Any) -> str:
    return str(body or "").strip().split("\n", 1)[0].strip()


def claim_match(body: Any) -> "Optional[re.Match[str]]":
    """The claim marker when it is the first line of `body`, else None."""
    return CLAIM_RE.fullmatch(_first_line(body))


def heartbeat_match(body: Any) -> "Optional[re.Match[str]]":
    """The heartbeat marker when it is the first line of `body`, else None."""
    return HEARTBEAT_RE.fullmatch(_first_line(body))


def claim_body(lane: str, number: int, beat: float, first: Optional[float] = None) -> str:
    tail = "" if first is None or first == beat else " %.3f" % first
    return ("<!-- loom:remote-claim v1 %s %d %.3f%s -->\nClaimed by remote lane %s."
            % (lane, number, beat, tail, lane))


def heartbeat_body(lane: str, now: float) -> str:
    return ("<!-- loom:remote-heartbeat v1 %s %.3f -->\nRemote lane %s is available."
            % (lane, now, lane))
