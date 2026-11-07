"""Ticket move rules and label calculations for state changes."""
from __future__ import annotations

from typing import Iterable, List, Optional

from . import stale_claims, tickets

# State names a ticket can carry in Loom.
STATES = (
    "queued",
    "briefed",
    "running",
    "pr",
    "answered",
    "failed",
    "merged",
    "shipped",
    "verified",
    "ready",
    "closed-unmerged",
)

STATE_LABELS = frozenset("loom:" + item for item in STATES + tickets.TICKET_STATES)


def _strip_prefix(name: str, prefix: str) -> str:
    """Return `name` without `prefix` when it starts with it."""
    return name[len(prefix):] if name.startswith(prefix) else name


def _drop(name: str, lane_given: bool) -> bool:
    """Return True if `name` is cleared on a ticket move."""
    if name in STATE_LABELS or name.startswith("wait:") or name == "needs-brain":
        return True
    return lane_given and name.startswith("lane:")


def next_labels(
    have: Iterable[str],
    state: str,
    lane: Optional[str] = None,
    wait: Optional[str] = None,
    own_logins: Iterable[str] = (),
) -> List[str]:
    """Return the labels after a ticket move.

    Every loom state label, wait:* label, and needs-brain is removed.
    When a lane is given, existing lane:* labels are removed.
    When the ticket leaves loom:running, this Loom's own claim:<login> labels
    are removed.
    Then loom:<state>, lane:<lane> (if given), and wait:<wait> (if given) are added.
    """
    clean_state = _strip_prefix(state, "loom:")
    lane_given = lane is not None
    have_list = list(have)
    kept = [x for x in have_list if not _drop(x, lane_given)]
    if "loom:running" in have_list and clean_state != "running":
        # The claim marks a run in progress; the run is over.
        kept = stale_claims.without_own_claim(kept, own_logins)
    kept.append("loom:" + clean_state)
    if lane:
        kept.append("lane:" + _strip_prefix(lane, "lane:"))
    if wait:
        kept.append("wait:" + _strip_prefix(wait, "wait:"))
    return kept
