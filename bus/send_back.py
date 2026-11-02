"""The seven-day send-back comparison that closes loom#2162.

The weakened-test pre-check (bus/weakened.py) landed on the default branch with
#2170. Seven days after that merge the station counts three review rules over
the trailing week, the same count the Hot spots page shows, and compares them
with the week the ticket was filed. It posts the numbers on the ticket once, and
if any count has not fallen, the ticket reopens.

The numbers to beat are the filed week's 27, 18 and 13, the counts the ticket
says must fall. The 9, 0 and 6 it also names are the week before, the rise it
reported: comparing against those would reopen on any single weakened-test
finding, since nothing is below 0.

The three names are rules, the third field of a finding's first line, so the
count reads them through the page's own parser. They are never the second
field, which is a category such as stability.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping

from dashboard import hot_spots

# The ticket is an issue of the home repo, so only a station that serves that
# repo runs the comparison: elsewhere number 2162 is some other issue.
REPO = "quadseven/loom"
TICKET = 2162
# The squash merge of #2170 (245b0354163b), which put bus/weakened.py on main.
LANDED_AT = "2026-11-01T09:08:45Z"
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


def not_fallen(found: Mapping[str, int]) -> bool:
    """Whether any rule has not fallen below its count at filing."""
    return any(found[rule] >= FILED_COUNTS[rule] for rule in RULES)


def report(found: Mapping[str, int]) -> str:
    """The result comment: each rule, now against filing, the verdict and the
    marker."""
    lines = ", ".join("%s: %d (was %d)" % (rule, found[rule], FILED_COUNTS[rule])
                      for rule in RULES)
    verdict = ("At least one has not fallen, so this ticket reopens." if not_fallen(found)
               else "All three fell.")
    return "Seven-day send-back comparison: %s. %s\n%s" % (lines, verdict, MARKER)


def since(now: float) -> str:
    """The GitHub `since` stamp of the trailing week."""
    start = datetime.fromtimestamp(now, timezone.utc) - timedelta(seconds=WAIT)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")
