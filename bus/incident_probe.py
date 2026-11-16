"""The base-check incident probe (loom#3084).

A base-check factory incident holds every ticket of its repo, and it closes
only when a lane's base check on that repo passes (docs/LANES.md). With every
ticket held, no lane ran a base check there, so the incident never closed:
three repos sat frozen for days until a person closed theirs by hand.

The station therefore gives one free lane a base-check-only brief for the
repo at most once every PROBE_EVERY (lanes/run_lane.sh, Deliverable:
base-check): a pass closes the incident through the runner's own close path,
a fail updates its repeat count. A ticket named as the incident's fix is not
held by it. The helpers here are the pure parts: what counts as an open
base-check incident, its last repeat, which ticket is its fix, and the brief.
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from . import status
from . import tickets

# How often one incident gets a base-check run, and how often the station
# reads it and its comments again.
PROBE_EVERY = 3600
# The station's record of each held incident: when it was last read and
# probed, when it opened, its last repeat, and when a probe last finished.
LEDGER = "incident_probe.json"
INCIDENT_MARKER = "<!-- loom:factory-incident v1 -->"
# The runner's running count on an open incident (lanes/run_lane.sh).
REPEAT_MARKER = "<!-- loom:incident-repeat v1 -->"
DELIVERABLE = "base-check"


def epoch(stamp: Any) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(str(stamp or ""), "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def open_base_check(issue: Any) -> bool:
    """Whether `issue` is an open base-check factory incident."""
    if not isinstance(issue, dict) or issue.get("state") != "open" or "pull_request" in issue:
        return False
    lines = str(issue.get("body") or "").splitlines()
    return INCIDENT_MARKER in lines and "Kind: base-check" in lines


def last_repeat(issue: Dict[str, Any], comments: Iterable[Any]) -> Optional[float]:
    """When a base check last failed on the incident: its repeat comment's
    last edit, else when it was filed."""
    for comment in comments:
        if isinstance(comment, dict) and str(comment.get("body") or "").startswith(REPEAT_MARKER):
            return epoch(comment.get("updated_at") or comment.get("created_at"))
    return epoch(issue.get("created_at"))


def _names(text: str, repo: str, number: int) -> bool:
    """Whether `text` names issue `number` of `repo`: owner/name#N, name#N
    or its issue URL."""
    owner, name = repo.split("/", 1)
    pattern = r"(?<![A-Za-z0-9._/-])(?:%s/)?%s#%d(?![0-9])|github\.com/%s/issues/%d(?![0-9])" % (
        re.escape(owner), re.escape(name), number, re.escape(repo), number)
    return re.search(pattern, text) is not None


def is_fix(ref: str, ticket_key: str, body: Any, comments: Iterable[Any]) -> bool:
    """Whether the ticket `ticket_key` is the fix of incident `ref`: its body
    names the incident (its station status section aside, which names it on
    every held ticket), or a comment on the incident other than the
    runner's repeat count names the ticket."""
    incident_repo, _, raw = ref.rpartition("#")
    repo, _, number = ticket_key.rpartition("#")
    if not raw.isdigit() or not number.isdigit():
        return False
    text = status.strip_body(body)
    if _names(text, incident_repo, int(raw)):
        return True
    if re.search(r"^Repairs incident:[ \t]*#%s[ \t]*$" % raw, text, re.M):
        return True
    return any(isinstance(c, dict) and not str(c.get("body") or "").startswith(REPEAT_MARKER)
               and _names(str(c.get("body") or ""), repo, int(number))
               for c in comments)


def with_repairs_header(body: Any, number: int) -> Optional[str]:
    """The body with a "Repairs incident: #N" brief header, which lets the
    lane run past the red base (lanes/run_lane.sh repair_baseline_allowed);
    None when it already has that header or names another incident."""
    text = str(body or "")
    try:
        headers = tickets.parse(text)[0]
    except ValueError:
        return None
    if "Repairs incident" in headers:
        return None
    first = text.split("\n", 1)[0].rstrip()
    gap = "\n" if tickets.HEADER_RE.fullmatch(first) else "\n\n"
    return "Repairs incident: #%d%s%s" % (number, gap, text)


def brief(repo: str, lane: str, ref: str, stamp: int) -> Tuple[str, str]:
    """The base-check brief's file name and text: the repo's default branch
    (no Base:), no ticket (no Issue:), and the incident it probes."""
    name = "%s-base-check-%d" % (repo.replace("/", "."), stamp)
    text = ("# Brief: %s\n\nRepo: %s\nBranch: loom/base-check-%d\nLane: %s\n"
            "Deliverable: %s\nIncident: %s\n\n"
            "Run only the base check on the default branch: a pass closes the "
            "incident, a fail updates it. No agent runs.\n"
            % (name, repo, stamp, lane, DELIVERABLE, ref))
    return name, text
