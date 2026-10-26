"""Did a fix round move its pull request? Failures compared by signature.

A fix round is stuck when the failures it was asked to fix come back after
it: the same check names still fail, or the review bot holds a finding at
the same path, within LINE_SLACK lines (a round's edits shift line numbers),
under the same rule. Thread ids and check run ids are not compared: the
review bot may post the same finding as a new thread on the next review, and
a check that fails again has a new run id.

On 2026-10-26 two pull requests went round and round on one lane with the
same failures: four rounds of one private repo's ticket on a local model,
each "done" in 10 to 27 minutes, each leaving the same two checks failing
and the same review finding open; and two rounds of another ticket whose
three findings came back two to five lines lower each time. The station's
hand-back sent every round to the same lane and never asked whether the last
one had moved anything (bus/station.py, _post_fix_or_escalate).

The asked side is read from the last loom:fix body (bus/brain.py _fix_body):
its "### Check: <name>" headings, and its "### Thread <id> (comment <n>) at
<path>:<line>" headings, each followed by the thread's quoted text, whose
first line carries the finding's rule. The current side is the failing check
names and the open review-bot threads the station just read.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from .tickets import FIX_MARKER

# Lines a finding may move between rounds and still be the same finding.
LINE_SLACK = 10
CHECK_RE = re.compile(r"^### Check: (.+?)[ \t]*$", re.M)
THREAD_RE = re.compile(r"^### Thread (\S+) \(comment [^)\n]*\) at (.+):([0-9]+|\?|None)[ \t]*$", re.M)
# The review bot's rule is the first code span on its finding's first line
# ("medium | _robustness_ | `resource-leak` | ...").
RULE_RE = re.compile(r"`([A-Za-z0-9][A-Za-z0-9_.:-]*)`")
# A quoted review line is "    <login>: <body line>" (bus/brain.py _quoted).
QUOTED_AUTHOR_RE = re.compile(r"^ {4}[A-Za-z0-9_.\[\]-]+: ?")
SHOWN_MAX = 5


class Finding(NamedTuple):
    ident: str
    path: str
    line: Optional[int]
    rule: str


def rule_of(first_line: str) -> str:
    """A finding's rule: the first code span on its first line, lower case;
    "" when it names none (such a finding matches only by its thread id)."""
    found = RULE_RE.search(first_line)
    return found.group(1).lower() if found else ""


def _first_line(text: str) -> str:
    return next((line for line in str(text or "").splitlines() if line.strip()), "")


def _line(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def asked_checks(body: str) -> Set[str]:
    """The check names a loom:fix body asked a lane to fix."""
    return {" ".join(name.split()) for name in CHECK_RE.findall(body)}


def asked_findings(body: str) -> List[Finding]:
    """The review findings a loom:fix body asked a lane to fix."""
    out = []
    for match in THREAD_RE.finditer(body):
        rest = body[match.end():].split("\n### ", 1)[0]
        quoted = next((line for line in rest.splitlines() if line.startswith("    ")), "")
        out.append(Finding(match.group(1), match.group(2).strip(), _line(match.group(3)),
                           rule_of(QUOTED_AUTHOR_RE.sub("", quoted, count=1))))
    return out


def _root_body(thread: Dict[str, Any]) -> str:
    for field in ("root", "comments"):
        nodes = (thread.get(field) or {}).get("nodes") or []
        if nodes and isinstance(nodes[0], dict):
            return str(nodes[0].get("body") or "")
    return ""


def thread_finding(thread: Dict[str, Any]) -> Finding:
    """The signature of an open review thread the station or factory read."""
    line = _line(thread.get("line")) or _line(thread.get("originalLine"))
    return Finding(str(thread.get("id") or ""), str(thread.get("path") or "?"), line,
                   rule_of(_first_line(_root_body(thread))))


def same_finding(old: Finding, new: Finding) -> bool:
    """The same thread, or the same rule at the same path within LINE_SLACK
    lines (either line unknown: the same path is enough)."""
    if old.ident and old.ident == new.ident:
        return True
    if old.path != new.path or not old.rule or old.rule != new.rule:
        return False
    if old.line is None or new.line is None:
        return True
    return abs(old.line - new.line) <= LINE_SLACK


def last_fix_body(comments: Iterable[Any], authors: Iterable[str],
                  pull: Optional[Tuple[str, int]] = None) -> Optional[str]:
    """The newest loom:fix body a trusted login wrote (for `pull` when given:
    its "Pull request: <repo>#<n>" line must name it), or None."""
    allowed = {str(a).lower() for a in authors if a}
    found = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user")
        login = str(user.get("login") or "").lower() if isinstance(user, dict) else ""
        body = str(comment.get("body") or "")
        if login not in allowed or not body.startswith(FIX_MARKER):
            continue
        if pull is not None and ("Pull request: %s#%d," % pull) not in body:
            continue
        found = body
    return found


def persistent(last_body: Optional[str], failing: Iterable[str],
               threads: Sequence[Dict[str, Any]], ignore: Iterable[str] = ()) -> List[str]:
    """What the last fix round was asked to fix and still fails, one short
    line each: "check <name>" and "finding <rule> at <path>:<line>". Empty
    when there was no earlier round or it moved everything. Checks in
    `ignore` (the review check, whose failure is its findings) are left out:
    a review that finds new things is progress."""
    if not last_body:
        return []
    skip = {" ".join(str(n).split()) for n in ignore}
    now = {" ".join(str(n).split()) for n in failing} - skip
    out = ["check %s" % name for name in sorted((asked_checks(last_body) - skip) & now)]
    current = [thread_finding(t) for t in threads]
    for old in asked_findings(last_body):
        new = next((f for f in current if same_finding(old, f)), None)
        if new is not None:
            out.append("finding %s at %s:%s" % (new.rule or "?", new.path, new.line or "?"))
    return out


def describe(failing: Iterable[str], threads: Sequence[Dict[str, Any]],
             ignore: Iterable[str] = ()) -> List[str]:
    """Every current failure, in the same short lines as persistent()."""
    skip = {" ".join(str(n).split()) for n in ignore}
    out = ["check %s" % name for name in sorted({" ".join(str(n).split()) for n in failing} - skip)]
    for found in (thread_finding(t) for t in threads):
        out.append("finding %s at %s:%s" % (found.rule or "?", found.path, found.line or "?"))
    return out


def summary(found: Sequence[str]) -> str:
    """The persistent failures as one line, at most SHOWN_MAX named."""
    shown = list(found[:SHOWN_MAX])
    if len(found) > SHOWN_MAX:
        shown.append("%d more" % (len(found) - SHOWN_MAX))
    return "; ".join(shown)
