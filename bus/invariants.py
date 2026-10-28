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
newer than the close. A rolling breach (ticket-outside-epic, lane-held,
lane-failing-streak, silent-lane, roll-pin-off) is one issue for its whole
cause, a line per goal home or lane, and its open issue is rewritten in place whenever what it
lists changes; an older open issue of the same invariant is folded into it.
lane-no-success stays one issue per lane: the station reads its per-lane keys
to release runaways a dead lane held. Adding an invariant is one function in
INVARIANTS plus a test.

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
from . import journal
from . import naming
from . import notify
from . import shadow
from . import text as _text
from . import watch
from .github import GitHubError, RateLimited
from .labels import REMOTE_LANES

DAY = 24 * 3600
CLEAR_AFTER = 6 * 3600
RECURRING_AFTER = 7 * DAY
# No journal from any brain for this long is a brain that stopped ticking.
BRAIN_SILENT = 90 * 60
# Two loom:report comments on one goal closer than this are one too many; the
# same gap bus journal and brain status hold a report back by.
REPORT_GAP = journal.REPORT_GAP
RUNS_MIN = 5
# A lane whose newest this-many runs all failed, not counting a limit or an
# expired key, is failing now even with a success earlier in the day.
FAILING_STREAK = 5
HELD_MAX = 2 * 3600
# A gate that has refused a goal's briefed tickets for this long gets its own
# invariant naming it (loom#564).
GATE_HOLD_MAX = 3600
# How long a lane that held a running ticket while silent stays a breach after
# the factory last found one: the move takes the ticket off the lane, so the
# breach would otherwise be gone before the next invariant check.
SILENT_KEEP = 2 * 3600
# One refused decision repeating this long with no success of its kind is an
# invariant (loom#593). The event log is read EVENT_WINDOW back, so a run that
# began just over an hour ago is seen whole between irregular station passes.
REPEATED_FAILURE = 3600
EVENT_WINDOW = 2 * REPEATED_FAILURE
FAILURE_KINDS = ("rewrite_failed", "rewrite_skipped", "model_wait", "not_claiming")
# Decision 0003: an epic holds at most EPIC_TICKETS tickets.
EPIC_TICKETS = 15
# GitHub refuses a sub-issue past this many under one issue.
SUB_ISSUE_LIMIT = brain.SUB_ISSUE_LIMIT
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
# The one note an issue carries for every time its breach came back, edited
# with a count and the last time (never a new comment per reopen).
BROKEN_MARKER = "<!-- loom:broken-again -->"
BROKEN_TEXT = "Broken again after this was closed:"
BROKEN_TIMES_RE = re.compile(r"<!-- loom:broken-again times=([0-9,]*) -->")
BROKEN_LAST_RE = re.compile(r"last at ([0-9T:Z-]+)")
BROKEN_COUNT_RE = re.compile(r"Reopened (\d+) time")
# Times kept in the note: enough for the 3-in-RECURRING_AFTER count.
BROKEN_TIMES_MAX = 12
CLEARED_TEXT = "The invariant cleared by "
TICKET_LABEL = "loom:ticket"
MARKER_RE = re.compile(r"<!-- loom:invariant v1 key=([a-z-]+/[0-9a-f]{12}) -->")
# Issues filed plus issues reopened in one pass.
WRITES_MAX = 5
SHOWN = 5
# The one subject of the rolling ticket-outside-epic breach: every goal home
# with tickets under no epic is a line in one issue, not an issue of its own
# (one backlog sweep found 18 issues for this one cause).
OUTSIDE_EPIC_SUBJECT = "every goal home"
# The one subject of the rolling lane-held, lane-failing-streak and
# silent-lane breaches: each lane is a line in one issue per invariant. These
# alarm in bursts with one shared cause: one provider limit opened 9 lane-held
# issues in 6 minutes (2026-10-15), and 6 failing-streak issues opened in 27
# minutes (2026-10-20).
LANES_SUBJECT = "every lane"
# The one subject of the rolling ticket-waiting breach: each reason the
# factory gives for a ticket's wait is a line in one issue, so a lane outage
# that holds forty tickets is one line, not forty issues.
TICKETS_SUBJECT = "every ticket"
# The one subject of the rolling roll-pin-off breach: each goal home that
# ships by roll with no roll ship document is a line in one issue.
ROLL_SUBJECT = "every roll goal"
# The one subject of the rolling merged-not-shipped breach: each ticket still
# at loom:merged MERGED_UNSHIPPED_AFTER after a ship rule found its commit
# deployed is a line in one issue (loom#2025).
UNSHIPPED_SUBJECT = "every deployed merged ticket"
MERGED_UNSHIPPED_AFTER = 24 * 3600
# How long a ticket may wait for one reason the factory repeats every pass
# before it is raised: six fix rounds waited nine hours on a lane at a limit
# above the last rung of the ladder, logged every pass and raised by nothing
# (2026-10-21).
WAIT_LIMIT = 6 * 3600
# The line of a rolling issue that changes every pass; it is left out when
# the body is compared, so an unchanged list writes nothing.
LAST_SEEN = "Last seen by the station: %s."
LINE_MAX = 300
# Invariants kept as one standing issue (loom#845): when the cause clears the
# issue says so in place and stays open, and the next breach edits it again,
# so a hold that comes and goes never closes and reopens an issue (each reopen
# mailed the people watching it).
STANDING = ("lane-held",)
STANDING_CLEAR_TITLE = "No lane is held by a limit now"
CLEAR_SINCE = "Clear since %s."
# What the body keeps of past holds: a line per hold, newest first.
HOLD_LOG = "Hold log (newest first, last %d):"
HOLD_LOG_MAX = 20
HELD_LINE_RE = re.compile(r"^- ([A-Za-z0-9._-]+): held by a limit since (\S+?);", re.M)
LOG_LINE_RE = re.compile(r"^- [A-Za-z0-9._-]+: held since \S+, cleared \S+$")
WITHHELD = "(withheld: the line fails the written-text check)"
# Invariants whose subject can be gone for good: a lane pull request merged or
# closed, an epic back at its cap. The filer keeps each breach's subject in
# its local state and, once the breach is gone, asks the caller's settled()
# whether the subject itself settled; one that did closes its issue on that
# pass, before any other clear, whatever labels it carries (loom#2022: five
# lane-pr-stalled issues stayed open up to a day after their pull requests
# merged or closed, and an operator closed them by hand).
SETTLES = ("lane-pr-stalled", "epic-over-cap")

