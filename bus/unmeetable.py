"""A Live box that can never be met as written (loom#2028).

Some shipped tickets wait on a Live box no run can ever meet, and the
station handed them to verify lanes again and again, each run coming back
unmet. Two shapes are read here:

- the box names a closed issue, or a pull request closed without merging,
  as the event it waits for ("muster#163 is verified through the new
  mechanism", when muster#163 closed before the mechanism merged). A merged
  pull request is not flagged: "after #N merges" has happened, and the log
  decides the rest.
  A reference the box words in the past tense ("fixed in #N", "after #N
  merged", "#N was closed") is history, not a wait, and is not flagged.
- the box quotes a literal log string that nothing in the repo's source
  tree contains ("the log contains `Dashboard revision updated`", when no
  code prints it). A string the box needs to be absent, and a template too
  general to search (bus/evidence.py), are never flagged. A quoted line
  often carries filled-in values the source holds as a placeholder
  (`claimed #528 for lane` against "claimed %s for
  lane"): issue references, hex SHAs and numbers are read as wildcards
  (`Term`), so the source line is matched, not the literal values.

A lookup that fails (an unreadable issue, a source read that errors or
cannot cover the tree) proves nothing and flags nothing. A code search
that finds nothing is not proof either: GitHub's index skips large files,
so the station reads those files itself before it calls a string absent
(bus/station.py, _source_has).

`reasons` returns why the box cannot be met; the station posts one restate
ask (`ask`) for each box text and does not hand the ticket to a lane again
until the text changes (bus/station.py, _unmeetable_live).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from . import evidence

MARKER = "<!-- loom:unmeetable-live v1 %s -->"
MARKER_RE = re.compile(r"<!-- loom:unmeetable-live v1 ([0-9a-f]{16}) -->")
# An issue or pull request reference: #N, name#N, owner/name#N, or a GitHub
# issue or pull request URL.
REF_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)"
    r"|(?<![\w/.#-])((?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)?#(\d+)\b")
MAX_REFS = 5
# A reference worded as history, not as the event the box waits for: "fixed
# in #N" before it, or "#N was merged", "after #N merged" around it.
PAST_BEFORE_RE = re.compile(
    r"\b(?:fixed|resolved|closed|landed|shipped|merged|done|added|introduced|delivered"
    r"|replaced|superseded)\s+(?:in|by|with|via|through)\s*$", re.I)
PAST_AFTER_RE = re.compile(
    r"\s*(was\s+|has\s+been\s+|had\s+been\s+)?"
    r"(?:merged|closed|fixed|landed|shipped|resolved)\b", re.I)
PAST_LEAD_RE = re.compile(r"\b(?:after|since|once|now\s+that|as\s+of)\s*$", re.I)
# A filled-in value in a quoted log line, which the source holds as a
# placeholder: an issue reference (owner/name#N, name#N, #N), a hex SHA
# (seven or more hex digits with a digit and a letter) or a number.
FILLED_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]*#\d+"
    r"|\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"
    r"|\b\d+(?:\.\d+)*\b")


class Term(NamedTuple):
    """A log string a Live box waits for, as it is looked up in source."""
    shown: str            # as the box quotes it
    literal: str          # its longest fixed part, what a code search looks for
    pattern: "re.Pattern[str]"  # a source line that can print it matches this


# (state, merged) of an issue or pull request; None when it cannot be read.
StateOf = Callable[[str, int], Optional[Tuple[str, bool]]]
# Whether the repo's source tree holds a term; None when unknown.
Present = Callable[[Term], Optional[bool]]


def key(live: str) -> str:
    """A short stable key for the Live box text: a new text is a new key."""
    return hashlib.sha256(" ".join((live or "").split()).encode("utf-8")).hexdigest()[:16]


def _mentions(live: str, repo: str) -> List[Tuple[Tuple[str, int], bool]]:
    """Each reference in the Live box with whether it is worded as history."""
    owner = repo.split("/", 1)[0]
    out = []
    for match in REF_RE.finditer(live):
        if match.group(1):
            where, number = match.group(1), int(match.group(2))
        else:
            name, number = match.group(3), int(match.group(4))
            where = repo if not name else (name if "/" in name else "%s/%s" % (owner, name))
        out.append(((where, number), _past(live, match.start(), match.end())))
    return out


def _past(live: str, start: int, end: int) -> bool:
    """Whether the reference at live[start:end] is worded in the past tense."""
    before = live[max(0, start - 40):start]
    if PAST_BEFORE_RE.search(before):
        return True
    after = PAST_AFTER_RE.match(live, end)
    if after is None:
        return False
    return bool(after.group(1)) or bool(PAST_LEAD_RE.search(before))


def refs(live: str, repo: str) -> List[Tuple[str, int]]:
    """The (repo, number) pairs the Live box names, in order, at most
    MAX_REFS. A bare #N is in `repo`; a bare name#N is in `repo`'s owner."""
    found: List[Tuple[str, int]] = []
    for pair, _ in _mentions(live or "", repo):
        if pair not in found:
            found.append(pair)
    return found[:MAX_REFS]


def waited_refs(live: str, repo: str) -> List[Tuple[str, int]]:
    """The references the Live box waits on: those named at least once not
    in the past tense ("fixed in #N" and "after #N merged" are history)."""
    found: List[Tuple[str, int]] = []
    for pair, past in _mentions(live or "", repo):
        if not past and pair not in found:
            found.append(pair)
    return found[:MAX_REFS]


def closed_refs(live: str, repo: str, number: int, state_of: StateOf) -> List[str]:
    """The references the Live box waits on that are a closed issue or an
    unmerged closed pull request, as "repo#N"; the ticket itself is never one."""
    out = []
    for where, ref in waited_refs(live, repo):
        if (where, ref) == (repo, number):
            continue
        seen = state_of(where, ref)
        if seen is None:
            continue
        state, merged = seen
        if state == "closed" and not merged:
            out.append("%s#%d" % (where, ref))
    return out


def term(needle: str) -> Optional[Term]:
    """The needle as the source would hold it: a template placeholder
    (bus/evidence.py) and a filled-in value (FILLED_RE) both stand for
    anything. None when its longest fixed part is too general to search."""
    pieces = [part for piece in evidence.PLACEHOLDER_RE.split(needle)
              for part in FILLED_RE.split(piece)]
    if len(pieces) == 1:
        return Term(needle, needle, re.compile(re.escape(needle)))
    literal = max((piece.strip() for piece in pieces), key=len)
    if len(literal) < evidence.MIN_LITERAL:
        return None
    return Term(needle, literal, re.compile(".+?".join(re.escape(piece) for piece in pieces)))


def terms(live: str) -> List[Term]:
    """The strings the Live box needs a log line to contain, as looked up in
    source: never a string the box needs absent, never one too general."""
    absent = set(evidence.absent_needles(live))
    out: List[Term] = []
    for needle in evidence.needles(live):
        if needle in absent:
            continue
        found = term(needle)
        if found is not None and found.literal not in [t.literal for t in out]:
            out.append(found)
    return out


def holds(found: Term, texts: Iterable[str]) -> bool:
    """Whether a line of any of these source texts can print the term."""
    return any(found.pattern.search(line)
               for text in texts for line in text.splitlines())


def missing_strings(live: str, present: Present) -> List[str]:
    """The log strings of the Live box, as quoted, the source tree lacks."""
    return [t.shown for t in terms(live) if present(t) is False]


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
