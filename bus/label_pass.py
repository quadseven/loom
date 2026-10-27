"""One label pass: remove the labels that contradict each other or describe a
step that is over (loom#2024).

Issues kept labels that said two things at once: a ticket with loom:answered
beside loom:human, needs-brain beside loom:human and needs:<login>,
loom:triage on an issue long since handled, a lane label days after its lane
answered no change. Every reader then saw two states. The station's
clean_labels step reads each repo's open issues at most once every EVERY
seconds and, per issue, removes:

- every Loom state label (loom:queued ... loom:verified) but one, keeping the
  newest the station set (its labeled events by a station login; else the
  newest by anyone; else the furthest state);
- needs-brain, when loom:human or a needs:<login> label says a person holds
  the issue (the brain has nothing to review until they act);
- loom:triage, once the issue is a loom:ticket or closed (triage is done);
- every lane:* label of a ticket at loom:answered: its lane's run ended with
  an answer and no pull request (no change), so no lane holds it until the
  brain briefs it again, which sets a lane label anew.

A body Part of #N that names a different issue than the sub-issue parent is
not changed: it is one health note, kept in the step's state file and said
once. In shadow mode (bus/shadow.py) each removal is a ledger line (action
UNLABEL) and is not sent. An issue another operator owns
(tickets.owned_elsewhere) is left alone.

plan() is pure: the station reads, plan() decides, the station writes.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence

from .brain import STATES

STATE_LABELS = tuple("loom:" + s for s in STATES)
NEEDS_BRAIN = "needs-brain"
HUMAN = "loom:human"
TRIAGE = "loom:triage"
TICKET = "loom:ticket"
ANSWERED = "loom:answered"
LANE_PREFIX = "lane:"
NEEDS_LOGIN_RE = re.compile(r"^needs:[A-Za-z0-9][A-Za-z0-9-]*$")
PART_OF_RE = re.compile(r"^Part of #(\d+)\s*$", re.M)
PARENT_URL_RE = re.compile(r"/issues/(\d+)(?:[/?#]|$)")

EVERY = 3600
PER_RUN = 20
STATE_FILE = "label-pass.json"


class Removal(NamedTuple):
    label: str
    why: str


class Plan(NamedTuple):
    remove: List[Removal]
    note: str          # "" or the one health note for the issue


def names_of(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") if isinstance(x, dict) else x) for x in issue.get("labels") or []]


def newest_state(present: Sequence[str], events: Iterable[Any],
                 station: Iterable[str]) -> str:
    """Which of the `present` state labels to keep: the one the station
    labelled last, else the one anybody labelled last, else the furthest."""
    logins = {s.lower() for s in station}
    by_station, by_anyone = "", ""
    for event in events or []:
        if not isinstance(event, dict) or event.get("event") != "labeled":
            continue
        name = str((event.get("label") or {}).get("name") or "")
        if name not in present:
            continue
        by_anyone = name
        actor = str((event.get("actor") or {}).get("login") or "").lower()
        if actor in logins:
            by_station = name
    if by_station or by_anyone:
        return by_station or by_anyone
    return max(present, key=STATE_LABELS.index)


def parent_mismatch(issue: Dict[str, Any]) -> str:
    """The health note when the body's Part of #N names another issue than
    the sub-issue parent, else ""."""
    said = PART_OF_RE.search(str(issue.get("body") or ""))
    linked = PARENT_URL_RE.search(str(issue.get("parent_issue_url") or ""))
    if not said or not linked or said.group(1) == linked.group(1):
        return ""
    return ("body says Part of #%s but the sub-issue parent is #%s"
            % (said.group(1), linked.group(1)))


def plan(issue: Dict[str, Any], events: Optional[Iterable[Any]] = None,
         station: Iterable[str] = ()) -> Plan:
    """The labels to remove from `issue` and its health note. `events` (the
    issue's events, oldest first) is read only when it carries two or more
    state labels; pass them then."""
    names = names_of(issue)
    remove: List[Removal] = []
    states = [n for n in names if n in STATE_LABELS]
    kept_state = ""
    if len(states) > 1:
        kept_state = newest_state(states, events or [], station)
        remove += [Removal(n, "%s is the newest state label" % kept_state)
                   for n in states if n != kept_state]
    elif states:
        kept_state = states[0]
    if NEEDS_BRAIN in names:
        holder = next((n for n in names if n == HUMAN or NEEDS_LOGIN_RE.match(n)), "")
        if holder:
            remove.append(Removal(NEEDS_BRAIN, "%s says a person holds it" % holder))
    if TRIAGE in names:
        if TICKET in names:
            remove.append(Removal(TRIAGE, "it is a loom:ticket"))
        elif str(issue.get("state") or "open") == "closed":
            remove.append(Removal(TRIAGE, "it is closed"))
    if kept_state == ANSWERED:
        remove += [Removal(n, "its lane answered no change") for n in names
                   if n.startswith(LANE_PREFIX)]
    return Plan(remove, parent_mismatch(issue))


def needs_events(issue: Dict[str, Any]) -> bool:
    """Whether plan() needs the issue's events (two or more state labels)."""
    return sum(1 for n in names_of(issue) if n in STATE_LABELS) > 1
