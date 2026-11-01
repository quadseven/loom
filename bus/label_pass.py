"""One label pass: remove the labels that contradict each other or describe a
step that is over (loom#2024).

Issues kept labels that said two things at once: a ticket with loom:answered
beside needs:human, needs-brain beside needs:human and needs:<login>,
loom:triage on an issue long since handled, a lane label days after its lane
answered no change. Every reader then saw two states. The station's
clean_labels step reads each repo's open issues at most once every EVERY
seconds and, per issue, removes:

- every Loom state label (loom:queued ... loom:verified) but one, keeping the
  newest the station set (its labeled events by a station login; else the
  newest by anyone; else the furthest state);
- needs-brain, when needs:human or a needs:<login> label says a person holds
  the issue (the brain has nothing to review until they act);
- loom:triage, once the issue is a loom:ticket or closed (triage is done);
- every lane:* label of a ticket at loom:answered: its lane's run ended with
  an answer and no pull request (no change), so no lane holds it until the
  brain briefs it again, which sets a lane label anew;
- every ask label once the answer is in the ticket (bus/ask_state.py), and
  needs-brain goes on when that answer names a command someone must run. An
  ask waiting on another ticket gets wait:blocked.

An old ask label (bus/labels.py) moves to needs:human only on a ticket this
pass writes to anyway, so no pass relabels on its own, PER_RUN bounds every
write, and nothing here posts a comment. A claimed ticket's ask is its
holder's.

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

from . import ask_state
from .brain import STATES
from .labels import HUMAN_LABELS, NEEDS_HUMAN, OLD_HUMAN_LABELS, needs_human

STATE_LABELS = tuple("loom:" + s for s in STATES)
NEEDS_BRAIN = "needs-brain"
HUMAN = "loom:human"
TRIAGE = "loom:triage"
TICKET = "loom:ticket"
ANSWERED = "loom:answered"
LANE_PREFIX = "lane:"
CLAIM_PREFIX = "claim:"
WAIT_PREFIX = "wait:"
WAIT_BLOCKED = "wait:blocked"
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
    add: Sequence[str] = ()


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


def _state_removals(names: List[str], events: Optional[Iterable[Any]],
                    station: Iterable[str]) -> List[Removal]:
    """Every state label but the newest the station set."""
    states = [n for n in names if n in STATE_LABELS]
    if len(states) < 2:
        return []
    kept = newest_state(states, events or [], station)
    return [Removal(n, "%s is the newest state label" % kept) for n in states if n != kept]


def _held_removals(issue: Dict[str, Any], names: List[str]) -> List[Removal]:
    """needs-brain once a person holds the issue; loom:triage once it is a
    ticket or closed."""
    found: List[Removal] = []
    holder = next((n for n in names if n == HUMAN or NEEDS_LOGIN_RE.match(n)), "")
    if NEEDS_BRAIN in names and holder:
        found.append(Removal(NEEDS_BRAIN, "%s says a person holds it" % holder))
    if TRIAGE in names and TICKET in names:
        found.append(Removal(TRIAGE, "it is a loom:ticket"))
    elif TRIAGE in names and str(issue.get("state") or "open") == "closed":
        found.append(Removal(TRIAGE, "it is closed"))
    return found


def _ask_changes(issue: Dict[str, Any], names: List[str], events: Iterable[Any],
                 comments: Sequence[Any], trusted: Iterable[str], ops: Iterable[str],
                 blockers: Iterable[int]) -> Plan:
    """What the ask's answer or wait changes on the ticket."""
    events, trusted, ops = list(events or []), list(trusted), list(ops)
    said = ask_state.answer_of(issue, comments, events, trusted, ops)
    if said is not None:
        return Plan([Removal(n, "the answer is in the ticket") for n in names
                     if n in HUMAN_LABELS], "",
                    [NEEDS_BRAIN] if ask_state.names_command(said) else [])
    if ask_state.state(issue, comments, events, trusted, ops, blockers) == ask_state.WAITING \
            and not any(n.startswith(WAIT_PREFIX) for n in names):
        return Plan([], "", [WAIT_BLOCKED])
    return Plan([], "", [])


def plan(issue: Dict[str, Any], events: Optional[Iterable[Any]] = None,
         station: Iterable[str] = (), comments: Optional[Sequence[Any]] = None,
         trusted: Iterable[str] = (), ops: Iterable[str] = (),
         blockers: Iterable[int] = ()) -> Plan:
    """The labels to change on `issue` and its health note. `events` (the
    issue's events, oldest first) is read only when needs_events says so;
    pass them then. `comments` are an ask's comments, with who may answer
    and the open issues its Blocked on section names; without them the ask
    is not judged."""
    names = names_of(issue)
    remove = _state_removals(names, events, station)
    gone = {r.label for r in remove}
    left = [n for n in names if n in STATE_LABELS and n not in gone]
    remove += _held_removals(issue, names)
    if left == [ANSWERED]:
        remove += [Removal(n, "its lane answered no change") for n in names
                   if n.startswith(LANE_PREFIX)]
    add: List[str] = []
    claimed = any(n.startswith(CLAIM_PREFIX) for n in names)
    if needs_human(names) and comments is not None and not claimed:
        asked = _ask_changes(issue, names, events or [], comments, trusted, ops, blockers)
        remove += asked.remove
        add += asked.add
    gone = {r.label for r in remove}
    old = [n for n in names if n in OLD_HUMAN_LABELS and n not in gone]
    if old and (remove or add) and not claimed:
        add.append(NEEDS_HUMAN)
        remove += [Removal(n, "needs:human replaces it") for n in old]
    return Plan(remove, parent_mismatch(issue), add)


def needs_events(issue: Dict[str, Any]) -> bool:
    """Whether plan() needs the issue's events: two or more state labels,
    or an ask."""
    names = names_of(issue)
    return sum(1 for n in names if n in STATE_LABELS) > 1 or needs_human(names)
