"""The factory's own invariants, checked by the station every pass.

Each invariant is one small function from Facts (what the station already
reads: brain journals and the marker comments on goal homes, lane run
outcomes, lane limits, queued tickets, each goal's epics, the review bot's plan
check on lane pull requests, the lane pull requests nothing drives, remote
heartbeats) to a list of Breach. A Facts field the station could not read this
pass is None, and the invariants that need it find nothing.

Filer files each breach as one loom:invariant issue on the factory's goal
home, deduplicated by invariant and subject: a breach whose issue is open
files nothing, and one whose issue was closed reopens it only for evidence
newer than the close. Adding an invariant is one function in INVARIANTS plus
a test.

Everything here is written to a public repo. A subject in another repo comes
in as a Ref whose label is already public (a private repo is named in words),
the dedup key in the marker is a hash of the subject, failure reasons have
repos, paths and credentials replaced, and a line that fails the
written-text check is withheld.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import brain
from . import text as _text
from . import watch
from .github import GitHubError
from .labels import REMOTE_LANES

DAY = 24 * 3600
CLEAR_AFTER = 6 * 3600
RECURRING_AFTER = 7 * DAY
# No journal from any brain for this long is a brain that stopped ticking.
BRAIN_SILENT = 90 * 60
# Two loom:report comments on one goal closer than this are one too many.
REPORT_GAP = 90 * 60
RUNS_MIN = 5
HELD_MAX = 2 * 3600
# A gate that has refused a goal's briefed tickets for this long gets its own
# invariant naming it (loom#564).
GATE_HOLD_MAX = 3600
# One refused decision repeating this long with no success of its kind is an
# invariant (loom#593). The event log is read EVENT_WINDOW back, so a run that
# began just over an hour ago is seen whole between irregular station passes.
REPEATED_FAILURE = 3600
EVENT_WINDOW = 2 * REPEATED_FAILURE
FAILURE_KINDS = ("rewrite_failed", "rewrite_skipped", "model_wait", "not_claiming")
# Decision 0003: an epic holds at most EPIC_TICKETS tickets.
EPIC_TICKETS = 15
PLAN_FAILED_AGE = 30 * 60
# More than LABELER_REFUSALS_MAX refusals of briefed tickets for one untrusted
# labeler within LABELER_WINDOW seconds is a trust gap (loom#660).
LABELER_REFUSALS_MAX = 3
LABELER_WINDOW = 3600
# A lane pull request open longer than this is a breach: six hours is the time
# the factory gives a lane to open one, and nothing else named the pull
# requests that sat past it (loom#715). Not CLEAR_AFTER, which is how long a
# breach must be gone before its issue closes.
PR_STALLED = 6 * 3600
# The station checks at most this often: goal homes and check runs are read
# no more often than its other goal-home reads.
CHECK_EVERY = 300
PLAN_FAILED = ("failure", "action_required", "timed_out")
DEFAULT_PLAN_CHECK = "Grug - Chief"
# The step names of a brain tick (docs/MUSE.md). A journal line such as
# "- Step status: failed (GitHub answered 502)" reports one that broke.
STEPS = ("lock", "status", "merge", "verify", "failed", "asks", "brief", "triage",
         "journal", "unlock")
STEP_RE = re.compile(r"(?im)^[ \t]*[-*]?[ \t]*(?:step[ \t]+)?(%s)(?:[ \t]+step)?[ \t]*:[ \t]*"
                     r"(failed|degraded)\b[ \t:,.-]*(.*?)[ \t]*$" % "|".join(STEPS))
LABEL = "loom:invariant"
LABELS = ["loom:triage", LABEL]
MARKER = "<!-- loom:invariant v1 key=%s -->"
MARKER_RE = re.compile(r"<!-- loom:invariant v1 key=([a-z-]+/[0-9a-f]{12}) -->")
# Issues filed plus issues reopened in one pass.
WRITES_MAX = 5
SHOWN = 5
LINE_MAX = 300
WITHHELD = "(withheld: the line fails the written-text check)"

Say = Callable[[str], None]


class Ref(NamedTuple):
    """An issue or pull request: key names it exactly (for deduplication),
    label is how a public issue may name it."""
    key: str
    label: str


class Journal(NamedTuple):
    at: float
    goal: Ref
    holder: str
    tick: str
    # The step lines that say failed or degraded (failed_steps).
    steps: Tuple[str, ...]


class Posted(NamedTuple):
    """A marker comment on a goal home: kind is "metrics" or "report"; day
    is the date a metrics line is for."""
    goal: Ref
    kind: str
    at: float
    day: str


class Run(NamedTuple):
    lane: str
    done: bool
    at: float
    reason: str


class Check(NamedTuple):
    """The newest completed run of the plan check on a lane pull request."""
    pr: Ref
    name: str
    conclusion: str
    at: float
    title: str


class LanePull(NamedTuple):
    """An open pull request on a loom/ branch as the station reads it: the pull
    request and when it opened, the issue that carries it (None when it names
    none that can be read), the labels of that issue and of the pull request,
    whether that issue is an open loom:ticket the factory can move (drives),
    how many unresolved threads the review bot left on it, and whether a fix
    round is queued on its ticket."""
    pr: Ref
    at: float
    ticket: Optional[Ref]
    labels: Tuple[str, ...]
    drives: bool
    threads: int
    fix_pending: bool


class EpicShape(NamedTuple):
    """One epic of a goal: open or closed, how many tickets it holds, and
    which of them are still open."""
    epic: Ref
    open: bool
    tickets: int
    open_tickets: Tuple[Ref, ...]
    # The goal's standing "Factory repairs" epic: no ticket cap, and not
    # counted against the goal's open epic cap.
    standing: bool = False


class GoalShape(NamedTuple):
    """A goal home under the epic model (decision 0003): its epics, its open
    epic cap (6, or the goal home's recorded exception), and its open
    tickets that sit under no epic."""
    goal: Ref
    cap: int
    epics: Tuple[EpicShape, ...]
    outside: Tuple[Ref, ...]


class GateHold(NamedTuple):
    """A goal the unproven-work gate refuses briefed tickets for: the goal,
    when the gate first refused it, the unproven count and the limit it hit,
    the briefed tickets it holds, and the lanes left idle meanwhile."""
    goal: Ref
    since: float
    count: int
    limit: int
    tickets: Tuple[Ref, ...]
    idle: int


class Event(NamedTuple):
    at: float
    kind: str
    reason: str
    item: str = ""
    lane: str = ""
    model: str = ""


@dataclass
class Facts:
    now: float
    journals: Optional[List[Journal]] = None
    # The newest journal time ever seen, kept between passes.
    brain_seen: Optional[float] = None
    posts: Optional[List[Posted]] = None
    runs: Optional[List[Run]] = None
    # lane -> since when it has been held by a limit.
    held: Optional[Dict[str, float]] = None
    # (ticket, the lanes of its difficulty tier) for each queued ticket.
    queued: Optional[List[Tuple[Ref, Tuple[str, ...]]]] = None
    # Each goal home with its epics and the open tickets under none.
    shapes: Optional[List[GoalShape]] = None
    checks: Optional[List[Check]] = None
    # The open lane pull requests past PR_STALLED, with what the station can
    # see driving each of them.
    lane_pulls: Optional[List[LanePull]] = None
    # goal key -> the unproven-work gate's hold on it, per loom#564.
    gate_held: Optional[Dict[str, GateHold]] = None
    # remote lane -> its newest heartbeat, None when it never posted one.
    beats: Optional[Dict[str, Optional[float]]] = None
    events: Optional[List[Event]] = None
    # untrusted labeler login -> briefed tickets refused for it in the last
    # LABELER_WINDOW seconds.
    label_refusals: Optional[Dict[str, int]] = None


@dataclass(frozen=True)
class Breach:
    invariant: str
    subject: str
    title: str
    evidence: Tuple[str, ...]
    # The newest evidence: a closed issue reopens only for a later breach.
    at: float

    @property
    def key(self) -> str:
        return "%s/%s" % (self.invariant, hashlib.sha256(self.subject.encode()).hexdigest()[:12])


def utc(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def failed_steps(body: str) -> Tuple[str, ...]:
    """The journal's lines that report a tick step failed or degraded, as
    "<step> <failed|degraded>[: why]"."""
    out = []
    for m in STEP_RE.finditer(body):
        why = public(m.group(3))
        out.append("%s %s%s" % (m.group(1).lower(), m.group(2).lower(), ": " + why if why else ""))
    return tuple(out)


def public(line: str) -> str:
    """A line of evidence fit for a public issue: repos, paths, credentials,
    addresses and mentions replaced (watch.redact), clipped."""
    return watch.redact(line)[:LINE_MAX]


def _minutes(seconds: float) -> int:
    return int(seconds // 60)


def _hours(seconds: float) -> str:
    return "%.1f hours" % (seconds / 3600)


# -- the invariants ------------------------------------------------------
def brain_failing(f: Facts) -> List[Breach]:
    """A brain holder whose two newest ticks each journaled a failed or
    degraded step. One tick may journal on several goals; it is one tick."""
    if f.journals is None:
        return []
    ticks: Dict[Tuple[str, str], List[Journal]] = {}
    for j in f.journals:
        ticks.setdefault((j.holder, j.tick or utc(j.at)), []).append(j)
    by_holder: Dict[str, List[List[Journal]]] = {}
    for (holder, _), found in ticks.items():
        by_holder.setdefault(holder, []).append(found)
    out = []
    for holder, found in by_holder.items():
        last = sorted(found, key=lambda t: max(j.at for j in t))[-2:]
        if len(last) < 2 or not all(any(j.steps for j in t) for t in last):
            continue
        name = holder or "unnamed"
        evidence = []
        for tick in last:
            for j in tick:
                evidence += ["Tick %s on %s: %s" % (utc(j.at), j.goal.label, s) for s in j.steps]
        out.append(Breach("brain-failing", name,
                          "Brain %s reported a failed or degraded step two ticks in a row" % name,
                          tuple(evidence), max(j.at for j in last[-1])))
    return out


def brain_silent(f: Facts) -> List[Breach]:
    """No journal from any brain for BRAIN_SILENT, once one has been seen."""
    if f.journals is None:
        return []
    newest = max(f.journals, key=lambda j: j.at, default=None)
    seen = max([x for x in (f.brain_seen, newest.at if newest else None) if x is not None],
               default=None)
    if seen is None or f.now - seen <= BRAIN_SILENT:
        return []
    evidence = ["Newest journal: %s" % utc(seen)]
    if newest is not None and newest.at == seen:
        evidence = ["Newest journal: %s by %s on %s"
                    % (utc(seen), newest.holder or "an unnamed brain", newest.goal.label)]
    return [Breach("brain-silent", "brain",
                   "No brain tick for %d minutes" % _minutes(f.now - seen),
                   tuple(evidence + ["Limit: one tick every %d minutes" % _minutes(BRAIN_SILENT)]),
                   f.now)]


def marker_cadence(f: Facts) -> List[Breach]:
    """A loom:metrics line posted more than once for one day, or two
    loom:report comments within REPORT_GAP, on one goal home."""
    if f.posts is None:
        return []
    groups: Dict[Tuple[str, str], List[Posted]] = {}
    for p in f.posts:
        groups.setdefault((p.goal.key, p.kind), []).append(p)
    out = []
    for (goal, kind), found in sorted(groups.items()):
        found.sort(key=lambda p: p.at)
        label = found[0].goal.label
        if kind == "metrics":
            days: Dict[str, List[Posted]] = {}
            for p in found:
                days.setdefault(p.day, []).append(p)
            many = [d for d in sorted(days) if len(days[d]) > 1]
            if not many:
                continue
            day = days[many[-1]]
            out.append(Breach("metrics-cadence", goal,
                              "The daily metrics line posted %d times for %s on %s"
                              % (len(day), many[-1], label),
                              ("%d loom:metrics comments for %s, the first at %s and the last at %s"
                               % (len(day), many[-1], utc(day[0].at), utc(day[-1].at)),
                               "Limit: one a day"),
                              day[-1].at))
        elif kind == "report":
            close = [(a, b) for a, b in zip(found, found[1:], strict=False) if b.at - a.at < REPORT_GAP]
            if not close:
                continue
            a, b = close[-1]
            out.append(Breach("report-cadence", goal,
                              "Reports posted %d minutes apart on %s" % (_minutes(b.at - a.at), label),
                              ("%d loom:report comments in the last day, %d of them within %d "
                               "minutes of the one before; the latest pair at %s and %s"
                               % (len(found), len(close), _minutes(REPORT_GAP), utc(a.at), utc(b.at)),
                               "Limit: one every %d minutes per goal" % _minutes(REPORT_GAP)),
                              b.at))
    return out


def lane_no_success(f: Facts) -> List[Breach]:
    """A lane with RUNS_MIN or more runs in the last day and none done."""
    if f.runs is None:
        return []
    lanes: Dict[str, List[Run]] = {}
    for r in f.runs:
        if 0 <= f.now - r.at <= DAY:
            lanes.setdefault(r.lane, []).append(r)
    out = []
    for lane, runs in sorted(lanes.items()):
        if len(runs) < RUNS_MIN or any(r.done for r in runs):
            continue
        last = max(runs, key=lambda r: r.at)
        out.append(Breach("lane-no-success", lane,
                          "Lane %s: %d runs in 24 hours and none done" % (lane, len(runs)),
                          ("%d runs since %s, every one failed" % (len(runs), utc(f.now - DAY)),
                           "Last failure at %s: %s" % (utc(last.at), public(last.reason)
                                                       or "no reason was recorded")),
                          last.at))
    return out


def lane_held(f: Facts) -> List[Breach]:
    """A lane held by a limit for over HELD_MAX while tickets of its tier
    are queued."""
    if f.held is None or f.queued is None:
        return []
    out = []
    for lane, since in sorted(f.held.items()):
        if f.now - since <= HELD_MAX:
            continue
        waiting = [ref for ref, lanes in f.queued if lane in lanes]
        if not waiting:
            continue
        shown = ", ".join(r.label for r in waiting[:SHOWN])
        more = " and %d more" % (len(waiting) - SHOWN) if len(waiting) > SHOWN else ""
        out.append(Breach("lane-held", lane,
                          "Lane %s held by a limit for %s while %d tickets of its tier wait"
                          % (lane, _hours(f.now - since), len(waiting)),
                          ("Held by a limit since %s" % utc(since),
                           "Queued for its tier: %s%s" % (shown, more)),
                          f.now))
    return out


def _refs(found: Sequence[Ref]) -> str:
    shown = ", ".join(r.label for r in list(found)[:SHOWN])
    return shown + (" and %d more" % (len(found) - SHOWN) if len(found) > SHOWN else "")


def ticket_outside_epic(f: Facts) -> List[Breach]:
    """A goal with open loom:ticket issues that sit under no epic: every
    ticket belongs to an epic (decision 0003). One breach per goal."""
    if f.shapes is None:
        return []
    return [Breach("ticket-outside-epic", shape.goal.key,
                   "Goal home %s has %d open tickets under no epic"
                   % (shape.goal.label, len(shape.outside)),
                   ("Open tickets that are the goal's own children, not an epic's: %s"
                    % _refs(shape.outside),
                    "Move each under an open epic of the goal (its sub-issue, with a "
                    "Part of line naming the epic)"),
                   f.now)
            for shape in f.shapes if shape.outside]


def epic_over_cap(f: Facts) -> List[Breach]:
    """An epic holding more than EPIC_TICKETS tickets."""
    if f.shapes is None:
        return []
    return [Breach("epic-over-cap", epic.epic.key,
                   "Epic %s holds %d tickets (cap %d)" % (epic.epic.label, epic.tickets,
                                                          EPIC_TICKETS),
                   ("Epic %s of goal home %s holds %d tickets; an epic holds at most %d, "
                    "and more work goes into a next epic" % (
                        epic.epic.label, shape.goal.label, epic.tickets, EPIC_TICKETS),),
                   f.now)
            for shape in f.shapes for epic in shape.epics
            if epic.tickets > EPIC_TICKETS and not epic.standing]


def goal_epics_over_cap(f: Facts) -> List[Breach]:
    """A goal with more open epics than its cap (6, or its recorded
    exception)."""
    if f.shapes is None:
        return []
    out = []
    for shape in f.shapes:
        opened = [e.epic for e in shape.epics if e.open and not e.standing]
        if len(opened) > shape.cap:
            out.append(Breach("goal-epics-over-cap", shape.goal.key,
                              "Goal home %s has %d open epics (cap %d)"
                              % (shape.goal.label, len(opened), shape.cap),
                              ("Open epics: %s" % _refs(opened),
                               "Finish an epic before opening another, or record a one-time "
                               "exception on the goal home"),
                              f.now))
    return out


def closed_epic_open_tickets(f: Facts) -> List[Breach]:
    """A closed epic that still holds open tickets."""
    if f.shapes is None:
        return []
    return [Breach("closed-epic-open-tickets", epic.epic.key,
                   "Closed epic %s holds %d open tickets" % (epic.epic.label,
                                                            len(epic.open_tickets)),
                   ("Open tickets under it: %s" % _refs(epic.open_tickets),
                    "Reopen the epic, or move the tickets under an open one"),
                   f.now)
            for shape in f.shapes for epic in shape.epics
            if not epic.open and epic.open_tickets]


def plan_check_failed(f: Facts) -> List[Breach]:
    """An open lane pull request whose plan check failed over PLAN_FAILED_AGE ago."""
    if f.checks is None:
        return []
    return [Breach("plan-check-failed", c.pr.key,
                   "Lane %s held by its %s check for %d minutes"
                   % (c.pr.label, c.name, _minutes(f.now - c.at)),
                   tuple(["%s: %s at %s" % (c.name, c.conclusion, utc(c.at))]
                         + (["It says: %s" % public(c.title)] if c.title.strip() else [])),
                   c.at)
            for c in f.checks
            if c.conclusion in PLAN_FAILED and f.now - c.at > PLAN_FAILED_AGE]


def _stall(p: LanePull) -> Tuple[Optional[str], str]:
    """Why nothing drives this open lane pull request, and the one thing that
    unsticks it: (None, "") when something does drive it."""
    if not p.drives:
        return ("no open loom:ticket carries it",
                "make its ticket an open loom:ticket and move it to loom:pr with "
                "python3 -m bus.brain move OWNER/NAME#N --to pr, or close the pull request "
                "as superseded")
    if "loom:failed" in p.labels:
        return ("its ticket is past the last rung of the model ladder",
                "brief the work again as a new loom:ticket with "
                "python3 -m bus.brain brief OWNER/NAME --title TITLE --file BODY.md, or "
                "close the pull request as superseded")
    if "needs-brain" in p.labels:
        return ("its ticket waits on the brain (needs-brain)",
                "review it in a brain tick and move it on: python3 -m bus.brain merge "
                "OWNER/NAME#N, or python3 -m bus.brain fix OWNER/NAME#N --ticket "
                "OWNER/NAME#N --file FIX.md")
    if p.threads and not p.fix_pending:
        return ("%d open review thread%s and no fix round queued"
                % (p.threads, "" if p.threads == 1 else "s"),
                "send it back to a lane with "
                "python3 -m bus.brain fix OWNER/NAME#N --ticket OWNER/NAME#N --file FIX.md")
    return (None, "")


def _ticket_line(p: LanePull) -> str:
    """What the invariant can say about the issue behind a lane pull request:
    the one it found, whether the factory can move it, and the labels that
    explain why it does not."""
    if p.ticket is None:
        return "Ticket: the pull request names none that could be read"
    marks = [n for n in ("loom:failed", "needs-brain") if n in p.labels]
    return "Ticket: %s%s%s" % (p.ticket.label,
                              "" if p.drives else ", which is not an open loom:ticket",
                              " (%s)" % ", ".join(marks) if marks else "")


def lane_pr_stalled(f: Facts) -> List[Breach]:
    """An open lane pull request nothing drives for over PR_STALLED: no open
    loom:ticket carries it, its ticket is past the last rung of the model
    ladder, its ticket waits on the brain, or it holds open review threads
    with no fix round queued (loom#715). Each breach names its own reason and
    the one thing that unsticks it."""
    if f.lane_pulls is None:
        return []
    out = []
    for p in f.lane_pulls:
        if f.now - p.at <= PR_STALLED:
            continue
        why, fix = _stall(p)
        if why is None:
            continue
        out.append(Breach("lane-pr-stalled", p.pr.key,
                          "Lane %s open %s: %s" % (p.pr.label, _hours(f.now - p.at), why),
                          ("Opened at %s and still open after %s (limit: %s)"
                           % (utc(p.at), _hours(f.now - p.at), _hours(PR_STALLED)),
                           _ticket_line(p),
                           "Unstick it: %s" % fix),
                          f.now))
    return out


def remote_silent(f: Facts) -> List[Breach]:
    """A remote lane whose newest heartbeat is older than its beat_max. A
    lane that never posted one is not running anywhere and is left alone."""
    if f.beats is None:
        return []
    out = []
    for lane, at in sorted(f.beats.items()):
        limit = REMOTE_LANES.get(lane, {}).get("beat_max")
        if at is None or not isinstance(limit, (int, float)) or f.now - at <= limit:
            continue
        out.append(Breach("remote-silent", lane,
                          "Remote lane %s has not posted a heartbeat for %s" % (lane, _hours(f.now - at)),
                          ("Newest heartbeat: %s" % utc(at), "Limit: %s" % _hours(limit)),
                          f.now))
    return out


def gate_hold(f: Facts) -> List[Breach]:
    """A goal the unproven-work gate refused briefed tickets for past
    GATE_HOLD_MAX: the refusal would otherwise live only in the station log
    until the operator asks why nothing moves (loom#564). The breach clears
    when the gate stops holding the goal, as other invariants do."""
    if f.gate_held is None:
        return []
    out = []
    for key, hold in sorted(f.gate_held.items()):
        if f.now - hold.since < GATE_HOLD_MAX:
            continue
        shown = ", ".join(t.label for t in hold.tickets[:SHOWN])
        more = (" and %d more" % (len(hold.tickets) - SHOWN)
                if len(hold.tickets) > SHOWN else "")
        out.append(Breach("gate-hold", key,
                          "The unproven-work gate holds goal %s for %s"
                          % (hold.goal.label, _hours(f.now - hold.since)),
                          ("The station refused every briefed ticket of goal %s for %s"
                           % (hold.goal.label, _hours(f.now - hold.since)),
                           "%d tickets shipped without a proof (limit %d)"
                           % (hold.count, hold.limit),
                           "Briefed tickets waiting: %s%s" % (shown or "none", more),
                           "%d lanes idle meanwhile" % hold.idle),
                          f.now))
    return out


def _decision(kind: str) -> str:
    """The decision an event kind reports on: rewrite_failed, rewrite_skipped
    and rewrite_succeeded all report on rewrite."""
    for suffix in ("_succeeded", "_failed", "_skipped"):
        if kind.endswith(suffix):
            return kind[:-len(suffix)]
    return kind


def repeated_failure(f: Facts) -> List[Breach]:
    """A refused decision that began at least an hour ago, still repeats in
    the last hour, and saw no success of its decision since it began. Any
    success suppresses every failure kind and reason of its decision, since a
    success logs its action, not the failures' reason."""
    if f.events is None:
        return []
    recent = [e for e in f.events if f.now - EVENT_WINDOW <= e.at <= f.now and e.reason]
    succeeded: Dict[str, float] = {}
    for event in recent:
        if event.kind.endswith("_succeeded"):
            decision = _decision(event.kind)
            succeeded[decision] = max(event.at, succeeded.get(decision, event.at))
    failures: Dict[Tuple[str, str], List[float]] = {}
    for event in recent:
        if event.kind in FAILURE_KINDS and event.at > succeeded.get(_decision(event.kind), -1.0):
            failures.setdefault((event.kind, event.reason), []).append(event.at)
    out = []
    for (kind, reason), stamps in sorted(failures.items()):
        first, last = min(stamps), max(stamps)
        if f.now - first < REPEATED_FAILURE or f.now - last >= REPEATED_FAILURE:
            continue
        out.append(Breach("repeated-failure", kind + "|" + reason,
                          "Repeated %s for one hour: %s" % (kind.replace("_", " "), public(reason)),
                          ("%d matching events since %s" % (len(stamps), utc(first)),
                           "Most recent: %s" % utc(last)), f.now))
    return out


def untrusted_labeler(f: Facts) -> List[Breach]:
    """A login whose briefed-ticket labels the station refused more than
    LABELER_REFUSALS_MAX times in an hour: a trust gap (a credential change,
    a new bot login) that would otherwise live only in the station log while
    no new work starts (loom#660). It clears when the refusals stop."""
    if f.label_refusals is None:
        return []
    out = []
    for login, count in sorted(f.label_refusals.items()):
        if count <= LABELER_REFUSALS_MAX:
            continue
        out.append(Breach("untrusted-labeler", login,
                          "Briefed tickets were refused %d times in an hour for labeller %s"
                          % (count, login),
                          ("%d briefed tickets were refused because %s applied their labels"
                           % (count, login),
                           "Add the login to LOOM_BRIEF_AUTHORS (or LOOM_TRUSTED_BRIEFERS) "
                           "if it is yours to trust",
                           "No new work starts for those tickets until it is trusted"),
                          f.now))
    return out


INVARIANTS: Tuple[Callable[[Facts], List[Breach]], ...] = (
    brain_failing, brain_silent, marker_cadence, lane_no_success, lane_held,
    ticket_outside_epic, epic_over_cap, goal_epics_over_cap, closed_epic_open_tickets,
    plan_check_failed, lane_pr_stalled, remote_silent, gate_hold, repeated_failure,
    untrusted_labeler)


def check(facts: Facts, say: Say,
          invariants: Sequence[Callable[[Facts], List[Breach]]] = INVARIANTS) -> List[Breach]:
    """Every breach of every invariant; one that raises is logged and skipped."""
    out: List[Breach] = []
    for invariant in invariants:
        try:
            out += invariant(facts)
        except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError) as err:
            say("invariants: %s raised %s: %s" % (invariant.__name__, type(err).__name__, err))
    return out


# -- filing --------------------------------------------------------------
def _epoch(stamp: Any) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, OverflowError):
        return None


def _checked(line: str) -> str:
    try:
        _text.check(line)
    except ValueError:
        return WITHHELD
    return line


class Filer:
    def __init__(self, gh: Any, repo: str, goal: int, state: Dict[str, Any], now: float,
                 say: Say, write: bool = True) -> None:
        """gh files in repo under goal home #goal. state maps a breach key to
        its issue number and is changed in place (the caller keeps it);
        write False (the GitHub budget is low) files nothing this pass."""
        self.gh = gh
        self.repo = repo
        self.goal = goal
        self.state = state
        self.now = now
        self.say = say
        self.write = write
        self.writes = 0
        self._listed: Optional[Dict[str, int]] = None

    def _listing(self) -> Dict[str, int]:
        """Breach key -> issue number for every loom:invariant issue, open or
        closed, read once a pass and only when the state does not know a key."""
        if self._listed is None:
            self._listed = {}
            for issue in self.gh.paginate("/repos/%s/issues?labels=%s&state=all"
                                          % (self.repo, LABEL)):
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                m = MARKER_RE.search(str(issue.get("body") or ""))
                if m and m.group(1) not in self._listed:
                    self._listed[m.group(1)] = int(issue["number"])
        return self._listed

    def _issue(self, number: int) -> Optional[Dict[str, Any]]:
        try:
            data = self.gh.rest("GET", "/repos/%s/issues/%d" % (self.repo, number)).data
        except GitHubError as err:
            if err.status in (404, 410):
                return None
            raise
        return data if isinstance(data, dict) else None

    def _evidence(self, b: Breach) -> List[str]:
        return ["- " + _checked(line) for line in b.evidence]

    def _home(self) -> int:
        """Where breaches are filed: the goal's standing repairs epic, or the
        goal home itself when the epic cannot be read or opened."""
        if getattr(self, "_repairs", None) is None:
            try:
                self._repairs = brain.repairs_epic(self.gh, self.repo, self.goal)
            except (GitHubError, ValueError, KeyError) as err:
                self.say("invariants: filing under goal #%d, no repairs epic: %s"
                         % (self.goal, err))
                self._repairs = self.goal
        return self._repairs

    def _body(self, b: Breach) -> str:
        return "\n".join(
            [MARKER % b.key,
             "The station found one of the factory's own invariants broken: %s." % b.title,
             "", "Evidence:"] + self._evidence(b) +
            ["", "Invariant: `%s` (bus/invariants.py), checked every station pass. Filed "
                 "without a model: this says what broke, not why. Rewrite it as a ticket "
                 "that fixes the cause." % b.invariant,
             "", "Part of #%d" % self._home()])

    def _file(self, b: Breach) -> None:
        title = b.title
        body = self._body(b)
        _text.check(title + "\n" + body)
        made = self.gh.rest("POST", "/repos/%s/issues" % self.repo,
                            {"title": title, "body": body, "labels": LABELS}).data
        self.writes += 1
        number = int(made["number"])
        self.state[b.key] = number
        try:
            self.gh.rest("POST", "/repos/%s/issues/%d/sub_issues" % (self.repo, self._home()),
                         {"sub_issue_id": made["id"]})
        except (GitHubError, KeyError) as err:
            # A refused link (a goal at GitHub's sub-issue limit): the Part
            # of line still names where it belongs.
            self.say("invariants: %s#%d is linked to #%d by its Part of line only: %s"
                     % (self.repo, number, self._home(), err))
        self.say("invariants: filed %s#%d: %s" % (self.repo, number, title))

    def _reopen(self, b: Breach, number: int) -> None:
        body = "\n".join([MARKER % b.key,
                          "Broken again after this was closed: %s." % b.title,
                          "", "Evidence:"] + self._evidence(b))
        _text.check(body)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number), {"state": "open"})
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (self.repo, number), {"body": body})
        self.writes += 1
        self.say("invariants: reopened %s#%d: %s" % (self.repo, number, b.title))

    def _comments(self, number: int) -> List[Dict[str, Any]]:
        return [c for c in self.gh.paginate("/repos/%s/issues/%d/comments?per_page=100"
                                            % (self.repo, number)) if isinstance(c, dict)]

    def _clear_stale(self, current: Dict[str, Breach]) -> None:
        for key, number in self._listing().items():
            if self.writes >= WRITES_MAX:
                return
            if key in current:
                continue
            issue = self._issue(number)
            if issue is None or issue.get("state") != "open":
                continue
            labels = [str(x.get("name", "")) for x in issue.get("labels", [])
                      if isinstance(x, dict)]
            if "loom:triage" not in labels:
                continue
            comments = self._comments(number)
            breaches = [_epoch(issue.get("created_at"))]
            breaches += [_epoch(c.get("created_at")) for c in comments
                         if "Broken again after this was closed:" in str(c.get("body", ""))]
            times = [at for at in breaches if at is not None]
            if not times or self.now - max(times) < CLEAR_AFTER:
                continue
            recent = [at for at in times if 0 <= self.now - at <= RECURRING_AFTER]
            if len(recent) >= 3:
                if "loom:recurring" not in labels:
                    self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                                 {"labels": labels + ["loom:recurring"]})
                    self.writes += 1
                continue
            if self.writes + 2 > WRITES_MAX:
                return
            body = ("The invariant cleared by %s; its last breach was observed at %s, "
                    "more than 6 hours earlier." % (utc(self.now), utc(max(times))))
            already_explained = any("The invariant cleared by " in str(c.get("body", ""))
                                    for c in comments)
            if not already_explained:
                _text.check(body)
                self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (self.repo, number),
                             {"body": body})
                self.writes += 1
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                         {"state": "closed", "state_reason": "not_planned"})
            self.writes += 1
            self.say("invariants: closed cleared %s#%d" % (self.repo, number))

    def _one(self, b: Breach) -> None:
        known = self.state.get(b.key)
        number = (known if isinstance(known, int) and not isinstance(known, bool)
                  else self._listing().get(b.key))
        issue = self._issue(number) if number is not None else None
        if number is None or issue is None:
            self.state.pop(b.key, None)
            self._file(b)
            return
        self.state[b.key] = number
        if issue.get("state") == "open":
            return
        closed = _epoch(issue.get("closed_at"))
        if closed is None or b.at > closed:
            self._reopen(b, number)

    def run(self, breaches: Iterable[Breach]) -> None:
        current = {b.key: b for b in breaches}
        for b in current.values():
            if not self.write or self.writes >= WRITES_MAX:
                return
            try:
                self._one(b)
            except (GitHubError, OSError, ValueError, KeyError, TypeError) as err:
                self.say("invariants: could not file %s (%s): %s" % (b.invariant, b.title, err))
        if self.write and self.writes < WRITES_MAX:
            try:
                self._clear_stale(current)
            except (GitHubError, OSError, ValueError, KeyError, TypeError) as err:
                self.say("invariants: could not close cleared issues: %s" % err)


def load(path: str, say: Say) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        # Lost issue numbers are found again by the label listing.
        say("invariants: unreadable state %s, starting over: %s" % (os.path.basename(path), err))
        return {}
    return state if isinstance(state, dict) else {}


def save(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)