Say = Callable[[str], None]
# (invariant, subject) -> why the subject is settled, in words a public issue
# may carry, or None while it is not (or cannot be read).
Settled = Callable[[str, str], Optional[str]]


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
    # How many sub-issues GitHub holds under it: at SUB_ISSUE_LIMIT it takes
    # no more links, standing or not (loom#2023).
    sub_issues: int = 0
    # Its title: two open epics of one repo must not share one (loom#2030).
    # Never written into a breach, which names epics by their labels only.
    title: str = ""


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


class Wait(NamedTuple):
    """A ticket the factory leaves where it is, each pass, for the same
    reason (bus/factory.py Factory.waits): the reason without the ticket's
    name, and since when it has given that reason."""
    ticket: Ref
    reason: str
    since: float


class SilentRun(NamedTuple):
    """A lane that held running tickets while it sent no heartbeat: how long
    it had been silent when the factory found them, the tickets, and when."""
    lane: str
    silent_for: float
    tickets: Tuple[Ref, ...]
    at: float


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
    # The held lanes that are out of credit on an account the operator never
    # tops up (LOOM_NO_TOPUP): the hold lasts until the credit renews, and
    # nothing is briefed onto them meanwhile, so it is no breach.
    no_topup: Optional[Tuple[str, ...]] = None
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
    # Lanes that held a running ticket while silent, in the last SILENT_KEEP.
    silent_runs: Optional[List[SilentRun]] = None
    events: Optional[List[Event]] = None
    # untrusted labeler login -> briefed tickets refused for it in the last
    # LABELER_WINDOW seconds.
    label_refusals: Optional[Dict[str, int]] = None
    # Last, so no positional construction shifts.
    waits: Optional[List[Wait]] = None
    # The goal homes whose body names a roll that no LOOM_ROLL_SHIP_DOCS
    # entry names, and how many entries there are.
    roll_goals: Optional[List[Ref]] = None
    roll_docs: int = 0
    # Each open loom:merged ticket a ship rule found deployed but could not
    # move, with when it was first found deployed (loom#2025).
    unshipped: Optional[List[Tuple[Ref, float]]] = None


@dataclass(frozen=True)
class Breach:
    invariant: str
    subject: str
    title: str
    evidence: Tuple[str, ...]
    # The newest evidence: a closed issue reopens only for a later breach.
    at: float
    # One issue for the whole cause, its title and body rewritten in place
    # while it is open and what it lists changes.
    rolling: bool = False

    @property
    def key(self) -> str:
        return "%s/%s" % (self.invariant, hashlib.sha256(self.subject.encode()).hexdigest()[:12])


def _standing_keys() -> set:
    """The keys of the standing issues: the rolling breach of each STANDING
    invariant. An older per-lane issue of the same invariant is not one."""
    return {Breach(inv, LANES_SUBJECT, "", (), 0.0).key for inv in STANDING}


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


def _lanes(count: int) -> str:
    """"1 lane" or "N lanes", for a rolling lane title."""
    return "%d lane%s" % (count, "" if count == 1 else "s")


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


# A brief the runner parked before its agent ran: the fix round's pull
# request had merged or closed, or another push moved its branch
# (lanes/run_lane.sh fix_pr_open and the branch-moved park).
_PARKED_UNRUN = re.compile(r"(\[[a-z]+\] )?(pull request #[0-9]+ is no longer open on |branch moved: )")


def _says_lane_health(r: Run) -> bool:
    """A run whose end says something about the lane itself: a limit, an
    expired key, a factory incident (the repo's check or setup) or a brief
    parked before its agent ran does not."""
    return not (r.reason.startswith(("[limit]", "[auth]", "[factory]"))
                or _PARKED_UNRUN.match(r.reason))


def lane_no_success(f: Facts) -> List[Breach]:
    """A lane with RUNS_MIN or more runs in the last day and none done, runs
    that ended at a limit, an expired key or a factory incident aside: agy's
    34 runs at its plan's quota read as a broken lane (loom#651, 2026-10-20),
    and a lane held at a limit is lane_held's."""
    if f.runs is None:
        return []
    lanes: Dict[str, List[Run]] = {}
    for r in f.runs:
        if 0 <= f.now - r.at <= DAY and _says_lane_health(r):
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


def lane_failing_streak(f: Facts) -> List[Breach]:
    """Lanes whose newest FAILING_STREAK runs in the last day all failed,
    a limit, an expired key or a factory incident aside, after a success
    earlier that day (a lane with none is lane_no_success). On 2026-10-19
    zen's free model left the
    catalogue at about 12:13Z and every run after failed, while a success at
    05:00Z kept lane_no_success quiet all day. One rolling breach, a line per
    lane."""
    if f.runs is None:
        return []
    lanes: Dict[str, List[Run]] = {}
    for r in f.runs:
        # Two base-check incidents counted toward zen's streak (2026-10-19).
        if 0 <= f.now - r.at <= DAY and _says_lane_health(r):
            lanes.setdefault(r.lane, []).append(r)
    lines, last = [], []
    for lane, runs in sorted(lanes.items()):
        runs.sort(key=lambda r: r.at, reverse=True)
        newest = runs[:FAILING_STREAK]
        if (len(newest) < FAILING_STREAK or any(r.done for r in newest)
                or not any(r.done for r in runs)):
            continue
        lines.append("%s: %d runs in a row failed since %s; the last at %s: %s"
                     % (lane, FAILING_STREAK, utc(newest[-1].at), utc(newest[0].at),
                        public(newest[0].reason) or "no reason was recorded"))
        last.append(newest[0].at)
    if not lines:
        return []
    return [Breach("lane-failing-streak", LANES_SUBJECT,
                   "%s failed %s last %d runs" % (_lanes(len(lines)),
                                           "its" if len(lines) == 1 else "their", FAILING_STREAK),
                   ("Lanes whose newest %d runs all failed after a success earlier in the "
                    "day, limits and expired keys aside:" % FAILING_STREAK,) + tuple(lines),
                   max(last), rolling=True)]


