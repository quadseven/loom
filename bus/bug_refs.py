"""Check the bugs a lane says it discovered against real issues.

A lane may write a ``## Bugs discovered`` section in its Intent summary
(``.loom/summary.md``), one bug per line. A line that names no issue is a
dead end nobody triages, and a model can invent one that reads as real, so
each line must cite an issue: ``#N`` in the pull request's own repo, or
OWNER/NAME#N. The station looks each cited issue up and keeps the line only
when the issue exists, is an issue (not a pull request) and is open or was
filed in the last two days (the lane may have filed it this run). A failed
lookup rejects the line: an unverified claim never reaches a body.

``lanes/run_lane.sh::pr_body`` leaves a rejected line out of a new pull
request body and logs it; ``bus/pr_repair.py`` refuses a fix round's repair
while any line is rejected, asking the lane to file the issue and cite it,
or to drop the line.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

# The section heading a body carries, and the spellings a lane may use.
HEADING = "Bugs discovered"
_HEADINGS = ("bugs discovered", "bugs found", "found bugs", "discovered bugs")
_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_ANY_HEADING = re.compile(r"^#+ ")
# An OWNER/NAME#N citation, or a bare #N meaning the pull request's own repo.
_FULL_REF = re.compile(r"(?<![\w/.-])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#([0-9]+)\b")
_OWN_REF = re.compile(r"(?<![\w/#-])#([0-9]+)\b")
# How old a closed issue may be and still count as the lane's own report.
RECENT = dt.timedelta(days=2)
MAX_LINE = 300
_NONE = {"none", "none.", "no bugs", "none found", "n/a"}
_BULLET = re.compile(r"^(?:[-*+][ \t]+|[0-9]+[.)][ \t]+)")

Lookup = Callable[[str, int], Dict[str, object]]


class Rejected(NamedTuple):
    """One rejected line: its 1-based position in the section and why. The
    line's own text is never repeated, since the reason may reach a log or a
    comment that the text has not been checked for."""
    position: int
    why: str


def section(summary: str) -> List[str]:
    """The non-blank lines of the summary's first bugs section, without
    their bullets; [] when it has none or says only "none"."""
    out: List[str] = []
    on = False
    for line in summary.replace("\r\n", "\n").split("\n"):
        m = _HEADING_RE.match(line)
        if not on and m and m.group(1).strip().lower() in _HEADINGS:
            on = True
            continue
        if on and _ANY_HEADING.match(line):
            break
        if on and line.strip():
            out.append(_BULLET.sub("", line.strip()))
    if len(out) == 1 and out[0].lower() in _NONE:
        return []
    return out


def citations(line: str, repo: str) -> List[Tuple[str, int]]:
    """Every issue the line cites, as (owner/name, number)."""
    found = [("%s/%s" % (m.group(1), m.group(2)), int(m.group(3)))
             for m in _FULL_REF.finditer(line)]
    found += [(repo, int(m.group(1))) for m in _OWN_REF.finditer(line)]
    return found


def _parse_time(value: object) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def issue_problem(issue: Dict[str, object], now: dt.datetime) -> str:
    """Why a looked-up issue does not back a bug line; "" when it does."""
    if not isinstance(issue, dict) or "number" not in issue:
        return "the cited issue does not exist"
    if issue.get("pull_request"):
        return "the citation is a pull request, not an issue"
    state = str(issue.get("state") or "").lower()
    if state == "open":
        return ""
    created = _parse_time(issue.get("created_at"))
    if state == "closed" and created and now - created <= RECENT:
        return ""
    if state == "closed":
        return "the cited issue is closed and was not filed in the last two days"
    return "the cited issue's state is unknown"


def _line_problem(line: str, repo: str, lookup: Lookup, now: dt.datetime) -> str:
    if not line.isascii() or len(line) > MAX_LINE or any(ord(c) < 32 for c in line):
        return "the line is not plain ASCII text of at most %d characters" % MAX_LINE
    refs = citations(line, repo)
    if not refs:
        return "the line cites no issue"
    for where, number in refs:
        try:
            problem = issue_problem(lookup(where, number), now)
        except Exception as err:  # fail closed: an unverified claim is rejected
            problem = "the cited issue could not be looked up (%s)" % type(err).__name__
        if problem:
            return problem
    return ""


def check(lines: List[str], repo: str, lookup: Lookup,
          now: Optional[dt.datetime] = None) -> Tuple[List[str], List[Rejected]]:
    """(kept lines, rejected lines) for a section's lines."""
    now = now or dt.datetime.now(dt.timezone.utc)
    kept: List[str] = []
    rejected: List[Rejected] = []
    for position, line in enumerate(lines, 1):
        problem = _line_problem(line, repo, lookup, now)
        if problem:
            rejected.append(Rejected(position, problem))
        else:
            kept.append(line)
    return kept, rejected


def render_line(line: str, repo: str, public: bool) -> str:
    """The line as a body carries it: the repo's own OWNER/NAME#N as #N, and
    in a public repo another repo's as words, so no private repo name
    reaches a public body."""
    own = repo.lower()

    def ref(m: "re.Match[str]") -> str:
        where = "%s/%s" % (m.group(1), m.group(2))
        if where.lower() == own:
            return "#%s" % m.group(3)
        return "issue %s of another repo" % m.group(3) if public else m.group(0)
    return "- " + _FULL_REF.sub(ref, line)


def render(kept: List[str], repo: str, public: bool) -> str:
    """The section body for kept lines, one bullet each; "" for none."""
    return "\n".join(render_line(line, repo, public) for line in kept)


def refusal(rejected: List[Rejected]) -> str:
    """The PR text repair's refusal reason for rejected lines."""
    parts = ["line %d: %s" % (r.position, r.why) for r in rejected]
    return ("%s refused (%s); file the issue and cite it as #N, or drop the line"
            % (HEADING, "; ".join(parts)))


def _net_timeout() -> int:
    try:
        return max(1, int(os.environ.get("NET_TIMEOUT", "120")))
    except ValueError:
        return 120


def gh_lookup(repo: str, number: int) -> Dict[str, object]:
    """The issue from the GitHub API; raises when gh cannot answer."""
    out = subprocess.run(["gh", "api", "repos/%s/issues/%d" % (repo, number)],
                         capture_output=True, text=True, timeout=_net_timeout())
    if out.returncode != 0:
        raise RuntimeError("gh api failed")
    data = json.loads(out.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("gh api returned no JSON object")
    return data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the verified bugs section of a lane's Intent summary.")
    parser.add_argument("summary", help="the lane's Intent summary file")
    parser.add_argument("--repo", required=True, help="owner/name of the pull request")
    parser.add_argument("--private", action="store_true",
                        help="the repo is private (another repo's refs stay as written)")
    args = parser.parse_args(argv)
    try:
        with open(args.summary, encoding="utf-8") as fh:
            lines = section(fh.read())
    except (OSError, UnicodeDecodeError) as err:
        print("%s: summary unreadable, no section written: %s" % (HEADING, err), file=sys.stderr)
        return 0
    kept, rejected = check(lines, args.repo, gh_lookup)
    for r in rejected:
        print("%s line %d left out: %s" % (HEADING, r.position, r.why), file=sys.stderr)
    out = render(kept, args.repo, not args.private)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
