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
import hashlib
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
# Repeats after which an incident with no named cause gets a cause ticket
# in its own repo (loom#2983).
CAUSE_AFTER = 3
CAUSE_TITLE = "Fix the failing base check named by a factory incident"
_COUNT_RE = re.compile(r"^Still failing: (\d+) times since ", re.M)
_CAUSE_RE = re.compile(r"^Cause:[ \t]*\S", re.M)
_REPO_RE = re.compile(r"^Repo:[ \t]*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)[ \t]*$", re.M)
CAUSE_DETAIL_MAX = 1500
# A private repo's incident names it "private repo <alias>" on the public
# factory repo (lanes/run_lane.sh incident_subject). The alias is the first 8
# hex digits of SHA-256 over this prefix and the repo's numeric GitHub id, so
# every lane host and the station derive the same one with no shared secret
# (loom#3573: the shared key in SSM never existed, so a cluster lane could not
# rebuild the title and close an incident another host filed). The id says
# nothing about the repo's name to anyone who cannot read the repo.
ALIAS_PREFIX = "loom factory incident alias v1\n"
# The incident kinds that describe the repo and its check, which a pass on any
# host closes; an environment incident describes the host that filed it.
REPO_KINDS = ("base-check", "contract")


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


def repo_alias(repo_id: int) -> str:
    """The 8-hex alias a private repo's incident title and Repo: line carry."""
    if isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id <= 0:
        raise ValueError("a repo id is a positive integer")
    return hashlib.sha256((ALIAS_PREFIX + str(repo_id)).encode()).hexdigest()[:8]


def subjects(repo: str, repo_id: Optional[int]) -> List[str]:
    """Every way an incident's Repo: line can name `repo`: by name (a public
    repo), or by the alias its GitHub id derives (a private one)."""
    found = [repo]
    if isinstance(repo_id, int) and not isinstance(repo_id, bool) and repo_id > 0:
        found.append("private repo %s" % repo_alias(repo_id))
    return found


def holds_repo(issue: Any, names: Sequence[str]) -> bool:
    """Whether `issue` is an open base-check or contract incident whose Repo:
    line is one of `names`: such an incident holds every ticket of its repo."""
    if not isinstance(issue, dict) or issue.get("state") != "open" or "pull_request" in issue:
        return False
    lines = str(issue.get("body") or "").splitlines()
    return (INCIDENT_MARKER in lines
            and any("Kind: %s" % kind in lines for kind in REPO_KINDS)
            and any("Repo: %s" % name in lines for name in names))


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


def _designates(comment: str, repo: str, number: int) -> bool:
    """Whether an incident comment has a "Fix: <ref>" line naming issue
    `number` of `repo`: a mention in passing designates nothing."""
    return any(_names(m.group(1), repo, number)
               for m in re.finditer(r"^Fix:[ \t]*(\S+)[ \t]*$", comment, re.M))


def is_fix(ref: str, ticket_key: str, body: Any, comments: Iterable[Any]) -> bool:
    """Whether the ticket `ticket_key` is the fix of incident `ref`: its body
    names the incident (its station status section aside, which names it on
    every held ticket), or a comment on the incident other than the
    runner's repeat count has a "Fix:" line naming the ticket."""
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
               and _designates(str(c.get("body") or ""), repo, int(number))
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


def repeat_count(comments: Iterable[Any]) -> int:
    """How many times the runner counted its base check failing on the
    incident, from its repeat comment; 0 when it has none."""
    for comment in comments:
        body = str(comment.get("body") or "") if isinstance(comment, dict) else ""
        if body.startswith(REPEAT_MARKER):
            found = _COUNT_RE.search(body)
            return int(found.group(1)) if found else 0
    return 0


def has_cause(body: Any) -> bool:
    """Whether the incident body already has a `Cause:` line."""
    return _CAUSE_RE.search(str(body or "")) is not None


def incident_repo(body: Any) -> str:
    """The repo an incident names on its `Repo:` line, empty for a private
    repo, whose incident names a withheld alias instead."""
    found = _REPO_RE.search(str(body or ""))
    return found.group(1) if found else ""


def cause_ticket(ref: str, body: Any) -> Tuple[str, str]:
    """The cause ticket's title and body: the incident's check line and error
    lines (the text after its `Ticket:` line), quoted."""
    lines = str(body or "").splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith("Ticket:")),
                 len(lines))
    detail = "\n".join(lines[start:]).strip()[:CAUSE_DETAIL_MAX] or "no error text"
    fence = "`" * 3
    text = ("## Why\nFactory incident %s has held this repo's tickets while its base check "
            "failed on the unmodified default branch, and nothing names the cause.\n\n"
            "Failing check and errors the incident records:\n\n%stext\n%s\n%s\n\n"
            "## What\nFind why the check fails on the default branch and fix it, so the "
            "incident closes.\n\n## Acceptance criteria\n- [ ] The repo's validation check "
            "passes on the default branch.\n\n## Size\nS\n"
            % (ref, fence, detail.replace(fence, "'''"), fence))
    return CAUSE_TITLE, text


def with_cause(body: Any, line: str) -> str:
    """The incident body with a `Cause:` line after its `Ticket:` line (at the
    end when it has none)."""
    lines = str(body or "").rstrip("\n").split("\n")
    at = next((i + 1 for i, text in enumerate(lines) if text.startswith("Ticket:")), len(lines))
    lines.insert(at, "Cause: " + line)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str]) -> int:
    """`alias <repo id>` prints the repo's incident alias (lanes/run_lane.sh)."""
    if len(argv) != 2 or argv[0] != "alias" or not argv[1].isdigit():
        print("usage: python3 -m bus.incident_probe alias <repo id>", file=sys.stderr)
        return 2
    try:
        print(repo_alias(int(argv[1])))
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