def lane_held(f: Facts) -> List[Breach]:
    """Lanes held by a limit for over HELD_MAX while tickets of their tier
    are queued: one rolling breach, a line per lane. A lane out of credit on
    an account the operator never tops up (Facts.no_topup) is skipped:
    stamped with the pass time, its breach reopened every day until the
    credit renewed (loom#399 to loom#406)."""
    if f.held is None or f.queued is None:
        return []
    lines = []
    for lane, since in sorted(f.held.items()):
        if f.now - since <= HELD_MAX or lane in (f.no_topup or ()):
            continue
        waiting = [ref for ref, lanes in f.queued if lane in lanes]
        if not waiting:
            continue
        # The hold's start, not its length: a line that changes every pass
        # would rewrite the issue every pass.
        lines.append("%s: held by a limit since %s; %d tickets of its tier queued: %s"
                     % (lane, utc(since), len(waiting), _refs(waiting)))
    if not lines:
        return []
    return [Breach("lane-held", LANES_SUBJECT,
                   "%s held by a limit while %s" % (_lanes(len(lines)),
                                    "its tier waits" if len(lines) == 1 else "their tiers wait"),
                   ("Lanes held by a limit for over %s while tickets of their tier are "
                    "queued:" % _hours(HELD_MAX),) + tuple(lines),
                   f.now, rolling=True)]


def _refs(found: Sequence[Ref]) -> str:
    shown = ", ".join(r.label for r in list(found)[:SHOWN])
    return shown + (" and %d more" % (len(found) - SHOWN) if len(found) > SHOWN else "")


def ticket_outside_epic(f: Facts) -> List[Breach]:
    """Goals with open loom:ticket issues that sit under no epic: every
    ticket belongs to an epic (decision 0003). One rolling breach for all
    goals, a line per goal, so one cause is one issue however many goals
    it touches."""
    if f.shapes is None:
        return []
    shapes = [shape for shape in f.shapes if shape.outside]
    if not shapes:
        return []
    total = sum(len(shape.outside) for shape in shapes)
    lines = tuple("%s: %d open, %s" % (shape.goal.label, len(shape.outside),
                                       _refs(shape.outside)) for shape in shapes)
    return [Breach("ticket-outside-epic", OUTSIDE_EPIC_SUBJECT,
                   "%d open tickets sit under no epic across %d goal homes"
                   % (total, len(shapes)),
                   ("Open tickets that are a goal's own children, not an epic's, "
                    "by goal home:",) + lines +
                   ("Move each under an open epic of its goal (its sub-issue, with a "
                    "Part of line naming the epic)",),
                   f.now, rolling=True)]


def epic_over_cap(f: Facts) -> List[Breach]:
    """An epic holding more than EPIC_TICKETS tickets (its sub-issues and
    the issues whose Part of line names it, each once), or any epic, the
    standing repairs epic too, holding GitHub's limit of sub-issues: GitHub
    refuses its next link (loom#2023)."""
    if f.shapes is None:
        return []
    out = []
    for shape in f.shapes:
        for epic in shape.epics:
            if epic.open and epic.sub_issues >= SUB_ISSUE_LIMIT:
                out.append(Breach(
                    "epic-over-cap", epic.epic.key,
                    "Epic %s holds %d sub-issues (GitHub's limit %d)"
                    % (epic.epic.label, epic.sub_issues, SUB_ISSUE_LIMIT),
                    ("Epic %s of goal home %s holds %d sub-issues; GitHub refuses a sub-issue "
                     "past %d, so new work goes into a next epic" % (
                         epic.epic.label, shape.goal.label, epic.sub_issues,
                         SUB_ISSUE_LIMIT),),
                    f.now))
            elif epic.tickets > EPIC_TICKETS and not epic.standing:
                out.append(Breach(
                    "epic-over-cap", epic.epic.key,
                    "Epic %s holds %d tickets (cap %d)" % (epic.epic.label, epic.tickets,
                                                           EPIC_TICKETS),
                    ("Epic %s of goal home %s holds %d tickets; an epic holds at most %d, "
                     "and more work goes into a next epic" % (
                         epic.epic.label, shape.goal.label, epic.tickets, EPIC_TICKETS),),
                    f.now))
    return out


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


def epic_title_shared(f: Facts) -> List[Breach]:
    """Two or more open epics of one repo with the same title: rewrites and
    people pick an epic by its title, and five intake epics with one title
    split their tickets across all five (loom#2030). The breach names the
    epics by their labels, never by the title, which a private repo keeps."""
    if f.shapes is None:
        return []
    groups: Dict[Tuple[str, str], Dict[str, Ref]] = {}
    for shape in f.shapes:
        for epic in shape.epics:
            title = " ".join(epic.title.split()).lower()
            if not epic.open or not title:
                continue
            repo = epic.epic.key.rsplit("#", 1)[0]
            groups.setdefault((repo, title), {})[epic.epic.key] = epic.epic
    out: List[Breach] = []
    for found in groups.values():
        if len(found) < 2:
            continue
        refs = sorted(found.values(), key=lambda r: _ref_number(r.key))
        out.append(Breach("epic-title-shared", refs[0].key,
                          "%d open epics share the title of epic %s" % (len(refs), refs[0].label),
                          ("Open epics with one title: %s" % _refs(refs),
                           "Give each a distinct title (a new intake epic carries a batch "
                           "number), and move the tickets of the older ones under one"),
                          f.now))
    return out


def _ref_number(key: str) -> int:
    tail = key.rsplit("#", 1)[-1]
    return int(tail) if tail.isdigit() else 0


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


