"""The per-pass view of GitHub state: reads, deadlines and request budget (loom#2458).

Provides PassView, which extends IssueReads into a single home for pass state:
- single-issue and paginated issue caching (IssueReads)
- multi-pass ticket comments and events caching with updated_at stamps
- pass deadline arming, disarming, and overrun detection
- GitHub request target and rate limit budget tracking, low budget warnings,
  and budget persistence
"""
from __future__ import annotations

from .issue_reads import (
    BUDGET_FLOOR,
    CACHE_KEEP,
    DEADLINE_MARKER_FILE,
    PASS_DEADLINE,
    PASS_REQUEST_TARGET,
    IssueReads,
    PassView,
    arm_deadline,
    deadline_stream,
    disarm_deadline,
    once,
    pass_deadline,
    pass_view,
    try_arm_deadline,
)

__all__ = [
    "BUDGET_FLOOR",
    "CACHE_KEEP",
    "DEADLINE_MARKER_FILE",
    "IssueReads",
    "PASS_DEADLINE",
    "PASS_REQUEST_TARGET",
    "PassView",
    "arm_deadline",
    "deadline_stream",
    "disarm_deadline",
    "once",
    "pass_deadline",
    "pass_view",
    "try_arm_deadline",
]
