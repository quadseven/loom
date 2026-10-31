"""A pull request the review asks to split is split into tickets.

The review bot's check output carries a "Reviewability" list when part of a
diff did not fit its bounded review units. Three of its notes ask the author
to split the change (SPLIT_KINDS). A lane that lands a change and its proof in
one pull request gets the note every round and does not re-split on its own,
so a fix round only repeats the failure (loom#1218; the second operator's
report on loom#1026). When that note is the only reason the review check
failed, the factory (bus/factory.py, Factory._split_pr) files the parts as new
tickets and closes the pull request. This module is the pure part: reading
the note, choosing the parts and writing their ticket bodies.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The note kinds whose message asks for the change to be split.
SPLIT_KINDS = ("cross-cohort-proof", "plan-truncated", "cross-cohort-module")
# The most tickets one pull request is split into; more paths than this share
# the last part, so a huge diff does not flood the queue.
MAX_PARTS = 4
# Posted on the original ticket once its parts are filed, so a pass that
# filed them but did not finish closing does not file them twice.
SPLIT_MARKER = "<!-- loom:split pr=%d -->"

# One line of the bot's list: "- `kind`: message Paths: `a`, `b`".
_NOTE_RE = re.compile(r"^\s*-\s+`([a-z0-9-]+)`:\s.*?(?:Paths:\s*(.*))?$", re.M)
_PATH_RE = re.compile(r"`([^`]+)`")
_BLOCKING_RE = re.compile(r"Elder markings - (\d+) blocking")
_TEST_AFFIX = re.compile(r"^(?:test_|tests?[._-])|(?:_test|[._-]tests?|[._-]spec)$")
_EPIC_RE = re.compile(r"^\s*Part of #(\d+)", re.M)


def notes(summary: str) -> List[Tuple[str, List[str]]]:
    """The reviewability notes in a review check summary, as (kind, paths)."""
    text = str(summary or "")
    start = text.find("**Reviewability**")
    if start == -1:
        return []
    return [(m.group(1), _PATH_RE.findall(m.group(2) or ""))
            for m in _NOTE_RE.finditer(text[start:])]


def asks_split(title: str, summary: str) -> bool:
    """True when a failed review check failed only on a note asking for a
    split: the summary carries one, and the title does not report a blocking
    finding (a blocking finding is a fix for a lane to make, so the pull
    request goes back as before)."""
    blocking = _BLOCKING_RE.search(str(title or ""))
    if blocking and int(blocking.group(1)) > 0:
        return False
    return any(kind in SPLIT_KINDS for kind, _ in notes(summary))


def _stem(path: str) -> str:
    """The name a source file and its test share (bus/a.py and
    tests/test_a.py are both "a"), so a change stays with its proof."""
    name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stripped = _TEST_AFFIX.sub("", name)
    return stripped or name


def paths_of(summary: str) -> List[str]:
    """Every path the split notes name, once, in order."""
    found: List[str] = []
    for kind, paths in notes(summary):
        if kind in SPLIT_KINDS:
            found.extend(p for p in paths if p not in found)
    return found


def plan(summary: str, criteria: Sequence[str]) -> Optional[List[Dict[str, Any]]]:
    """The parts to file, each {"paths": [...], "criteria": [...]}, or None
    when the note leaves nothing to split on.

    Paths group by stem, so a file and its test stay in one part; each group
    is a part. With one group (or none), the original ticket's unchecked
    criteria are the parts, one each. A note that gives neither returns None
    and the pull request goes back for a fix round as before."""
    groups: Dict[str, List[str]] = {}
    for path in paths_of(summary):
        groups.setdefault(_stem(path), []).append(path)
    pending = [c.partition(" ")[2] for c in criteria if not c.startswith("x ")]
    pending = [c for c in pending if not c.strip().casefold().startswith("live:")]
    if len(groups) >= 2:
        parts: List[Dict[str, Any]] = [{"paths": p, "criteria": []} for p in groups.values()]
    elif len(pending) >= 2:
        parts = [{"paths": paths_of(summary), "criteria": [c]} for c in pending]
    else:
        return None
    while len(parts) > MAX_PARTS:
        last = parts.pop()
        parts[-1]["paths"] = parts[-1]["paths"] + [p for p in last["paths"]
                                                   if p not in parts[-1]["paths"]]
        parts[-1]["criteria"] = parts[-1]["criteria"] + last["criteria"]
    return parts


def epic_of(body: str, fallback: int) -> int:
    """The epic the ticket names ("Part of #N"), else the ticket itself."""
    found = _EPIC_RE.search(str(body or ""))
    return int(found.group(1)) if found else fallback


def header_lines(body: str) -> List[str]:
    """The Difficulty and Priority header lines of the original ticket, so a
    part is worked in the same tier and order; Difficulty defaults to medium."""
    keep = {}
    for line in str(body or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key in ("Difficulty", "Priority") and value.strip():
            keep.setdefault(key, "%s: %s" % (key, value.strip()))
    return [keep.get("Difficulty", "Difficulty: medium")] + (
        [keep["Priority"]] if "Priority" in keep else [])


def ticket_body(original: int, pr: int, headers: Sequence[str], part: Dict[str, Any],
                index: int, total: int, epic: int, original_criteria: Sequence[str]) -> str:
    """The body of one part: Why, What, Acceptance criteria (3 bullets, as the
    plan checks need), Size and Out of scope, and its epic."""
    if part["criteria"]:
        scope = "Build this acceptance criterion of #%d, with the test that proves it: %s" % (
            original, "; ".join(part["criteria"]))
    else:
        scope = ("Build the part of #%d that touches these files, with the tests that prove "
                 "it, in one pull request: %s." % (
                     original, ", ".join("`%s`" % p for p in part["paths"])))
    others = [c.partition(" ")[2] for c in original_criteria]
    context = ("\n\nThe whole ticket's acceptance criteria, for context (build only the "
               "part above):\n" + "\n".join("- " + c for c in others)) if others else ""
    return "\n".join(list(headers) + [
        "",
        "## Why",
        "Pull request #%d for #%d was failed by the review check on a note asking for the "
        "change to be split, because its change and proof did not fit one reviewable unit. "
        "This is part %d of %d of that work, small enough to review in one pass." % (
            pr, original, index, total),
        "",
        "## What",
        scope + context,
        "",
        "## Acceptance criteria",
        "- [ ] The part described above is built and a test that fails without it is added",
        "- [ ] The pull request changes only this part, so the review check has no "
        "reviewability note",
        "- [ ] The full test suite passes as CI runs it",
        "",
        "## Size",
        "S",
        "",
        "## Out of scope",
        "- The other parts of #%d, which are their own tickets." % original,
        "",
        "Part of #%d" % epic,
        ""])