def _stall(p: LanePull) -> Tuple[Optional[str], str, str]:
    """Why nothing drives this open lane pull request, in a few words for
    the title and in full for the body, and the one thing that unsticks it:
    (None, "", "") when something does drive it."""
    if not p.drives:
        return ("no open loom:ticket carries it", "no open loom:ticket carries it",
                "make its ticket an open loom:ticket and move it to loom:pr with "
                "python3 -m bus.brain move OWNER/NAME#N --to pr, or close the pull request "
                "as superseded")
    if "loom:failed" in p.labels:
        return ("ticket past the model ladder",
                "its ticket is past the last rung of the model ladder",
                "brief the work again as a new loom:ticket with "
                "python3 -m bus.brain brief OWNER/NAME --title TITLE --file BODY.md, or "
                "close the pull request as superseded")
    if "needs-brain" in p.labels:
        return ("ticket waits on the brain",
                "its ticket waits on the brain (needs-brain)",
                "review it in a brain tick and move it on: python3 -m bus.brain merge "
                "OWNER/NAME#N, or python3 -m bus.brain fix OWNER/NAME#N --ticket "
                "OWNER/NAME#N --file FIX.md")
    if p.threads and not p.fix_pending:
        return ("%d review thread%s, no fix round" % (p.threads, "" if p.threads == 1 else "s"),
                "%d open review thread%s and no fix round queued"
                % (p.threads, "" if p.threads == 1 else "s"),
                "send it back to a lane with "
                "python3 -m bus.brain fix OWNER/NAME#N --ticket OWNER/NAME#N --file FIX.md")
    return (None, "", "")


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
        short, why, fix = _stall(p)
        if short is None:
            continue
        # A short reason, so the title fits the naming standard's 72
        # characters (docs/NAMING.md); the body says it in full.
        out.append(Breach("lane-pr-stalled", p.pr.key,
                          "Lane %s open %s: %s" % (p.pr.label, _hours(f.now - p.at), short),
                          ("Opened at %s and still open after %s (limit: %s)"
                           % (utc(p.at), _hours(f.now - p.at), _hours(PR_STALLED)),
                           "Stalled because %s" % why,
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


def silent_lane(f: Facts) -> List[Breach]:
    """Lanes that held a running ticket while they sent no heartbeat, so the
    claim could not end (loom#538): one rolling breach, a line per lane."""
    if f.silent_runs is None:
        return []
    found = [run for run in sorted(f.silent_runs, key=lambda r: r.lane)
             if 0 <= f.now - run.at <= SILENT_KEEP]
    if not found:
        return []
    lines = tuple("%s: silent for %s when found at %s; running tickets it held: %s"
                  % (run.lane, _hours(run.silent_for), utc(run.at), _refs(run.tickets) or "none")
                  for run in found)
    return [Breach("silent-lane", LANES_SUBJECT,
                   "%s held a running ticket while silent" % _lanes(len(found)),
                   ("Lanes found holding a running ticket with no heartbeat:",) + lines +
                   ("Each ticket moved to a free lane of its tier, or back to the queue",),
                   max(run.at for run in found), rolling=True)]


def ticket_waiting(f: Facts) -> List[Breach]:
    """Tickets the factory has left in place for over WAIT_LIMIT, each pass
    for the same reason: one rolling breach, a line per reason, so the brain
    sees what nothing in the factory moves and decides each line (climb the
    ticket, set its tier, rewrite it, or ask the operator one question)."""
    if f.waits is None:
        return []
    groups: Dict[str, List[Wait]] = {}
    for wait in f.waits:
        if f.now - wait.since > WAIT_LIMIT:
            groups.setdefault(wait.reason, []).append(wait)
    if not groups:
        return []
    lines = []
    for reason, found in sorted(groups.items(), key=lambda item: min(w.since for w in item[1])):
        found.sort(key=lambda w: w.since)
        lines.append(("%s: %d ticket%s, longest %s: %s" % (
            reason, len(found), "" if len(found) == 1 else "s",
            _hours(f.now - found[0].since), _refs([w.ticket for w in found])))[:LINE_MAX])
    count = sum(len(found) for found in groups.values())
    return [Breach("ticket-waiting", TICKETS_SUBJECT,
                   "%d ticket%s waited over %s for the same reason" % (
                       count, "" if count == 1 else "s", "%d hours" % (WAIT_LIMIT // 3600)),
                   ("Each line is a reason the factory gave every pass, with nothing "
                    "moving the ticket:",) + tuple(lines),
                   max(w.since for found in groups.values() for w in found), rolling=True)]


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


def roll_pin_off(f: Facts) -> List[Breach]:
    """Goal homes that ship by roll with no roll ship document: the station
    opens no source-pin pull request when their module changes merge."""
    if not f.roll_goals:
        return []
    why = ("No LOOM_ROLL_SHIP_DOCS entry names them" if f.roll_docs else
           "LOOM_ROLL_SHIP_DOCS is empty, so the station's roll source-pin pass is off")
    return [Breach("roll-pin-off", ROLL_SUBJECT,
                   ("1 goal home ships" if len(f.roll_goals) == 1 else
                    "%d goal homes ship" % len(f.roll_goals)) + " by roll with no source-pin pass",
                   ("Goal homes whose body names a roll: %s" % _refs(f.roll_goals),
                    why + "; their merged module changes wait for a brain to bump the pins",
                    "Give each ship document a loom-roll-pins block and add an entry for its "
                    "goal home to LOOM_ROLL_SHIP_DOCS in the station environment "
                    "(README.md, Roll source pins)"),
                   f.now, rolling=True)]


def merged_not_shipped(f: Facts) -> List[Breach]:
    """Tickets still at loom:merged MERGED_UNSHIPPED_AFTER after a ship rule
    found their merge commit deployed: the move to loom:shipped failed, and
    nothing else would say so (loom#2025). One rolling breach, a line per
    ticket; it clears once each has moved."""
    if not f.unshipped:
        return []
    late = sorted((at, ref) for ref, at in f.unshipped
                  if f.now - at >= MERGED_UNSHIPPED_AFTER)
    if not late:
        return []
    return [Breach("merged-not-shipped", UNSHIPPED_SUBJECT,
                   ("1 ticket stays" if len(late) == 1 else "%d tickets stay" % len(late))
                   + " at loom:merged over %d hours after deploy" % (MERGED_UNSHIPPED_AFTER // 3600),
                   ("A ship rule found each one's merge commit deployed, but its move to "
                    "loom:shipped failed (the station log names why):",)
                   + tuple("%s: deployed since %s" % (ref.label, utc(at)) for at, ref in late)
                   + ("Once the cause is fixed, move each with python3 -m bus.brain move "
                      "OWNER/NAME#N --to shipped",),
                   max(at for at, _ in late), rolling=True)]


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
                          "Labeller %s had %d briefed tickets refused in an hour"
                          % (login, count),
                          ("%d briefed tickets were refused because %s applied their labels"
                           % (count, login),
                           "Add the login to LOOM_BRIEF_AUTHORS (or LOOM_TRUSTED_BRIEFERS) "
                           "if it is yours to trust",
                           "No new work starts for those tickets until it is trusted"),
                          f.now))
    return out


INVARIANTS: Tuple[Callable[[Facts], List[Breach]], ...] = (
    brain_failing, brain_silent, marker_cadence, lane_no_success, lane_failing_streak,
    lane_held,
    ticket_outside_epic, epic_over_cap, goal_epics_over_cap, closed_epic_open_tickets,
    epic_title_shared, plan_check_failed, lane_pr_stalled, remote_silent, silent_lane, gate_hold,
    repeated_failure, ticket_waiting,
    untrusted_labeler, roll_pin_off, merged_not_shipped)


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
                 say: Say, write: bool = True,
                 authors: Optional[Sequence[str]] = None,
                 subjects: Optional[Dict[str, Any]] = None,
                 settled: Optional[Settled] = None) -> None:
        """gh files in repo under goal home #goal. state maps a breach key to
        its issue number and is changed in place (the caller keeps it);
        write False (the GitHub budget is low) files nothing this pass.
        `authors` (default brain.trusted_authors()) are the logins whose
        comments it reads: a stranger's marker is data. `subjects` maps the
        key of each SETTLES breach to its subject (kept by the caller, never
        written to GitHub), and `settled` says whether a gone breach's
        subject is settled for good."""
        self.gh = gh
        self.repo = repo
        self.goal = goal
        self.state = state
        self.now = now
        self.say = say
        self.write = write
        self.authors = list(brain.trusted_authors() if authors is None else authors)
        self.subjects = subjects if subjects is not None else {}
        self.settled = settled
        self.writes = 0
        self._listed: Optional[Dict[str, int]] = None
        # Issue numbers the listing found closed: clearing skips them unread.
        self._listed_closed: set = set()
        # Issue numbers the listing found carrying loom:ticket: a ticket is
        # the work, no longer the breach's own issue.
        self._ticketed: set = set()
        # Rolling breach key -> its issue, once this pass has it open.
        self._rolling_open: Dict[str, int] = {}

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
                if not m:
                    continue
                number = int(issue["number"])
                ticket = self._note_listed(issue, number)
                held = self._listed.get(m.group(1))
                # The first issue of a key wins, but a plain invariant issue
                # replaces a ticket: the ticket is not where a breach lives.
                if held is None or (held in self._ticketed and not ticket):
                    self._listed[m.group(1)] = number
        return self._listed

    def _note_listed(self, issue: Dict[str, Any], number: int) -> bool:
        """Record what the listing shows of one issue: a ticket, a closed
        one. True when it carries loom:ticket."""
        ticket = TICKET_LABEL in self._label_names(issue)
        if ticket:
            self._ticketed.add(number)
        if issue.get("state") == "closed":
            self._listed_closed.add(number)
        return ticket

    @staticmethod
    def _label_names(issue: Dict[str, Any]) -> List[str]:
        return [str(x.get("name", "")) if isinstance(x, dict) else str(x)
                for x in issue.get("labels") or []]

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

    def _body(self, b: Breach, log: Sequence[str] = ()) -> str:
        rolling = (["", "One rolling issue for this cause: the station rewrites this list "
                        "in place while it changes.", LAST_SEEN % utc(b.at)]
                   if b.rolling else [])
        return "\n".join(
            [MARKER % b.key,
             "The station found one of the factory's own invariants broken: %s." % b.title,
             "", "Evidence:"] + self._evidence(b) + rolling + self._log_block(log) +
            ["", "Invariant: `%s` (bus/invariants.py), checked every station pass. Filed "
                 "without a model: this says what broke, not why. Rewrite it as a ticket "
                 "that fixes the cause." % b.invariant,
             "", "Part of #%d" % self._home()])

    @staticmethod
    def _log_block(log: Sequence[str]) -> List[str]:
        return ["", HOLD_LOG % HOLD_LOG_MAX] + list(log)[:HOLD_LOG_MAX] if log else []

    @staticmethod
    def _old_log(body: Any) -> List[str]:
        """The hold log lines an issue body already carries."""
        return [line.strip() for line in str(body or "").replace("\r\n", "\n").split("\n")
                if LOG_LINE_RE.match(line.strip())]

    def _log_for(self, issue: Dict[str, Any], still_held: Sequence[str], at: float
                 ) -> List[str]:
        """The log of the issue's body plus a line for each lane its list showed
        held that is no longer held (still_held names the lanes that are)."""
        body = str(issue.get("body") or "").replace("\r\n", "\n")
        ended = ["- %s: held since %s, cleared %s" % (m.group(1), m.group(2), utc(at))
                 for m in HELD_LINE_RE.finditer(body) if m.group(1) not in still_held]
        return (list(reversed(ended)) + self._old_log(body))[:HOLD_LOG_MAX]

    @staticmethod
    def _held_lanes(b: Breach) -> List[str]:
        return [m.group(1) for line in b.evidence for m in [HELD_LINE_RE.match("- " + line)]
                if m]

    def _refresh(self, b: Breach, number: int, issue: Dict[str, Any]) -> None:
        """Rewrite a rolling breach's open issue when its title or list
        changed; the last-seen line alone is no reason to write."""
        title = naming.clip(b.title)
        log: Sequence[str] = ()
        if b.invariant in STANDING:
            log = self._log_for(issue, self._held_lanes(b), b.at)
        body = self._body(b, log)

        def steady(text: Any) -> str:
            return "\n".join(line for line in str(text or "").replace("\r\n", "\n").split("\n")
                             if not line.startswith(LAST_SEEN.split("%")[0]))
        if issue.get("title") == title and steady(issue.get("body")) == steady(body):
            return
        _text.check(title + "\n" + body)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                     {"title": title, "body": body})
        self.writes += 1
        self.say("invariants: updated %s#%d: %s" % (self.repo, number, title))

    def _file(self, b: Breach, ticket: Optional[int] = None) -> None:
        # The body quotes the whole breach; the title is clipped to the
        # naming standard's TITLE_MAX. ticket names the closed ticket this
        # breach outlived: it is left alone and referenced.
        title = naming.clip(b.title)
        body = self._body(b)
        if ticket is not None:
            body = body.replace("\n\nPart of #", "\n\nThe breach outlived #%d, a ticket for "
                                "it that is closed; Refs #%d.\n\nPart of #" % (ticket, ticket))
        _text.check(title + "\n" + body)
        made = self.gh.rest("POST", "/repos/%s/issues" % self.repo,
                            {"title": title, "body": body, "labels": LABELS}).data
        self.writes += 1
        number = int(made["number"])
        self.state[b.key] = number
        try:
            self.gh.rest("POST", "/repos/%s/issues/%d/sub_issues" % (self.repo, self._home()),
                         {"sub_issue_id": made["id"]})
        except GitHubError as err:
            if err.status == 422:
                self._roll_over(number, made, body, err)
            else:
                self.say("invariants: %s#%d is linked to #%d by its Part of line only: %s"
                         % (self.repo, number, self._home(), err))
        except KeyError as err:
            self.say("invariants: %s#%d is linked to #%d by its Part of line only: %s"
                     % (self.repo, number, self._home(), err))
        self.say("invariants: filed %s#%d: %s" % (self.repo, number, title))

    def _roll_over(self, number: int, made: Dict[str, Any], body: str, err: GitHubError) -> None:
        """GitHub refused the issue as a sub-issue of the repairs epic (HTTP
        422: the epic holds GitHub's limit of sub-issues). The issue moves to
        the next factory repairs batch epic with room, its Part of line names
        that epic, and later filings this pass go there too (loom#2023)."""
        home = self._home()
        self.say("invariants: #%d refused %s#%d as a sub-issue (HTTP 422: %s); rolling over "
                 "to a factory repairs batch epic" % (home, self.repo, number, err))
        try:
            batch = brain.repairs_epic(self.gh, self.repo, self.goal, full=(home,))
        except (GitHubError, ValueError, KeyError) as exc:
            self.say("invariants: %s#%d is linked to #%d by its Part of line only, no batch "
                     "epic to roll over to: %s" % (self.repo, number, home, exc))
            return
        if batch == home:
            return
        self._repairs = batch
        moved = re.sub(r"^Part of #%d[ \t]*$" % home, "Part of #%d" % batch, body, flags=re.M)
        if moved != body:
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number), {"body": moved})
            self.writes += 1
        try:
            self.gh.rest("POST", "/repos/%s/issues/%d/sub_issues" % (self.repo, batch),
                         {"sub_issue_id": made["id"]})
        except GitHubError as exc:
            self.say("invariants: %s#%d is linked to batch epic #%d by its Part of line only: %s"
                     % (self.repo, number, batch, exc))
            return
        self.say("invariants: %s#%d filed under factory repairs batch epic #%d"
                 % (self.repo, number, batch))

    @staticmethod
    def _broken_note(comment: Dict[str, Any]) -> Optional[float]:
        """When a broken-again note was last written: its "last at" time, else
        its creation (a note from before there was one note per issue)."""
        body = str(comment.get("body", ""))
        if BROKEN_MARKER not in body and BROKEN_TEXT not in body:
            return None
        m = BROKEN_LAST_RE.search(body)
        return (_epoch(m.group(1)) if m else None) or _epoch(comment.get("created_at"))

    def _broken_times(self, comments: List[Dict[str, Any]]) -> List[float]:
        """Every time an issue's breach came back, from its one note's list
        and from any older note of its own."""
        times: List[float] = []
        for c in comments:
            body = str(c.get("body", ""))
            m = BROKEN_TIMES_RE.search(body)
            if m:
                times += [float(t) for t in m.group(1).split(",") if t]
            elif BROKEN_TEXT in body:
                at = _epoch(c.get("created_at"))
                if at is not None:
                    times.append(at)
        return sorted(set(times))

    def _already_reopened(self, comments: List[Dict[str, Any]]) -> bool:
        """True when the breach has already reopened this issue and not
        cleared since: a breach that stands reopens its issue once."""
        noted = [t for t in (self._broken_note(c) for c in comments) if t is not None]
        if not noted:
            return False
        cleared = [t for t in (_epoch(c.get("created_at")) for c in comments
                               if CLEARED_TEXT in str(c.get("body", ""))) if t is not None]
        return not cleared or max(noted) >= max(cleared)

    def _held(self, number: int, body: Dict[str, Any], issue: Any, why: str) -> bool:
        """True, after recording it, when shadow mode holds this close or
        reopen (bus/shadow.py guard, loom#1551): the caller then posts no note
        and sends no PATCH. Asked before the note, so a held close never says
        it closed. The ledger keeps one entry, so the log says it once."""
        entry = shadow.guard("PATCH", "/repos/%s/issues/%d" % (self.repo, number), body, issue,
                             source="station invariants", reason=why)
        if entry is None:
            return False
        if entry.get("new", True):
            self.say("invariants: shadow mode: would %s %s#%d (%s); recorded in the shadow "
                     "ledger, left as it is" % (entry.get("action") or "close", self.repo,
                                                number, why))
        return True

    def _reopen(self, b: Breach, number: int, comments: List[Dict[str, Any]],
                issue: Any = None) -> bool:
        """Reopen the issue and say so in its one broken-again note: a count
        and the last time, edited in place (a new comment each time mailed
        everyone watching, loom#1223). False when shadow mode holds it."""
        if self._held(number, {"state": "open"}, issue, "breach again: " + b.title):
            return False
        mine = [c for c in comments if BROKEN_MARKER in str(c.get("body", ""))]
        note = mine[-1] if mine else None
        times = (self._broken_times(comments) + [self.now])[-BROKEN_TIMES_MAX:]
        count = 1
        if note is not None:
            m = BROKEN_COUNT_RE.search(str(note.get("body", "")))
            count = (int(m.group(1)) if m else 1) + 1
        body = "\n".join([BROKEN_MARKER,
                          "<!-- loom:broken-again times=%s -->" % ",".join(str(int(t)) for t in times),
                          "%s %s." % (BROKEN_TEXT, b.title),
                          "Reopened %d time%s; last at %s." % (count, "" if count == 1 else "s",
                                                                utc(self.now)),
                          "", "Evidence:"] + self._evidence(b))
        _text.check(body)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number), {"state": "open"})
        if note is not None and note.get("id") is not None:
            self.gh.rest("PATCH", "/repos/%s/issues/comments/%s" % (self.repo, note["id"]),
                         {"body": body})
        else:
            notify.post(self.gh, self.repo, number, body, notify.REOPENED)
        self.writes += 1
        self.say("invariants: reopened %s#%d: %s" % (self.repo, number, b.title))
        return True

    def _comments(self, number: int) -> List[Dict[str, Any]]:
        return brain.trusted_comments(self.gh.paginate(
            "/repos/%s/issues/%d/comments?per_page=100" % (self.repo, number)), self.authors)

    def _rolled_into(self, key: str, current: Dict[str, Breach]) -> Optional[int]:
        """The open rolling issue an older issue of the same invariant now
        lives in (the per-goal ticket-outside-epic issues, the per-lane
        lane-held, lane-failing-streak and silent-lane issues), or None."""
        invariant = key.split("/", 1)[0]
        for b in current.values():
            number = self._rolling_open.get(b.key)
            if b.rolling and b.invariant == invariant and b.key != key and number is not None:
                return number
        return None

    def _fold(self, number: int, into: int, comments: List[Dict[str, Any]],
              issue: Any = None) -> None:
        """Close an issue whose breach is now one line of rolling issue
        #into: not cleared, only listed there."""
        if self._held(number, {"state": "closed", "state_reason": "not_planned"}, issue,
                      "folded into #%d" % into):
            return
        body = ("Folded into #%d, the one rolling issue for this invariant: it lists "
                "this subject with the others and is updated in place." % into)
        if not any(("Folded into #%d" % into) in str(c.get("body", "")) for c in comments):
            _text.check(body)
            notify.post(self.gh, self.repo, number, body, notify.CLOSED)
            self.writes += 1
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                     {"state": "closed", "state_reason": "not_planned"})
        self.writes += 1
        self.say("invariants: folded %s#%d into #%d" % (self.repo, number, into))

    def _settle(self, current: Dict[str, Breach]) -> None:
        """Close the open issue of each gone SETTLES breach whose subject
        settled for good, with one note naming how. Runs before the other
        clears, so a pass short of writes still spends them here first, and
        reads no comments (a deferred comment read stopped the whole clear)."""
        listing = self._listing()
        for key, subject in list(self.subjects.items()):
            if self.writes + 2 > WRITES_MAX:
                return
            invariant = key.split("/", 1)[0]
            if key in current or invariant not in SETTLES or not isinstance(subject, str):
                continue
            number = listing.get(key)
            if number is None or number in self._listed_closed:
                self.subjects.pop(key, None)
                continue
            issue = self._issue(number)
            if issue is None or issue.get("state") != "open":
                self.subjects.pop(key, None)
                continue
            if TICKET_LABEL in self._label_names(issue):
                # Rewritten into work: the ticket path closes it.
                self.subjects.pop(key, None)
                continue
            why = self._settled_why(invariant, subject, number)
            if why and self._close_settled(number, issue, why):
                self.subjects.pop(key, None)

    def _settled_why(self, invariant: str, subject: str, number: int) -> Optional[str]:
        """The caller's word on whether the subject settled; None when it has
        none or cannot read it this pass (a rate limit still stops the pass)."""
        if self.settled is None:
            return None
        try:
            return self.settled(invariant, subject)
        except RateLimited:
            raise
        except GitHubError as err:
            self.say("invariants: could not tell whether %s#%d settled: %s"
                     % (self.repo, number, err))
            return None

    def _close_settled(self, number: int, issue: Dict[str, Any], why: str) -> bool:
        """Close an issue whose subject settled, with one note saying how.
        False when shadow mode holds it (recorded in the ledger)."""
        if self._held(number, {"state": "closed", "state_reason": "not_planned"}, issue,
                      "settled: " + why):
            return False
        body = "%s%s: %s." % (CLEARED_TEXT, utc(self.now), why.rstrip("."))
        _text.check(body)
        notify.post(self.gh, self.repo, number, body, notify.CLOSED)
        self.writes += 1
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                     {"state": "closed", "state_reason": "not_planned"})
        self.writes += 1
        self._listed_closed.add(number)
        self.say("invariants: closed settled %s#%d: %s" % (self.repo, number, why))
        return True

    def _clear_stale(self, current: Dict[str, Breach]) -> None:
        self._settle(current)
        for key, number in self._listing().items():
            if self.writes >= WRITES_MAX:
                return
            if key in current or number in self._listed_closed:
                continue
            if not self._clear_one(key, number, current):
                return

    def _clear_one(self, key: str, number: int, current: Dict[str, Breach]) -> bool:
        """Settle one open issue whose breach is gone this pass: an older
        issue folds into its rolling one, a standing one says clear, any other
        closes once quiet. False when the pass has no writes left."""
        issue = self._issue(number)
        if issue is None or issue.get("state") != "open":
            return True
        labels = [str(x.get("name", "")) for x in issue.get("labels", [])
                  if isinstance(x, dict)]
        if "loom:triage" not in labels and "loom:rewritten" not in labels:
            return True
        comments = self._comments(number)
        into = self._rolled_into(key, current)
        if into is not None and into != number:
            return self._fold_old(number, into, issue, labels, comments)
        if key in _standing_keys():
            if self.writes >= WRITES_MAX:
                return False
            self._mark_clear(key, number, issue)
            return True
        return self._close_quiet(number, issue, labels, comments)

    def _fold_old(self, number: int, into: int, issue: Dict[str, Any], labels: List[str],
                  comments: List[Dict[str, Any]]) -> bool:
        """Fold an older issue into rolling issue #into. False when the pass
        has no writes left."""
        if any(n.startswith("needs:") for n in labels):
            # An ask for an operator (needs:<login>) stays open on its own
            # issue: folding would close it unanswered.
            return True
        # Still broken, so no clear wait: its subject is a line of #into.
        if self.writes + 2 > WRITES_MAX:
            return False
        self._fold(number, into, comments, issue)
        return True

    def _mark_clear(self, key: str, number: int, issue: Dict[str, Any]) -> None:
        """A standing issue whose breach is gone says so in place and stays
        open; what it listed moves into its hold log. Already clear: no write."""
        title = naming.clip(STANDING_CLEAR_TITLE)
        if issue.get("title") == title:
            return
        invariant = key.split("/", 1)[0]
        body = "\n".join(
            [MARKER % key,
             "The station found no lane held by a limit for over %s while tickets of its "
             "tier are queued." % _hours(HELD_MAX),
             CLEAR_SINCE % utc(self.now), "",
             "This is the one standing issue for the invariant: the station edits it in place "
             "when a hold starts, changes or clears, and does not close it."] +
            self._log_block(self._log_for(issue, (), self.now)) +
            ["", "Invariant: `%s` (bus/invariants.py), checked every station pass." % invariant,
             "", "Part of #%d" % self._home()])
        _text.check(title + "\n" + body)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                     {"title": title, "body": body})
        self.writes += 1
        self.say("invariants: %s#%d is clear, left open: %s" % (self.repo, number, title))

    def _close_quiet(self, number: int, issue: Dict[str, Any], labels: List[str],
                     comments: List[Dict[str, Any]]) -> bool:
        """Close an open triage or rewritten issue whose breach has been gone CLEAR_AFTER,
        or mark it loom:recurring when it broke 3 times in RECURRING_AFTER.
        False when the pass has no writes left for it."""
        breaches = [_epoch(issue.get("created_at"))] + self._broken_times(comments)
        times = [at for at in breaches if at is not None]
        if not times or self.now - max(times) < CLEAR_AFTER:
            return True
        recent = [at for at in times if 0 <= self.now - at <= RECURRING_AFTER]
        if len(recent) >= 3:
            if "loom:recurring" not in labels:
                self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                             {"labels": labels + ["loom:recurring"]})
                self.writes += 1
            return True
        if self.writes + 2 > WRITES_MAX:
            return False
        if self._held(number, {"state": "closed", "state_reason": "not_planned"}, issue,
                      "cleared: last breach %s" % utc(max(times))):
            return True
        body = ("The invariant cleared by %s; its last breach was observed at %s, "
                "more than 6 hours earlier." % (utc(self.now), utc(max(times))))
        # A note from an earlier close does not explain this one (loom#651).
        already_explained = any(CLEARED_TEXT in str(c.get("body", ""))
                                and (_epoch(c.get("created_at")) or 0) >= max(times)
                                for c in comments)
        if not already_explained:
            _text.check(body)
            notify.post(self.gh, self.repo, number, body, notify.CLOSED)
            self.writes += 1
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                     {"state": "closed", "state_reason": "not_planned"})
        self.writes += 1
        self.say("invariants: closed cleared %s#%d" % (self.repo, number))
        return True

    def _one(self, b: Breach) -> None:
        known = self.state.get(b.key)
        number = (known if isinstance(known, int) and not isinstance(known, bool)
                  else self._listing().get(b.key))
        issue = self._issue(number) if number is not None else None
        if number is None or issue is None:
            self.state.pop(b.key, None)
            self._file(b)
            if b.rolling:
                self._rolling_open[b.key] = self.state[b.key]
            return
        self.state[b.key] = number
        if TICKET_LABEL in self._label_names(issue):
            self._one_ticketed(b, number, issue)
            return
        if issue.get("state") == "open":
            if b.rolling:
                self._rolling_open[b.key] = number
                self._refresh(b, number, issue)
            return
        self._one_closed(b, number, issue)

    def _one_ticketed(self, b: Breach, number: int, issue: Dict[str, Any]) -> None:
        """A ticket (an invariant issue rewritten into work) is no longer the
        breach's own issue: it is never reopened or rewritten back to the
        invariant text (loom#1223 flipped 18 times)."""
        if issue.get("state") == "open":
            if b.rolling:
                self._rolling_open[b.key] = number
            return
        closed = _epoch(issue.get("closed_at"))
        if closed is not None and b.at <= closed:
            return
        plain = self._listing().get(b.key)
        if plain is not None and plain != number and plain not in self._ticketed:
            # The breach already has its own invariant issue beside the ticket.
            self.state[b.key] = plain
            self._one(b)
            return
        self.state.pop(b.key, None)
        self._file(b, ticket=number)
        if b.rolling:
            self._rolling_open[b.key] = self.state[b.key]

    def _one_closed(self, b: Breach, number: int, issue: Dict[str, Any]) -> None:
        """The breach's own issue is closed: reopen it for evidence newer than
        the close, once while the breach stands."""
        closed = _epoch(issue.get("closed_at"))
        if closed is None or b.at > closed:
            comments = self._comments(number)
            if self._already_reopened(comments):
                # Reopened once for this breach and closed again while it still
                # stands: say so in the log, do not reopen it again.
                self.say("invariants: %s#%d was closed again while its breach stands; not "
                         "reopened: %s" % (self.repo, number, b.title))
                return
            if not self._reopen(b, number, comments, issue):
                return
            if b.rolling:
                self._rolling_open[b.key] = number
                if self.writes < WRITES_MAX:
                    self._refresh(b, number, issue)

    def run(self, breaches: Iterable[Breach]) -> None:
        current = {b.key: b for b in breaches}
        for b in current.values():
            if b.invariant in SETTLES:
                self.subjects[b.key] = b.subject
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
