"""The factory's own invariants, checked by the station every pass.

Each invariant is one small function from Facts (what the station already
reads: brain journals and the marker comments on goal homes, lane run
outcomes, lane limits, queued tickets, sub-issue counts, the review bot's plan
check on lane pull requests, remote heartbeats) to a list of Breach. A Facts
field the station could not read this pass is None, and the invariants that
need it find nothing.

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

from . import text as _text
from . import watch
from .github import GitHubError
from .labels import REMOTE_LANES

DAY = 24 * 3600
# No journal from any brain for this long is a brain that stopped ticking.
BRAIN_SILENT = 90 * 60
# Two loom:report comments on one goal closer than this are one too many.
REPORT_GAP = 90 * 60
RUNS_MIN = 5
HELD_MAX = 2 * 3600
# GitHub refuses a goal's 101st sub-issue; 90 leaves time to act.
SUB_ISSUES_MAX = 90
PLAN_FAILED_AGE = 30 * 60
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
    # (goal home, its sub-issue count).
    goals: Optional[List[Tuple[Ref, int]]] = None
    checks: Optional[List[Check]] = None
    # remote lane -> its newest heartbeat, None when it never posted one.
    beats: Optional[Dict[str, Optional[float]]] = None


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


def goal_full(f: Facts) -> List[Breach]:
    """A goal home at SUB_ISSUES_MAX or more sub-issues."""
    if f.goals is None:
        return []
    return [Breach("goal-full", goal.key,
                   "Goal home %s has %d sub-issues" % (goal.label, count),
                   ("%d sub-issues; GitHub refuses the 101st, and tickets past it are linked "
                    "only by a Part of line" % count,
                    "Limit: %d" % SUB_ISSUES_MAX),
                   f.now)
            for goal, count in f.goals if count >= SUB_ISSUES_MAX]


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


INVARIANTS: Tuple[Callable[[Facts], List[Breach]], ...] = (
    brain_failing, brain_silent, marker_cadence, lane_no_success, lane_held,
    goal_full, plan_check_failed, remote_silent)


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

    def _body(self, b: Breach) -> str:
        return "\n".join(
            [MARKER % b.key,
             "The station found one of the factory's own invariants broken: %s." % b.title,
             "", "Evidence:"] + self._evidence(b) +
            ["", "Invariant: `%s` (bus/invariants.py), checked every station pass. Filed "
                 "without a model: this says what broke, not why. Rewrite it as a ticket "
                 "that fixes the cause." % b.invariant,
             "", "Part of #%d" % self.goal])

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
            self.gh.rest("POST", "/repos/%s/issues/%d/sub_issues" % (self.repo, self.goal),
                         {"sub_issue_id": made["id"]})
        except (GitHubError, KeyError) as err:
            # A goal at GitHub's sub-issue limit refuses it; the Part of line
            # still names the goal.
            self.say("invariants: %s#%d is linked to #%d by its Part of line only: %s"
                     % (self.repo, number, self.goal, err))
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
        for b in breaches:
            if not self.write or self.writes >= WRITES_MAX:
                return
            try:
                self._one(b)
            except (GitHubError, OSError, ValueError, KeyError, TypeError) as err:
                self.say("invariants: could not file %s (%s): %s" % (b.invariant, b.title, err))


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
