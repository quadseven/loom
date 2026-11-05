"""The naming standard (docs/NAMING.md): a kind label carries an issue's kind,
a title carries at most one leading kind word, and generated titles keep one
fixed lead phrase clipped to TITLE_MAX.

Code that decides an issue's kind reads its labels through this module, never
a title prefix, so retitling an issue cannot change what the factory does with
it. Pages and reports that already show the kind drop the redundant word with
strip_kind."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# The kind labels. A goal home is loom:goal; the one per repo that onboard
# opens for the repo's whole backlog also carries loom:backlog.
GOAL_LABEL = "loom:goal"
BACKLOG_LABEL = "loom:backlog"
MAP_LABEL = "wayfinder:map"
EPIC_LABEL = "epic"

# The one leading kind word a title of each kind starts with.
GOAL_WORD = "Goal: "
EPIC_WORD = "Epic: "
MAP_WORD = "Map: "

# Goal homes, epics, maps and generated tickets: at most this long.
TITLE_MAX = 72
# Any ticket title: a hard cap.
TICKET_TITLE_MAX = 100

# Backlog homes opened before the loom:backlog label existed start with this.
# Accepted while those homes are retitled and labelled; drop it once none is
# left (docs/NAMING.md, Migration).
LEGACY_BACKLOG_PREFIX = "Backlog:"

# Every leading kind word in use, old and new: the standard's three, and the
# four older goal home prefixes and the bracketed epic tag.
_KIND_WORD = re.compile(
    r"^\s*(?:\[\s*epic\s*\]\s*:?|(?:loom\s+goal|goal\s+home|goal|backlog|wayfinder|map|epic)\s*:)\s*",
    re.IGNORECASE)


def label_names(issue: Dict[str, Any]) -> List[str]:
    """The issue's label names, from GitHub's label objects or plain strings."""
    out = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            out.append(name)
    return out


def is_backlog_home(issue: Dict[str, Any]) -> bool:
    """A goal home that holds its repo's backlog: labelled loom:backlog, or
    (during the migration) titled with the old "Backlog:" prefix."""
    return (BACKLOG_LABEL in label_names(issue)
            or str(issue.get("title") or "").startswith(LEGACY_BACKLOG_PREFIX))


def strip_kind(title: Optional[str]) -> str:
    """The title without its leading kind words ("Goal:", "Loom goal:", "Goal
    home:", "Backlog:", "Wayfinder:", "Map:", "Epic:", "[EPIC]"), for a place
    that already shows the kind. A title that is only a kind word is kept."""
    text = " ".join(str(title or "").split())
    out = text
    while True:
        match = _KIND_WORD.match(out)
        if not match or match.end() == 0:
            break
        out = out[match.end():]
    return out or text


def clip(text: str, limit: int = TITLE_MAX) -> str:
    """One line of at most `limit` characters, cut on a word with "..." when
    it is longer."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit - 3]
    if " " in head and text[limit - 3] != " ":
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" :,;-\\") + "..."


def generated(lead: str, detail: str = "", limit: int = TITLE_MAX) -> str:
    """A generated title: the generator's fixed lead phrase, then ": detail"
    when there is one, clipped to `limit` with the lead kept whole."""
    lead = " ".join(lead.split())
    detail = " ".join(str(detail or "").split())
    if not detail:
        return clip(lead, limit)
    return clip("%s: %s" % (lead, detail), limit)
