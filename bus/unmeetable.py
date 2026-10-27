"""A Live box that can never be met as written (loom#2028).

Some shipped tickets wait on a Live box no run can ever meet, and the
station handed them to verify lanes again and again, each run coming back
unmet. Two shapes are read here:

- the box names a closed issue, or a pull request closed without merging,
  as the event it waits for ("muster#163 is verified through the new
  mechanism", when muster#163 closed before the mechanism merged). A merged
  pull request is not flagged: "after #N merges" has happened, and the log
  decides the rest.
- the box quotes a literal log string that nothing in the repo's source
  tree contains ("the log contains `Dashboard revision updated`", when no
  code prints it). A string the box needs to be absent, and a template too
  general to search (bus/evidence.py), are never flagged.

A lookup that fails (an unreadable issue, a code search that errors or
says its results are incomplete) proves nothing and flags nothing.

`reasons` returns why the box cannot be met; the station posts one restate
ask (`ask`) for each box text and does not hand the ticket to a lane again
until the text changes (bus/station.py, _unmeetable_live).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import evidence

MARKER = "<!-- loom:unmeetable-live v1 %s -->"
MARKER_RE = re.compile(r"<!-- loom:unmeetable-live v1 ([0-9a-f]{16}) -->")
# An issue or pull request reference: #N, name#N, owner/name#N, or a GitHub
# issue or pull request URL.
REF_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)"
    r"|(?<![\w/.#-])((?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)?#(\d+)\b")
MAX_REFS = 5

# (state, merged) of an issue or pull request; None when it cannot be read.
StateOf = Callable[[str, int], Optional[Tuple[str, bool]]]
# Whether the repo's source tree contains a string; None when unknown.
Present = Callable[[str], Optional[bool]]


def key(live: str) -> str:
    """A short stable key for the Live box text: a new text is a new key."""
    return hashlib.sha256(" ".join((live or "").split()).encode("utf-8")).hexdigest()[:16]


def refs(live: str, repo: str) -> List[Tuple[str, int]]:
    """The (repo, number) pairs the Live box names, in order, at most
    MAX_REFS. A bare #N is in `repo`; a bare name#N is in `repo`'s owner."""
    owner = repo.split("/", 1)[0]
    found: List[Tuple[str, int]] = []
    for match in REF_RE.finditer(live or ""):
        if match.group(1):
            where, number = match.group(1), int(match.group(2))
        else:
            name, number = match.group(3), int(match.group(4))
            where = repo if not name else (name if "/" in name else "%s/%s" % (owner, name))
        if (where, number) not in found:
            found.append((where, number))
    return found[:MAX_REFS]


def closed_refs(live: str, repo: str, number: int, state_of: StateOf) -> List[str]:
    """The references in the Live box to a closed issue or an unmerged
    closed pull request, as "repo#N"; the ticket itself is never one."""
    out = []
    for where, ref in refs(live, repo):
        if (where, ref) == (repo, number):
            continue
        seen = state_of(where, ref)
        if seen is None:
            continue
        state, merged = seen
        if state == "closed" and not merged:
            out.append("%s#%d" % (where, ref))
    return out


def literal_strings(live: str) -> List[str]:
    """The strings the Live box needs a log line to contain, as the source
    would hold them: a template's longest fixed part, never a string the box
    needs absent, never a template too general to search."""
    absent = set(evidence.absent_needles(live))
    out = []
    for needle in evidence.needles(live):
        if needle in absent:
            continue
        literal, _ = evidence.needle_pattern(needle)
        if literal and literal not in out:
            out.append(literal)
    return out


def missing_strings(live: str, present: Present) -> List[str]:
    """The literal log strings of the Live box the source tree lacks."""
    return [text for text in literal_strings(live) if present(text) is False]


def reasons(live: str, repo: str, number: int, state_of: StateOf,
            present: Present) -> List[str]:
    """Why the Live box can never be met as written; empty when nothing
    shows it."""
    out = ["it waits on %s, which is closed without the event" % ref
           for ref in closed_refs(live, repo, number, state_of)]
    out += ["it waits for the log string `%s`, which no file in %s contains" % (text, repo)
            for text in missing_strings(live, present)]
    return out


def ask(live: str, found: List[str]) -> str:
    """The one restate comment for a Live box that cannot be met."""
    return ("%s\nThis ticket's Live box cannot be met as written: %s. The station stops "
            "handing the ticket to a verify lane until the box text changes. Restate the "
            "Live box so it waits on an event that can still happen and a string the "
            "merged code prints (a brain runs `bus.brain live <ticket> --text ...`), or "
            "close the ticket if the work no longer applies." % (
                MARKER % key(live), "; ".join(found)))


def asked(comments: List[Dict[str, Any]], live: str) -> bool:
    """Whether a comment already asks to restate this exact box text."""
    want = key(live)
    return any(want in MARKER_RE.findall(str(c.get("body") or ""))
               for c in comments if isinstance(c, dict))
