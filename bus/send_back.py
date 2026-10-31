"""The seven-day send-back comparison that closes loom#2162.

The weakened-test pre-check lands on the default branch as bus/weakened.py. Seven
days later the station counts three review rules over the trailing week, the
same count the Hot spots page shows, and compares them with the week the ticket
was filed. If any count has not fallen, the ticket reopens with the numbers.

The clock starts at the file's first commit, not at one pull request's merge,
because the work may land through a replacement pull request.

The three names are rules, the third field of a finding's first line, so the
count reads them through the page's own parser. They are never the second
field, which is a category such as stability.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from dashboard import hot_spots

TICKET = 2162
LANDED_PATH = "bus/weakened.py"
WAIT = 7 * 86400
STATE_FILE = "send-back-comparison-2162.json"
MARKER = "<!-- loom:send-back-comparison-2162 -->"
RULES = ("acceptance-criterion-unmet", "weakened-test", "intent-mismatch")
# Findings per rule in the 7 days to 2026-10-29, the week the ticket was filed.
FILED_COUNTS = {"acceptance-criterion-unmet": 27, "weakened-test": 18, "intent-mismatch": 13}


def counts(comments: Iterable[Mapping[str, Any]], bot: str, now: datetime) -> Dict[str, int]:
    """Each rule's review bot findings over the trailing seven days."""
    found = {rule: 0 for rule in RULES}
    for comment in comments:
        if not hot_spots._by_bot(comment, bot):
            continue
        if not hot_spots._within(comment, now - hot_spots.WINDOW, now):
            continue
        rule = hot_spots._rule(str(comment.get("body") or ""))
        if rule in found:
            found[rule] += 1
    return found


def landed_at(commits: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The stamp of the oldest commit listed (GitHub lists newest first)."""
    for commit in reversed(commits):
        stamp = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        if stamp:
            return str(stamp)
    return None


def not_fallen(found: Mapping[str, int]) -> bool:
    """Whether any rule has not fallen below its count at filing."""
    return any(found[rule] >= FILED_COUNTS[rule] for rule in RULES)


def report(found: Mapping[str, int]) -> str:
    """The reopen comment: each rule, now against filing, and the marker."""
    lines = ", ".join("%s: %d (was %d)" % (rule, found[rule], FILED_COUNTS[rule])
                      for rule in RULES)
    return "Seven-day send-back comparison: %s.\n%s" % (lines, MARKER)


def since(now: float) -> str:
    """The GitHub `since` stamp of the trailing week."""
    start = datetime.fromtimestamp(now, timezone.utc) - timedelta(seconds=WAIT)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")
