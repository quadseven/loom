"""Flag journal evidence gaps without rejecting a tick or inventing counts."""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

PREFIX = "- Journal check: "
_FIELD = re.compile(r"^(?:- )?(Moved|Merged|Briefed|Closed|Verified|Escalated|Shipped|"
                    r"Rebriefed|Factory tickets filed|Deferred|Actions?)(?::|\s+(?=\d))\s*(.*)$", re.I)
_COUNT = re.compile(r"\b(merged|briefed|closed|verified|escalated|shipped|rebriefed|"
                    r"factory tickets filed|deferred|actions?)\s*[:=]?\s*(\d{1,9})(?![\d.])\b", re.I)
_WAIT = re.compile(r"^(?:- )?(?:Waiting|Still stuck|Stuck):\s*(.*)$", re.I)
_STOP = re.compile(r"^(?:- )?(?:Standdown|Stand-down|No work|Nothing moved|Stopped):\s*(\S.*)$", re.I)
_EMPTY = {"", "none", "nothing", "0", "no work", "n/a"}


def _prose_indexes(text: str) -> List[int]:
    """Exclude fenced examples, including indented and long Markdown fences."""
    result = []
    fence = ""
    for index, line in enumerate(text.splitlines()):
        mark = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if mark:
            delimiter, suffix = mark.groups()
            if not fence:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence) and not suffix.strip():
                fence = ""
        elif not fence:
            result.append(index)
    return result


def _lines(text: str) -> List[str]:
    """Read top-level prose only; fenced examples are not tick evidence."""
    lines = text.splitlines()
    return [lines[index].rstrip() for index in _prose_indexes(text)]


def _counts(lines: List[str]) -> Optional[int]:
    """None means no count; otherwise sum work, treating deferrals as zero."""
    counts = []
    for line in lines:
        field = _FIELD.fullmatch(line)
        if field is None:
            continue
        name, value = field.groups()
        found = _COUNT.findall(name + " " + value)
        if name.lower() == "moved" and re.match(r"^\d{1,9}(?![\d.])\b", value):
            found = [("moved", re.match(r"^\d{1,9}(?![\d.])\b", value).group(0))]
        counts.extend(0 if verb.lower() == "deferred" else int(count) for verb, count in found)
    return sum(counts) if counts else None


def _waiting(lines: List[str]) -> Tuple[str, ...]:
    """Compare stuck fields and their indented items, not arbitrary prose."""
    values = []
    active = False
    for line in lines:
        match = _WAIT.fullmatch(line)
        if match:
            active = True
            values.append(match.group(1))
        elif active and line.startswith(("  - ", "  * ")):
            values.append(line[4:])
        elif line.strip():
            active = False
    values = [" ".join(value.split()) for value in values]
    return tuple(sorted(value for value in values if value.lower() not in _EMPTY))


def _stopped(lines: List[str]) -> bool:
    """An explicit standdown or no-work field must state a nonempty reason."""
    for line in lines:
        found = _STOP.fullmatch(line)
        if found and found.group(1).strip().lower() not in _EMPTY:
            return True
    return False


def annotate(text: str, previous: str = "") -> str:
    """Append bounded warnings; preserve the author's evidence and normal writes."""
    # Recalculate our own warning on replay; never accumulate stale checks.
    raw = text.splitlines()
    checks = {index for index in _prose_indexes(text) if raw[index].startswith(PREFIX)}
    text = "\n".join(line for index, line in enumerate(raw) if index not in checks)
    lines = _lines(text)
    if _stopped(lines):
        return text
    count = _counts(lines)
    warnings = []
    if count is None:
        warnings.append("missing counted moved line")
    waiting = _waiting(lines)
    if waiting and waiting == _waiting(_lines(previous)) and not count:
        warnings.append("repeated unchanged stuck list without action")
    if not warnings:
        return text
    return text.rstrip("\n") + "\n" + PREFIX + "; ".join(warnings)
