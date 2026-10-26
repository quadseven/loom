"""The factory pass: lane pull requests and stuck tickets move on their own.

The brain reads diffs, plans, briefs and verifies. It must not be the only
thing that moves finished work: on 2026-10-13 it left 29 lane pull requests
unmerged for 4 to 12 hours, because its tick budget ran out before it reached
their review threads, and their tickets filled every goal's cap. The factory
runs from the station at most every FACTORY_CHECK seconds for pull requests
and stuck tickets. Queue assignment runs on every station pass after claims.
Neither needs a model:

- A lane pull request whose checks have all finished, whose review check
  (LOOM_REVIEW_CHECK) has run on its head commit, and that is mergeable,
  green and free of unresolved threads is merged with bus.brain.merge (all
  of that function's own checks apply), and its ticket moves to loom:merged.
  Without the review check on its head commit it is never merged: every
  change is reviewed (the operator's rule), so it waits for the brain.
- One with a failing check, an unresolved thread or a conflict goes back to
  a lane as a fix brief (bus.brain.fix). It stays on the ticket's lane while
  the rounds make progress. It climbs the model ladder (docs/MUSE.md) only
  when it is stuck: a review thread survived a fix round (the same thread, or
  the same finding posted again: bus/converge.py compares path, a nearby
  line and the rule, never ids alone), checks still fail after a round that
  was asked to fix them, or the pull request is older than STUCK_AGE. A
  review that finds new things is progress, and a conflict is mechanical
  (the fix brief's runner merges the base), so neither climbs. The station's
  hand-back (bus/station.py _post_fix_or_escalate), which usually sends a
  round before this pass sees the pull request, applies the same test.
- Landing keeps the default branch green and nothing stale. A ready pull
  request that is behind its base is brought up to date first (GitHub's
  update-branch), and lands only once its checks and review pass again on
  the new head. Each repo lands at most one pull request a pass, so the
  next one is checked against the base that includes it. Pull requests of
  loom:urgent and loom:regression tickets land first, then by priority,
  then the oldest.
- A pull request that changes a code file and no test file goes back to its
  lane asking for a test that fails without the change, instead of landing
  (north star 5: on 2026-10-13, 6 of 45 merged lane pull requests changed no
  test, and two of them shipped behavior). Doc and config changes need none;
  a repo in LOOM_NO_TEST_REPOS (one with no test harness) and a ticket whose
  body says "Tests: none needed (<reason>)" skip the check. The send-back is
  a finding round: it climbs only when the next round still adds no test.
- A pull request whose only failing check is the review check, failed on a
  note asking for the change to be split (bus/split.py), is not sent back: the
  parts are filed as tickets and the pull request is closed with a pointer
  (loom#1218). A blocking finding or another failing check goes back as usual.
- A pull request on a public repo is checked against its ticket before it
  lands (bus/scope_fit.py, LOOM_SCOPE_CHECK_URL): a cheap model is asked
  whether the changed files fit the ticket's What section and the repos and
  resources it names. A NO with a reason holds the pull request (needs-brain,
  a comment with the reason, once per head commit); a YES, no route, a limit
  or a reply it cannot read lands it as before, so a dead route never blocks
  a merge. A private repo is never asked.
- A reviewed, green pull request whose only open threads are the review
  bot's medium or low findings files them in one loom:triage follow-up
  issue, answers and resolves each thread, and lands, with no fix round
  (2026-10-14: 64% of 361 fix rounds were review threads, most of them
  medium). High and critical findings, and threads a person opened, still
  go back to the lane.
- The merge rule follows the pull request's class:<name> label
  (bus/change_class.py; docs/MUSE.md, Merge by change class). docs and tests
  defer a minor finding at once; logic is the rule above; a sensitive change
  is never merged here: it is marked needs-brain for the brain's merge, and
  its conflicts, failing checks and high findings go back to its lane.
- A pull request whose only fault is a failed Actions job gets that job run
  once more before it is sent back, since one flaky run should not cost a
  lane round.
- A pull request whose only failing checks are leak scans that failed in
  their "title and body" section, with a clean diff section, is fixed in its
  text: no lane can fix text the runner re-renders from the ticket each
  round (a ticket spent 14 rounds on one example reference on 2026-10-13).
  Each flagged body line is rewritten, a cross-repo issue reference as
  "issue N of another repo" in words, or dropped when it cannot be; editing
  the body reruns the scan. Once per head commit, with a comment on the
  ticket saying which lines changed.
- A pull request whose only failing check is the plan check, failing on its
  body (bus/hunt_plan.py), has the failing parts rebuilt from its ticket
  (bus/pr_repair.py plan_repair) instead of a lane round whose agent edits
  code (loom#1837): the body edit makes the review bot read it again. Once
  per head commit, on the ticket's status comment. A ticket that lacks the
  parts is held for the brain with one note naming them, edited in place;
  the hold lifts when the ticket gains them.
- The review bot replies "Fixed in <sha>" on a thread whose flagged code
  changed, but GitHub does not let its app resolve threads, so the thread
  stays open. Such a thread, with nobody writing after the bot's reply, is
  resolved here before the pull request is judged; otherwise it would count
  as a finding that survived the round (77 of 218 open threads on
  2026-10-13 were the bot's own "Fixed in" threads).
- A round where the lane made no change and explained why waits for the
  review it asked for, up to REVIEW_GRACE, before the round is judged.
- A pull request whose open thread carries a lane's answer to a finding, and
  whose ticket has no lane up for its next rung, does not wait with nothing
  to move it: the review bot is asked to judge the answer (`/grug improve`,
  once per head commit), so it re-reviews the head with the reply in view. It
  clears the head if the answer holds, and keeps the finding if it does not
  (2026-10-19: grug#1189 waited from 18:25Z on an answered thread nothing
  judged).
- A failed ticket climbs the ladder, but only once for a run that committed
  nothing and wrote no reason (lanes/run_lane.sh NO_REASON): at the second
  such failure it is left failed with needs-brain for the brain, since a
  stronger model made no commit either for five tickets on 2026-10-13.
  A ticket left briefed for BRIEFED_STALL
  with no lane claiming it does not: it never ran, so it moves to the
  least-loaded other lane of its tier (a ladder ticket, of its rung), writing
  no rung marker.
- Each pass gives queued tickets (FIRST labels, then priority, then the
  oldest) free lanes in their difficulty tier, leaving held tickets and
  tickets without a readable tier queued.
- A ticket briefed on a lane at a limit (its heartbeat says "limit:", as the
  spark lane's does while its box is hot) moves at once to a free lane in its
  difficulty tier (TIERS), or a ladder ticket to a free lane of its own rung.
  A limit is not a failure, so it never climbs; with no free lane the ticket
  waits where it is for its lane to come back. A lane whose heartbeat names
  the ticket's Model: among its limited_models (a model ladder lane, up for
  its other models) counts as at a limit for that ticket, and the ladder
  never climbs to a rung whose model is limited on every lane of it.
- A running ticket on a local lane with no live heartbeat for SILENT_MAX
  moves like a limit move: to a free lane of its tier (of its Model: for a
  pinned ticket), else back to queued. It is no failure and no retry.
- When no lane of a ticket's tier is up, a limit move or a queued assignment
  falls back to a free lane of the tiers FALLBACK lists (medium: hard, then
  easy), and says so in its note. The ticket's Difficulty: line is kept, so
  its next move takes its own tier again once a lane of it is up.
- Past the last rung the ticket is left failed for the brain, with a note.
- A ticket waiting with no lane that takes it (NO_LANE_RE) for
  LOOM_NO_LANE_REROUTE hours (default 6) is rerouted to the first rung the
  climb rules allow with a free lane in its tier; with none, by
  NO_LANE_HANDOFF it goes to the hand-off pool (HANDOFF_LABEL), out of its
  goal's in-flight count, with one note (loom#1831, docs/BRAIN.md).
- A revert ticket (bus/revert.py, REVERT_LABEL) lands first when its own
  pull request is green, even while the default branch is red. Its medium
  and low findings are deferred at once instead of after a fix round, and
  it needs no test of its own (the failing check it fixes is its test).
- A lane pull request whose ticket has a newer open lane pull request, or whose
  ticket is at merged, shipped or verified through a different pull request, is
  closed with a comment naming the pull request that superseded it. A pull
  request a person opened, or one not on a loom/ branch, is never closed this
  way.

Each climb is recorded on the ticket as a RUNG_MARKER comment, so the ladder
survives restarts and any brain can read where a ticket stands.

Trust: the factory acts only on tickets and pull requests opened by a
LOOM_BRIEF_AUTHORS login, and reads rung, fix and claim comments only from
those logins. On a public repo anyone can comment; a stranger's comment must
not move a ticket up or down the ladder, and a stranger's issue must not be
turned into a trusted brief by the factory editing it.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import answer, bench, brain, brief_check, change_class, converge, criteria_gate, hunt_plan, labels, merge_tier, naming, notify, operator_hold, pr_repair, scope_fit, split, stale_claims, status, tickets
from .github import GitHub, GitHubError, rest_login
from .labels import REMOTE_LANES
from .tickets import FIX_MARKER, PRIORITIES, priority_of

FACTORY_CHECK = 300
STUCK_AGE = 6 * 3600
BRIEFED_STALL = 2 * 3600
# A ticket the factory leaves waiting with no lane that takes it (NO_LANE_RE)
# this long is rerouted to another rung of the model ladder with a free lane
# in its tier; LOOM_NO_LANE_REROUTE sets the hours (no_lane_reroute). On
# 2026-10-25, 89 tickets waited so, some since 10-23, and held their goals'
# in-flight caps the whole time (loom#1831).
NO_LANE_REROUTE = 6 * 3600
# By this long with no reroute allowed, the ticket leaves the queue and its
# goal's in-flight count for the hand-off pool (HANDOFF_LABEL).
NO_LANE_HANDOFF = 24 * 3600
# The wait reasons (Factory.waits) that mean no lane takes the ticket: a
# queued ticket no free lane of its model or tier takes, a briefed one stalled
# with no other lane of its rung or tier, or one on a lane at a limit or down
# with no free lane in its rung or tier.
NO_LANE_RE = re.compile(r"(no free lane takes model |no free lane in tier "
                        r"|stalled on lane \S+: no other lane of its "
                        r"|on lane \S+, .*: no free lane in its )")
# The hand-off pool the second operator's session works (docs/BRAIN.md).
HANDOFF_LABEL = labels.HANDOFF
# The states in which a lane has taken the ticket: its no-lane wait is over.
LANED_STATES = ("running", "pr", "answered", "merged", "shipped", "verified")


def no_lane_reroute(env: Optional[Dict[str, str]] = None) -> float:
    """Seconds a ticket waits with no lane before a reroute: the hours in
    LOOM_NO_LANE_REROUTE (a positive number, at most NO_LANE_HANDOFF's),
    else NO_LANE_REROUTE."""
    raw = str((os.environ if env is None else env).get("LOOM_NO_LANE_REROUTE", "")).strip()
    try:
        hours = float(raw)
    except ValueError:
        return float(NO_LANE_REROUTE)
    if not 0 < hours < float("inf"):
        return float(NO_LANE_REROUTE)
    return min(hours * 3600, float(NO_LANE_HANDOFF))


def no_lane(reason: str) -> bool:
    """Whether a wait reason (Factory.waits) says no lane takes the ticket."""
    return bool(NO_LANE_RE.match(str(reason or "")))
# A ticket held by its After: line this long, with none of the tickets it
# waits for merged, is reported to the brain once like a stuck ticket.
HOLD_STUCK = 48 * 3600
# A head commit with no review check this long after its push is logged once
# per pass as waiting on a review that may never come; it is never merged.
REVIEW_GRACE = 1800
# At most this many merges, send-backs and climbs per pass.
MAX_ACTIONS = 12
# At most this many dashboard answers (loom:answer-pending issues) are read in
# one repo a pass; the rest wait for the next.
ANSWERS_PER_PASS = 10
DEFAULT_REVIEW_CHECK = "Grug - Elder"
# The review bot's plan check (same default as bus/invariants.py).
DEFAULT_PLAN_CHECK = "Grug - Chief"
# The review bot's login as GraphQL reports it (an app, without "[bot]").
DEFAULT_REVIEW_BOT = "grug-tribe"
BOT_FIXED_PREFIX = "Fixed in "
# Asks the review bot to review a pull request's head again; it resolves the
# threads it no longer raises. LOOM_REVIEW_AGAIN overrides it.
REVIEW_AGAIN = "/grug improve"
# The reply a lane posts on a review thread when it answered the finding
# instead of changing the code (bus/station.py writes it). The factory reads
# the same words to know a thread was answered and not settled.
ANSWER_REPLY = "answered this finding instead of changing the code:"
ANSWER_RE = re.compile(r"^Lane [A-Za-z0-9_.-]+ %s" % re.escape(ANSWER_REPLY))
# The marker on the comment that asks the review bot to judge an answer: it
# carries the head commit, so the ask happens once per head.
IMPROVE_MARKER = "<!-- loom:improve-head %s -->"
# The review bot's check output title that means it did not finish every part
# of the diff, or did not trust its own result ("WARN Elder review coverage
# partial", "WARN Grug eyes clouded", "WARN Elder review low confidence").
# A lane cannot finish someone else's review by editing code, so the factory
# asks the bot to look at the head again instead of sending the pull request
# back for a fix round it cannot close.
PARTIAL_REVIEW_TITLES = ("WARN Elder review coverage partial",
                         "WARN Grug eyes clouded",
                         "WARN Elder review low confidence")
# The review bot's own words for a result that is not a verdict ("Degraded
# review (low_confidence_review): not a verdict. Comment /grug improve to
# re-run once the backend recovers.").
DEGRADED_REVIEW_MARK = "Degraded review ("


def degraded_review(title: str, summary: str) -> bool:
    """True when a review check's output is a partial or degraded result, not
    a verdict: the summary carries the review bot's degraded marker, or the
    title names a partial-review form anywhere (a hunt title puts a commit
    range before it, "Living Hunt a..b - WARN ...")."""
    return (DEGRADED_REVIEW_MARK in str(summary or "")
            or any(mark in str(title or "") for mark in PARTIAL_REVIEW_TITLES))


# Posted once per head commit on a pull request whose only failing check is a
# partial review, so a second partial result on the same head goes back.
REVIEW_AGAIN_MARKER = "<!-- loom:review-again v1 sha=%s -->"

ANTHROPIC = ("anthropic",) + tuple("anthropic-%d" % n for n in range(2, 9))
# The slots that draw on one API credit: an empty credit holds all of them.
# run_lane.sh reads this set (`python3 -m bus.factory credit-slots`).
SHARED_CREDIT = ANTHROPIC
# The operator's ladder, 2026-10-12: "sonnet 5.5, or codex sol or opus 5.5
# worst case"; since 2026-10-21 codex runs gpt-6-luna only (gpt-6-sol pins
# spent the weekly codex limit in a day), so the codex rung is luna. Each
# rung lists the lanes that serve it, best first. No rung, hard-tier pin or
# equivalent may name a tickets.FORBIDDEN_MODELS model.
LADDER: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("claude",), "claude-sonnet-5-5"),
    (("codex", "codex-2"), "gpt-6-luna"),
    (("claude",) + ANTHROPIC, "claude-opus-5-5"),
)
# The lanes a ticket may move to when its lane is at a limit, by its
# Difficulty: line, best first (docs/MUSE.md, Lane choice; the doc test reads
# this table). A limit is not a failure, so the move stays in the tier and
# never climbs the ladder: on 2026-10-13 a private repo's easy ticket on a
# hot spark lane went up the ladder to claude, the operator's subscription.
SPARKS = ("spark", "spark-2", "spark-3", "spark-4")
# One slot per free provider in the spark-gateway's pool (infra#5617): spark-
# type slots whose endpoint is the gateway's route pinned to that provider,
# never a Spark. Operator 2026-10-19: make every free provider a lane, spend
# its allowance and wait out each reset. free-ollama Ollama Cloud and free-gemma
# Gemma may take private repos; free-nvidia NVIDIA NIM, free-kilo Kilo and free-groq
# Groq may train on prompts, so they are public only (PUBLIC_ONLY).
FREE_POOL = ("free-ollama", "free-gemma", "free-nvidia", "free-kilo", "free-groq")
TIERS: Dict[str, Tuple[str, ...]] = {
    # The operator's priorities: Muse and agy first on public repos, so their
    # allowances are spent before the rest (2026-10-14); then the Sparks,
    # much more (2026-10-16): four local slots on the operator's own
    # hardware, free, and open to private repos. Tiers follow the Artificial
    # Analysis Intelligence Index (the operator's yardstick).
    # The Sparks come FIRST in every tier since 2026-10-17 (operator: "I wanna
    # use my sparks"): both boxes run NVIDIA Nemotron 3.5 Super VL on vLLM
    # (120B / 12B active; SWE-bench Verified 71.1 and Terminal-Bench 2.1 about
    # 61 on its model card; 6/6 on assay's agentic_coding). Artificial
    # Analysis has no listing for it yet. A ticket a Spark fails still climbs
    # the model ladder, so nothing waits on them.
    "easy": SPARKS + ("muse", "agy") + FREE_POOL + ("opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4",
                      "openrouter", "workersai", "zen", "zai", "copilot"),
    # The free lanes take medium work too, before the paid ones (operator
    # 2026-10-19: "I always want my free tier used up"). 84 medium tickets
    # waited a median 18.5 hours for codex while these sat idle; a ticket a
    # free model fails still climbs the model ladder.
    "medium": SPARKS + ("muse", "agy") + FREE_POOL + ("openrouter", "workersai", "zen", "zai")
              + ("codex", "codex-2", "opencode-go", "opencode-go-2",
                 "opencode-go-3", "opencode-go-4") + ANTHROPIC,
    # agy takes hard work on public repos (agy stays public only); since
    # 2026-10-21 it runs its gemini-3.8-flash default there, as codex runs
    # gpt-6-luna (tickets.FORBIDDEN_MODELS).
    "hard": SPARKS + ANTHROPIC + ("claude", "agy", "codex", "codex-2"),
}
# What a lane costs, for the dashboard's Tiers and allowances page (loom#968):
# own-hardware (the Sparks), free (a free provider's allowance), subscription
# (a plan with a usage window: claude, codex, opencode-go, agy on the
# operator's Google plan, muse) or never-paid (a prepaid API credit the
# operator does not top up, LOOM_NO_TOPUP: the anthropic slots). Every lane
# TIERS and LADDER name has one (tests/test_bus_factory.py LaneKinds). Base
# lanes only: a numbered slot takes its lane's kind (lane_kind).
KINDS = ("own-hardware", "free", "subscription", "never-paid")
LANE_KINDS: Dict[str, str] = {
    "spark": "own-hardware",
    **{lane: "free" for lane in FREE_POOL + ("openrouter", "workersai", "zen", "zai", "copilot")},
    **{lane: "subscription" for lane in ("claude", "codex", "opencode-go", "agy", "muse")},
    "anthropic": "never-paid",
}


def lane_kind(lane: str) -> Optional[str]:
    """LANE_KINDS for a lane or a numbered slot of one (anthropic-9 is an
    anthropic slot, labels.base_lane); None for a lane it does not know."""
    return LANE_KINDS.get(labels.base_lane(lane))
# The strongest model each lane has, for a hard ticket with no Model: line
# (bus/tickets.py render_brief writes it into the brief, so run_lane.sh runs
# it instead of the lane default). Empty since 2026-10-21: the codex and agy
# pins were gpt-6-sol and gemini-4-argon-high, both tickets.FORBIDDEN_MODELS, so a
# hard ticket runs each lane's default model.
HARD_MODELS: Dict[str, str] = {}
# A model pin another lane may serve with an equivalent model while every
# lane of the pin is at a limit or down (not merely busy): the lanes, the
# model they run, and why it is equivalent. Empty since 2026-10-21: the one
# entry moved claude-opus-5-5 work to agy on gemini-4-argon-high, a
# tickets.FORBIDDEN_MODELS model, and gemini-3.8-flash is no Opus equivalent.
EQUIVALENTS: Dict[str, Tuple[Tuple[str, ...], str, str]] = {}


def _climb_rungs() -> Tuple[Tuple[Tuple[str, ...], str], ...]:
    """LADDER, then one rung for each EQUIVALENTS or HARD_MODELS model the
    ladder lacks, with the lanes that run it. The rungs a stuck ticket climbs
    (Factory._climb, bus/brain.py escalate)."""
    have = {model for _, model in LADDER}
    extra: Dict[str, List[str]] = {}
    for lanes, model, _ in EQUIVALENTS.values():
        if model not in have:
            extra.setdefault(model, []).extend(lanes)
    for lane, model in HARD_MODELS.items():
        if model not in have:
            extra.setdefault(model, []).append(lane)
    return LADDER + tuple((tuple(dict.fromkeys(lanes)), model) for model, lanes in extra.items())


# The ladder's rungs, then a rung for each EQUIVALENTS or HARD_MODELS model
# the ladder lacks (none since 2026-10-21).
CLIMB = _climb_rungs()
# The strong models: the ladder's last rung, each lane's hard-tier model and
# each equivalent. A ticket that ran on the last rung or past it climbs only
# to another of these, never back down to a weaker model (climb_rungs).
STRONG = frozenset({LADDER[-1][1]} | set(HARD_MODELS.values())
                   | {model for _, model, _ in EQUIVALENTS.values()})
EQUIVALENT_MARKER = "<!-- loom:equivalent %s -->"
EQUIVALENT_RE = re.compile(r"<!-- loom:equivalent (\S+) -->")
# When no lane of a ticket's tier is up (each at a limit or down), a tier move
# takes a free lane of these tiers instead, in order: the next harder tier,
# then the easier one (docs/MUSE.md, Lane choice). On 2026-10-14 every medium
# lane was at a limit for hours while claude and the free lanes sat idle.
# Easy has none (never anthropic or claude) and hard has none (never a free
# lane or spark).
FALLBACK: Dict[str, Tuple[str, ...]] = {"medium": ("hard", "easy")}
# Lanes whose provider may train on prompts: public repos only.
# opencode-go-4 runs Muse Spark Contributor, which trains on prompts.
# free-nvidia, free-kilo and free-groq are free providers that may train on prompts.
PUBLIC_ONLY = ("zen", "zai", "agy", "copilot", "opencode-go-4", "muse", "free-nvidia", "free-kilo", "free-groq")
# A rung whose least-loaded lane already holds this many briefed or running
# tickets is passed over for the next one (docs/MUSE.md: "a higher rung rather
# than a wait behind a busy lane that is up"). Only lanes that are up and serve
# the rung's model count: an idle lane at a limit made rung 3 look free while
# its only up lane was the busy claude lane (2026-10-19). The last rung takes
# the work regardless.
RUNG_BUSY = 2
# A rung with one lane up holds at most this many waiting tickets while another
# untried rung has a shorter wait: past it, a waiting ticket climbs to an
# untried rung whose least-loaded lane holds at least three fewer, though that
# lane is no longer free (RUNG_BUSY). Left to wait for the
# one lane, 13 tickets stalled on the claude lane while the anthropic lanes
# were out of credit, two of them 10 hours (loom#1280, 2026-10-21).
ONE_LANE_MAX = 4
# A ladder ticket whose rung has no lane serving its model for longer than
# this (each lane down, or its model limited past it) climbs to the next
# untried rung instead of waiting, as no failure. An account quota held a
# rung's one lane for a day while two fix rounds and their open pull requests
# waited on it (2026-10-19).
RUNG_LIMIT_WAIT = 2 * 3600
# A tier ticket briefed to a lane that is down, or at a limit lifting further
# off than this, goes back to the queue when no lane of its tier is free.
LIMIT_REQUEUE = 2 * 3600
# A running ticket on a lane with no live heartbeat for this long moves off it.
SILENT_MAX = 30 * 60
RUNG_MARKER = "<!-- loom:rung %d -->"
RUNG_RE = re.compile(r"<!-- loom:rung (\d+) -->")
# The thread and check headings bus.brain.fix writes into a loom:fix comment.
FIX_THREAD_RE = re.compile(r"^### Thread (\S+) ", re.M)
FIX_CHECK_RE = re.compile(r"^### Check: ", re.M)
CLAIM_RE = re.compile(r"^Claimed for lane ")
# The factory's own note when it moves a stalled ticket to another lane.
STALL_MOVE_RE = re.compile(r"^Not a failure \(briefed \d+ hours on lane ")
# bus/station.py's note when a fix round ended without a commit.
NO_CHANGE_RE = re.compile(r"^Lane \S+ made no change for the fix")
# Fix rounds in a row that end with no commit on one failing check before the
# ticket goes to a person.
NO_CHANGE_LIMIT = 2
# bus/station.py's note on a failed run, and the runner's reason when a run
# committed nothing and its agent wrote no reason why (lanes/run_lane.sh).
FAILED_RE = re.compile(r"^Lane \S+ failed: ")
# Other endings of a claim, each read as a classed reason (loom#572): since
# status events (loom#866) these lines do not start "Lane X failed:". An
# automatic retry carries the failed lane's classed reason after its move; a
# factory-incident hold is a [factory] ending; and bus/station.py's limit,
# auth and branch-moved paths record "Lane X stopped: [class] ...", worded so
# that no reader of FAILED_RE counts it as a failure.
RETRY_ENDED_RE = re.compile(r"^Automatic retry [0-9]+ of [0-9]+ \([a-z]+\): moved from lane "
                            r"\S+ to lane \S+\. Lane \S+ failed: ")
HELD_RE = re.compile(r"^Lane \S+ held: ")
STOPPED_RE = re.compile(r"^Lane \S+ stopped: (?=\[(?:limit|auth|transient)\] )")
CLAIMED_RE = re.compile(r"Claimed for lane ([a-z0-9-]+) \(body sha256 [0-9a-f]{12}\)")
# The runner's footer on a lane pull request (lanes/run_lane.sh pr_body,
# rewritten by bus/pr_repair.py on each fix round): the lane and the model the
# newest run used.
RUN_FOOTER_RE = re.compile(r"^Loom ticket \S+, lane ([A-Za-z0-9_.-]+), model (\S+?)\.(?:\s|$)", re.M)
# The comments that assign the ticket to a lane: a brief. Claims after the
# newest of these ran on the lane the ticket was last briefed to (loom#565).
BRIEF_RES = (
    re.compile(r"^The station gave this queued ticket lane ([a-z0-9-]+) \(tier [a-z]+\)\.$"),
    re.compile(r"the factory moved this ticket to lane ([a-z0-9-]+),"),
    re.compile(r"moved from lane [a-z0-9-]+ to lane ([a-z0-9-]+)\."),
    re.compile(r"the factory moved this ticket up the model ladder to rung \d+, \S+ on lane ([a-z0-9-]+)\."),
)
RUNAWAY = "loom:runaway"
NO_REASON = "agent made no commit and wrote no reason"
THREADS_QUERY = ('query($owner:String!,$name:String!,$number:Int!) { repository(owner:$owner,name:$name) {'
                 ' pullRequest(number:$number) { reviewThreads(first:100) { nodes { id isResolved isOutdated path line originalLine'
                 ' root: comments(first:1) { nodes { author { __typename login } body url } }'
                 ' comments(last:20) { nodes { author { __typename login } body } } } } } } }')
RESOLVE_MUTATION = ('mutation($id:ID!) { resolveReviewThread(input:{threadId:$id})'
                    ' { thread { isResolved } } }')
REPLY_MUTATION = ('mutation($id:ID!,$body:String!) { addPullRequestReviewThreadReply('
                  'input:{pullRequestReviewThreadId:$id,body:$body}) { comment { id } } }')
# A medium or low finding the review bot opened, on a pull request whose
# review check passed on its head, is deferred to a follow-up issue instead of
# holding the merge, once a fix round asked for it or the pull request has had
# DEFER_AFTER fix rounds. On 2026-10-13, 24 of 32 blocked lane pull requests
# held only such findings, most past round 8: each round's review found new
# ones, so requiring every thread resolved never converged. High and critical
# findings, and every thread a person opened, still hold the merge.
# The bot opens each finding with its severity line, "<symbol> medium | ...".
MINOR_RE = re.compile(r"^\W*(medium|low) \|")
DEFER_AFTER = 2
# The change class a lane pull request carries (bus/change_class.py): the
# merge rule follows it (docs/MUSE.md, Merge by change class). Highest wins,
# so sensitive is checked first; a pull request with no class keeps today's
# rule.
CLASSES = change_class.CLASSES
DEFER_LABEL = "loom:triage"
DEFER_TITLE = "Settle the review findings left from PR #%d"
# The title before the naming standard (docs/NAMING.md): an open follow-up
# filed under it still counts, so a pull request never gets two.
DEFER_TITLE_LEGACY = "Deferred review findings from %s#%d"
# The follow-up issue is filed only after its pull request merges. Deferring
# resolves the threads and leaves this record on the ticket; the pass that
# finds the pull request merged files the issue (settle_deferred) and leaves
# the filed record. A pull request that closes unmerged files nothing.
DEFERRED_MARKER = "<!-- loom:deferred pr=%d sha=%s rounds=%d ids=%s -->"
DEFERRED_RE = re.compile(r"<!-- loom:deferred pr=(\d+) sha=(\w*) rounds=(\d+) ids=([^ ]*) -->")
DEFERRED_FILED_MARKER = "<!-- loom:deferred-filed pr=%d -->"
DEFER_TITLE_RE = re.compile(r"^Settle the review findings left from PR #(\d+)$")
# A file a pull request changes is code by its extension, and a test by a
# test or tests folder in its path or by its name.
CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".cpp", ".h", ".hpp",
             ".go", ".rs", ".sh", ".lua", ".sql")
TEST_NAME_RE = re.compile(r"^test_|_test\.|\.test\.|\.spec\.")
TESTS_NONE_RE = re.compile(r"^Tests: none needed \(\S[^)\n]*\)", re.M)
# The heading the no-test send-back writes into its loom:fix comment, so the
# next round can tell the finding survived.
NO_TEST_HEADING = "### Test: the change has no test"
ACTIONS_RUN_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")
# The leak scan (infra-public's _reusable.leak-scan.yml) logs its diff and its
# title-and-body text as two sections; each hit is "  <file>:<line>  [<rule>] ...".
LEAK_CHECK_RE = re.compile(r"leak|private identifiers", re.I)
LEAK_DIFF_MARK = "--- PR diff ---"
LEAK_TEXT_MARK = "--- title and body ---"
LEAK_HIT_RE = re.compile(r"^\s*\S+:(\d+)\s+\[([\w-]+)\]\s")
LEAK_REF_RULE = "private-issue-ref"
CRITERION_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.+?)\s*$")
NOT_BUILT_RE = re.compile(r"\s*;\s*not built:\s*\S.+$", re.I)
# The scanner's cross-repo reference shape: [owner/]name#N.
SCAN_REF_RE = re.compile(r"(?<![\w/-])(?:([a-z0-9][a-z0-9._-]*)/)?"
                         r"([a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9])#(\d+)\b", re.I)
BODY_LEAK_MARKER = "<!-- loom:body-leak-repaired %s -->"
# On the ticket: the station put it under an epic for this head commit.
EPIC_REPAIR_MARKER = "<!-- loom:epic-repaired %s -->"
# On the ticket's status comment: the factory rebuilt the pull request body
# from the ticket for this head commit after a body-only plan check failure.
PLAN_REPAIR_MARKER = "<!-- loom:plan-repaired %s -->"
# The plan check's summary table names this row for a ticket under no epic.
EPIC_ROW = "which epic"
_ORPHAN_NUMBER_RE = re.compile(r"#(\d+)")
# On the pull request: the scope check held this head commit (bus/scope_fit.py).
SCOPE_HOLD_MARKER = "<!-- loom:scope-hold %s -->"
MODEL_LINE = re.compile(r"^Model:[^\n]*\n?", re.M)
GREEN = ("success", "neutral", "skipped")
# bus/revert.py's revert tickets, landed first and on green.
REVERT_LABEL = "loom:revert"
FIRST = tickets.FIRST_LABELS

FIX_NOTES = """The factory sent this pull request back: it has failing checks, open \
review threads or a conflict with the default branch, listed below.

- Bring the branch up to date with the default branch first if it conflicts.
- Fix each finding that is valid against the current code, with a test that \
fails without the fix where the finding is about behavior.
- Where a finding is not valid, change the code or a comment so the reason is \
plain to the next reader, and say why in the commit message.
- Keep the whole suite green, and change nothing the threads and checks do not \
ask for."""

# A ticket with either label is never sent back: a person or the brain has it.
SEND_BACK_HOLDS = frozenset(("loom:human", "needs-brain"))

# The note for a ticket whose lane run failed with its pull request open: the
# failed run is the finding, so it is not the stock FIX_NOTES that brain.fix
# refuses to send alone.
FAILED_RUN_NOTES = """The lane's last run on this ticket failed before it finished. The \
open pull request holds the work so far.

- Read the pull request's branch and finish the ticket on it.
- Keep the whole suite green, and change nothing the ticket does not ask for."""

# A green pull request that leaves ticket criteria unticked goes back with
# each one named under this heading (2026-10-20: the generic note listed no
# finding, so lanes had nothing to act on and one pull request came back six
# times). stuck() climbs a ticket whose last round left a named one unticked.
UNTICKED_HEADING = "### Acceptance criteria not ticked"


def unticked_notes(missing: Sequence[str]) -> str:
    """The fix note for criteria a lane pull request leaves unticked."""
    return "\n".join([UNTICKED_HEADING, ""] + ["- " + c for c in missing] + [
        "", "The factory sent this pull request back: its checks and review pass, "
        "but the ticket criteria above are not ticked in its body.", "",
        "- Build each criterion the change does not cover yet, with a test that fails "
        "without it.",
        "- Then tick each built criterion in the pull request body's ## Acceptance "
        "criteria, or leave it unticked and end its line with \"; not built: <reason>\".",
        "- Change nothing else."])


TICKED_HEADING = "### Live or operator criteria ticked by the lane"


def _last_ask(comments: Sequence[Dict[str, Any]]) -> str:
    """The newest loom:human ask among the comments, or ""."""
    asks = [str(c.get("body") or "") for c in comments
            if str(c.get("body") or "").startswith(answer.HUMAN_MARKER)]
    return asks[-1] if asks else ""


def ticked_notes(ticked: Sequence[str]) -> str:
    """The fix note for live or operator criteria a lane ticked in its pull
    request body (bus/criteria_gate.py): only the verifier ticks those."""
    return "\n".join([TICKED_HEADING, ""] + ["- " + c for c in ticked] + [
        "", "The factory sent this pull request back: these criteria are met only on a "
        "running system or by a person, and the body ticks them.", "",
        "- Untick each one in the pull request body's ## Acceptance criteria. The "
        "station's verifier ticks a live criterion once its own check passes, and the "
        "operator ticks an operator one.",
        "- Change nothing else."])


TEST_NOTES = NO_TEST_HEADING + """

The factory sent this pull request back: it changes code and no test file.

- Add a test that fails without the change and passes with it, next to the \
repo's other tests.
- Change nothing else.
- Where the change truly needs no test (no behavior changed, or the repo has \
no test harness), the brain adds "Tests: none needed (<reason>)" to the \
ticket body; the lane does not."""


def _say(message: str) -> None:
    print("factory: %s" % message, flush=True)


def _default_close(gh: GitHub, repo: str, number: int, comment: str) -> None:
    """Close a pull request with its reason appended to its body, in one edit:
    a comment and then a close sent the operator two emails for one event
    (2026-10-21), and a body edit sends none."""
    brain._check_written_text(comment)
    pr = gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data or {}
    body = str(pr.get("body") or "").rstrip()
    gh.rest("PATCH", "/repos/%s/pulls/%d" % (repo, number),
            {"state": "closed", "body": (body + "\n\n" if body else "") + comment})


def _ts(value: Any) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


ts_of = _ts


def _labels(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") if isinstance(x, dict) else x)
            for x in issue.get("labels") or []]


def _state(issue: Dict[str, Any]) -> str:
    states = ["loom:" + s for s in brain.STATES]
    return next((x[5:] for x in _labels(issue) if x in states), "")


def class_of(item: Dict[str, Any]) -> str:
    """The change class a lane pull request carries, or empty when absent."""
    return change_class.class_of(item)


def _lane(issue: Dict[str, Any]) -> str:
    return next((x[5:] for x in _labels(issue) if x.startswith("lane:")), "")


def rung_of(comments: Sequence[Dict[str, Any]]) -> int:
    """The rung of the ticket's latest rung marker, 0 when none. A marker of
    0 (posted by the brain or the operator) starts the ladder again."""
    found = [int(m.group(1)) for c in comments
             for m in RUNG_RE.finditer(str(c.get("body") or ""))]
    return found[-1] if found else 0


def last_climb(comments: Sequence[Dict[str, Any]]) -> float:
    """When the ticket last moved on the ladder (its newest rung marker), 0
    when never."""
    return max((_ts(c.get("created_at")) for c in comments
                if RUNG_RE.search(str(c.get("body") or ""))), default=0.0)


def stuck(comments: Sequence[Dict[str, Any]], open_threads: Sequence[str],
          checks_failing: bool, age: float, now: float = 0.0,
          no_test: bool = False, unticked: Sequence[str] = (),
          threads: Sequence[Dict[str, Any]] = ()) -> bool:
    """Did the last fix round fail to move this pull request? True when a
    thread it was asked to fix is still open, or an open one in `threads`
    holds the same finding (bus/converge.py: same path, a nearby line, the
    same rule; the review bot may post it again as a new thread), when
    checks it was asked to fix still fail, when it was asked for a test and
    still has none, or when the
    pull request is older than STUCK_AGE and has not moved on the ladder for
    STUCK_AGE (age alone climbs once per window, not on every round)."""
    if age > STUCK_AGE and now - last_climb(comments) > STUCK_AGE:
        return True
    last = next((str(c.get("body") or "") for c in reversed(comments)
                 if str(c.get("body") or "").startswith(FIX_MARKER)), None)
    if last is None:
        return False
    if _finding_survived(last, open_threads, threads):
        return True
    if no_test and NO_TEST_HEADING in last:
        return True
    if _unticked_left(last, unticked):
        return True
    return checks_failing and bool(FIX_CHECK_RE.search(last))


def _unticked_left(last: str, unticked: Sequence[str]) -> bool:
    """The last fix round named a criterion that is still unticked."""
    return bool(unticked) and UNTICKED_HEADING in last and any("- " + c in last for c in unticked)


def _finding_survived(last: str, open_threads: Sequence[str],
                      threads: Sequence[Dict[str, Any]]) -> bool:
    """A thread the last fix round asked about is still open, by its id or,
    posted again, by its finding's signature (bus/converge.py). Failing
    checks are judged by stuck() itself: any check the round was asked to fix
    while checks still fail."""
    return (bool(set(FIX_THREAD_RE.findall(last)) & set(open_threads))
            or bool(converge.persistent(last, (), threads)))


def _end(thread: Dict[str, Any], field: str, index: int) -> Dict[str, Any]:
    nodes = (thread.get(field) or {}).get("nodes") or [{}]
    return nodes[index] or {}


def bot_marked_fixed(thread: Dict[str, Any], bot: str) -> bool:
    """An open thread the review bot opened whose newest comment is its own
    "Fixed in" reply. A thread a person opened, or any reply after the
    bot's, stays open for a person or the brain."""
    last = _end(thread, "comments", -1)
    return (not thread.get("isResolved") and brain.by_bot(_end(thread, "root", 0), bot)
            and brain.by_bot(last, bot)
            and str(last.get("body") or "").startswith(BOT_FIXED_PREFIX))


def trusted_threads(threads: Sequence[Dict[str, Any]], bot: str,
                    trusted: set) -> List[Dict[str, Any]]:
    """The open threads whose root comment the review bot or a trusted login
    wrote: the only ones that may count toward stuck(). On a public repo
    anyone can open a thread, and a stranger's thread at a finding's path and
    line must not climb the ticket's ladder or stop its rounds."""
    def ours(t: Dict[str, Any]) -> bool:
        root = _end(t, "root", 0) or _end(t, "comments", 0)
        return (brain.by_bot(root, bot)
                or rest_login(root.get("author")).lower() in trusted)
    return [t for t in threads if not t.get("isResolved") and ours(t)]


def bot_fixed(threads: Sequence[Dict[str, Any]], bot: str) -> List[str]:
    return [str(t.get("id")) for t in threads if bot_marked_fixed(t, bot)]


def lane_answered(comment: Dict[str, Any], trusted: set) -> bool:
    """A lane's answer to a finding, posted as the reply on its thread by a
    trusted login (bus/station.py writes it as the App)."""
    return (rest_login(comment.get("author")).lower() in trusted
            and bool(ANSWER_RE.match(str(comment.get("body") or ""))))


def answered_threads(threads: Sequence[Dict[str, Any]], trusted: set) -> List[str]:
    """The open threads a lane answered instead of changing the code: the
    thread carries the answer as one of its replies."""
    out = []
    for t in threads:
        if t.get("isResolved"):
            continue
        nodes = (t.get("comments") or {}).get("nodes") or []
        if any(lane_answered(c, trusted) for c in nodes):
            out.append(str(t.get("id")))
    return out


def review_again() -> str:
    """The comment that asks the review bot to review a pull request's head
    again (LOOM_REVIEW_AGAIN overrides the default)."""
    return os.environ.get("LOOM_REVIEW_AGAIN", "").strip() or REVIEW_AGAIN


def asked_threads(comments: Sequence[Dict[str, Any]]) -> set:
    """Every thread id a loom:fix comment on the ticket asked a lane to fix."""
    return {t for c in comments if str(c.get("body") or "").startswith(FIX_MARKER)
            for t in FIX_THREAD_RE.findall(str(c.get("body") or ""))}


def deferrable(threads: Sequence[Dict[str, Any]], bot: str, asked: set,
               rounds: int, immediate: bool = False) -> List[Dict[str, Any]]:
    """The open threads to defer to a follow-up issue, or [] unless every open
    thread is a medium or low finding the review bot opened that a fix round
    asked for, or the pull request has had DEFER_AFTER fix rounds. With
    `immediate` (a docs or tests change) any such finding defers at once,
    with no fix round. A bad-work finding never defers."""
    still = [t for t in threads if not t.get("isResolved")]

    def minor(t: Dict[str, Any]) -> bool:
        root = _end(t, "root", 0)
        body = str(root.get("body") or "")
        # A bad-work finding (bus/merge_tier.py) says the change does not do
        # what it claims: it goes back to the lane at any severity.
        return (brain.by_bot(root, bot) and bool(MINOR_RE.match(body))
                and not merge_tier.bad_work(body)
                and (immediate or str(t.get("id")) in asked or rounds >= DEFER_AFTER))
    return still if still and all(minor(t) for t in still) else []


def finding_line(thread: Dict[str, Any]) -> str:
    """One ASCII line for a deferred finding: its severity line without the
    bot's symbols, its file, and a link to the thread."""
    root = _end(thread, "root", 0)
    lines = str(root.get("body") or "").strip().splitlines()
    first = lines[0] if lines else ""
    words = " ".join(first.encode("ascii", "ignore").decode().split())
    return "- %s in `%s`: %s" % (words or "finding", thread.get("path") or "?",
                                 root.get("url") or thread.get("id"))


def no_change_at(comments: Sequence[Dict[str, Any]]) -> float:
    """When the last fix round ended with no commit, 0 when it did not."""
    for c in reversed(comments):
        body = str(c.get("body") or "")
        if body.startswith(FIX_MARKER):
            return 0.0
        if NO_CHANGE_RE.match(body):
            return _ts(c.get("created_at"))
    return 0.0


def no_change_rounds(comments: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Per check name, the fix rounds in a row that named it and ended with no
    commit (a loom:fix comment, then a no-change note). A round that changed
    something, or one that no longer names the check, ends its count."""
    counts: Dict[str, int] = {}
    asked: set = set()
    answered = True
    for c in comments:
        body = str(c.get("body") or "")
        if body.startswith(FIX_MARKER):
            names = {n.strip() for n in re.findall(r"^### Check: (.*)$", body, re.M)}
            if not answered:
                counts = {}
            counts = {n: v for n, v in counts.items() if n in names}
            asked, answered = names, False
        elif NO_CHANGE_RE.match(body) and not answered:
            answered = True
            for name in asked:
                counts[name] = counts.get(name, 0) + 1
    return counts


def fixes_of(comments: Sequence[Dict[str, Any]]) -> int:
    return sum(1 for c in comments if str(c.get("body") or "").startswith(FIX_MARKER))


def pick_lane(lanes: Sequence[str], load: Dict[str, int]) -> str:
    """The least-loaded lane of a rung, the first listed on a tie."""
    return min(lanes, key=lambda lane: (load.get(lane, 0), lanes.index(lane)))


def tried_rungs(comments: Sequence[Dict[str, Any]]) -> set:
    """The rungs the ticket has run on since its ladder last started (the
    newest rung 0 marker). A rung counts once a claim follows its marker: a
    rung skipped because its lanes were busy, or one the ticket sat briefed
    on with no lane claiming it, is not tried (on 2026-10-13 a restart wave
    filled the claude and codex lanes, tickets spilled straight to rung 3 and
    gave up after one more round; on 2026-10-14 a ticket that never ran
    climbed past the last rung). An equivalent move (EQUIVALENT_MARKER)
    leaves its rung untried: the claim after it runs the equivalent model."""
    tried: set = set()
    pending: set = set()
    for c in comments:
        body = str(c.get("body") or "")
        if CLAIM_RE.match(body):
            tried |= pending
            pending = set()
        for m in RUNG_RE.finditer(body):
            rung = int(m.group(1))
            if rung == 0:
                tried, pending = set(), set()
            else:
                pending = pending | {rung}
        if EQUIVALENT_RE.search(body):
            pending = set()
    return tried


def _mark_model(body: str) -> Tuple[bool, Optional[str]]:
    """Whether a comment sets the ticket's model, and the model it sets: a
    factory rung marker gives its rung's model (rung 0 restarts the ladder
    and sets none), an equivalent move its equivalent model, a brain
    escalation its `Rung:` line's model."""
    found = [int(m.group(1)) for m in RUNG_RE.finditer(body)]
    if found:
        rung = found[-1]
        return True, CLIMB[rung - 1][1] if 1 <= rung <= len(CLIMB) else None
    equivalent = EQUIVALENT_RE.search(body)
    if equivalent:
        return True, equivalent.group(1)
    line = brain._RUNG_LINE.search(body) if body.startswith(brain.ESCALATE_MARKER) else None
    return (True, line.group(2)) if line else (False, None)


def known_models(env: Optional[Dict[str, str]] = None) -> set:
    """The model names Loom configures: every CLIMB, HARD_MODELS and
    EQUIVALENTS model, and each lane's LOOM_<LANE>_MODEL or LOOM_<LANE>_MODELS
    setting (an agy setting also with its effort suffix, as run_lane.sh adds
    it)."""
    env = os.environ if env is None else env
    found = {model for _, model in CLIMB} | set(HARD_MODELS.values())
    found |= {model for _, model, _ in EQUIVALENTS.values()}
    for key, value in env.items():
        if re.fullmatch(r"LOOM_[A-Z0-9_]+_MODELS?", key) and key != "LOOM_BRAIN_MODEL":
            for model in str(value).split():
                found |= {model} | {"%s-%s" % (model, e) for e in ("low", "medium", "high")}
    return found


def footer_run(body: Any, env: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, str]]:
    """(lane, model) from the runner's footer on a lane pull request body,
    the newest run on it, or None when the body has no footer or names a
    model Loom does not configure (known_models): a footer is text in a pull
    request body, so an unknown name never steers the ladder. Callers read it
    only from a trusted author's pull request in the ticket's own repo."""
    found = RUN_FOOTER_RE.findall(str(body or ""))
    if not found or found[-1][1] not in known_models(env):
        return None
    return found[-1][0], found[-1][1]


def ran_models(issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
               footer: Optional[Tuple[str, str]] = None) -> set:
    """The models the ticket was claimed on since its ladder last started
    (the newest rung 0 marker), each read from what ran (claim_models). On
    2026-10-16 a ticket's three attempts on gpt-6-sol carried no rung marker,
    and the factory moved it back to gpt-6-sol for a fourth."""
    ran: set = set()
    for comment, model in claim_models(issue, comments, footer):
        if comment is None:
            ran = set()  # a rung 0 marker starts the ladder again
        else:
            ran |= {model} - {None}
    return ran


def claim_models(issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
                 footer: Optional[Tuple[str, str]] = None) -> List[Tuple[Optional[Dict[str, Any]], Optional[str]]]:
    """Each claim comment, oldest first, as (comment, the model it ran on or
    None when nothing names one); a rung 0 marker appears as (None, None),
    where the ladder starts again. A claim ran on the Model: line in force:
    a rung marker, an equivalent move or a brain escalation sets it for the
    claims after it, and the body's line holds before the first of those
    when none rewrote it since (lanes/run_lane.sh runs a brief's Model: line
    as it is). A claim with no Model: line in force ran on the model the
    runner footer of its lane's pull request names (`footer`, (lane, model)),
    else on its lane's configured model setting (bus/brain.py lane_model),
    else on a model nothing names. Never a fixed lane-to-model map: on
    2026-10-17 five runs on lane claude used claude-opus-5-5 while the ladder
    recorded claude-sonnet-5-5 and gave up with two strong models untried.
    The dashboard reads the newest claim's model as the model the ticket's
    last run used."""
    match = re.search(r"^Model:[ \t]*(\S+)", str(issue.get("body") or ""), re.M)
    body_model = match.group(1) if match else None
    bodies = [str(c.get("body") or "") for c in comments]
    marks = [i for i, body in enumerate(bodies) if _mark_model(body)[0]]
    current = body_model if not marks else None
    out: List[Tuple[Optional[Dict[str, Any]], Optional[str]]] = []
    for i, body in enumerate(bodies):
        sets, model = _mark_model(body)
        if sets:
            if [m.group(1) for m in RUNG_RE.finditer(body)][-1:] == ["0"]:
                out.append((None, None))
            current = (body_model or model) if i == marks[-1] else model
            continue
        claim = CLAIMED_RE.fullmatch(body.strip())
        if claim:
            lane = claim.group(1)
            ran = footer[1] if footer and footer[0] == lane else brain.lane_model(lane)
            out.append((comments[i], current or ran))
    return out


def ran_rungs(issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
              footer: Optional[Tuple[str, str]] = None) -> set:
    """The rungs whose model the ticket already ran on (ran_models), with or
    without a rung marker: the ladder never gives a ticket a model that
    already failed it (docs/MUSE.md, Repeatedly stuck work)."""
    models = ran_models(issue, comments, footer)
    return {n for n, (_, model) in enumerate(CLIMB, 1) if model in models}


def reached_rung(comments: Sequence[Dict[str, Any]]) -> int:
    """The highest rung the ticket has run on since its ladder last started, 0
    when none."""
    return max(tried_rungs(comments), default=0)


def issue_model(issue: Dict[str, Any]) -> str:
    match = re.search(r"^Model:[ \t]*(\S+)", str(issue.get("body") or ""), re.M)
    return match.group(1) if match else "unspecified"


def _ending(body: str) -> Optional[str]:
    """The classed reason a comment ends the attempt before it with, or None
    when it ends none: a "Lane X failed:" line or one of the other endings
    (RETRY_ENDED_RE, HELD_RE, STOPPED_RE)."""
    for rx in (FAILED_RE, RETRY_ENDED_RE, STOPPED_RE):
        match = rx.match(body)
        if match:
            return body[match.end():]
    held = HELD_RE.match(body)
    return "[factory] " + body[held.end():] if held else None


def _attempt_rows(comments: Sequence[Any], reset_at: Any = None) -> List[List[Any]]:
    """Each claim as [epoch, time, lane, reason it failed], oldest first, from
    the claim comments and the endings that followed them (_ending). An
    attempt takes the first ending after it, or "" when none was recorded.
    With `reset_at` given, a comment it matches starts a new segment: only the
    last segment's claims are returned."""
    found: List[List[Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "").strip() if isinstance(comment, dict) else ""
        if reset_at is not None and reset_at(body):
            found = []
            continue
        claim = CLAIMED_RE.fullmatch(body)
        at = _ts(comment.get("created_at")) if claim else 0.0
        if claim and at:
            found.append([int(at), str(comment["created_at"]), claim.group(1), ""])
        ending = _ending(body)
        if ending and found and not found[-1][3]:
            found[-1][3] = ending
    return found


def attempts(comments: Sequence[Any], current: str = "") -> List[Tuple[int, str, str, str]]:
    """Each claim as (epoch, time, lane, reason it failed), oldest first, from
    the claim comments and their endings (_attempt_rows). An attempt takes the
    ending that followed it, or "" when none was recorded; with `current` given, the last
    attempt is the one being reported and takes that reason."""
    found = _attempt_rows(comments)
    if found and current:
        found[-1][3] = current
    return [(a[0], a[1], a[2], a[3]) for a in found]


def _is_brief(body: str) -> bool:
    """Whether the comment assigns the ticket to a lane: a brief."""
    return any(rx.search(body) for rx in BRIEF_RES)


# The head of the note _give_up leaves when a ticket is past the last rung.
GAVE_UP = "The factory tried every rung of the model ladder"
# The class bus/station.py's CLASS_RE reads off a reported reason.
ENDING_CLASS_RE = re.compile(r"^\[(limit|transient|work|factory|auth)\] ")


# An agent run cut off at the agent timeout (lanes/agent_cli.sh classes exit 124
# [transient]). The class may be missing (an older reason) or [work] (the
# station's own reclassing of a repeated timeout, below), never [limit].
TIMEOUT_RE = re.compile(r"^(?:\[(?:transient|work)\] )?agent exited 124(?!\d)")
# Two runs in a row cut off on one ticket are the ticket's doing, not a lane's.
TIMEOUT_STRIKES = 2


def is_timeout(reason: str) -> bool:
    """Whether a classed failure reason is an agent timeout (exit 124)."""
    return bool(TIMEOUT_RE.match(reason))


def timeouts_in_a_row(rows: Sequence[Tuple[int, str, str, str]]) -> int:
    """How many of the newest `rows` (attempts, oldest first) ended at an
    agent timeout, counting back until one ended any other way."""
    count = 0
    for row in reversed(rows):
        if not is_timeout(row[3]):
            break
        count += 1
    return count


def newest_ending_class(comments: Sequence[Any]) -> str:
    """The class of the ending that closed the ticket's newest claim: limit,
    transient, factory, auth or work. A reason with no class, and a newest
    claim with no recorded ending, are work (the cautious reading: only an
    ending that says the lane did no work may keep a ticket from climbing)."""
    rows = attempts(comments)
    match = ENDING_CLASS_RE.match(rows[-1][3]) if rows else None
    return match.group(1) if match else "work"


def attempts_since_brief(comments: Sequence[Any]) -> List[Tuple[int, str, str, str]]:
    """The attempts after the ticket's last brief (its last lane assignment),
    oldest first, each with the failure that followed it. A ticket briefed
    with no assignment comment keeps every claim."""
    return [(a[0], a[1], a[2], a[3]) for a in _attempt_rows(comments, reset_at=_is_brief)]


def dead_lane(comments: Sequence[Any]) -> Optional[str]:
    """The one lane every attempt since the ticket's last brief ran on, or
    None when the attempts spread across lanes or there are none (loom#565)."""
    lanes = {lane for _, _, lane, _ in attempts_since_brief(comments)}
    return next(iter(lanes)) if len(lanes) == 1 else None


def _cell(text: str) -> str:
    """A reason as one table cell: its first line, with pipes escaped."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first.replace("|", "\\|") or "-"


def attempt_models(issue: Dict[str, Any], comments: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """{a claim's created_at: the model it ran (claim_models)} for each claim
    whose model something names."""
    return {str(c.get("created_at")): model for c, model in claim_models(issue, comments)
            if c is not None and model}


def runaway_comment(trigger: str, rows: Sequence[Tuple[int, str, str, str]], model: str,
                    ran: Optional[Dict[str, str]] = None,
                    counted: Optional[Callable[[str], bool]] = None) -> str:
    """The comment a flagged ticket carries: why, and its attempt table, each
    attempt with the model it ran (`ran`, from attempt_models), else `model`.
    With `counted` (reason -> counts as a work attempt), each row says so: a
    brain read a limit row in the table as a counted attempt and filed a
    factory ticket against a detector that was right (2026-10-21)."""
    ran = ran or {}
    if counted is None:
        lines = ["Runaway: %s." % trigger, "| Time | Lane | Model | Reason |", "|---|---|---|---|"]
        lines += ["| %s | %s | %s | %s |" % (stamp, lane, ran.get(stamp) or model, _cell(reason))
                  for _, stamp, lane, reason in rows]
        return "\n".join(lines)
    lines = ["Runaway: %s." % trigger, "| Time | Lane | Model | Reason | Counted |",
             "|---|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s | %s |" % (stamp, lane, ran.get(stamp) or model, _cell(reason),
                                              "yes" if counted(reason) else "no")
              for _, stamp, lane, reason in rows]
    return "\n".join(lines)


def repeated_no_reason(comments: Sequence[Dict[str, Any]]) -> bool:
    """Did the ticket's newest failure commit nothing and give no reason, as
    an earlier one did since its ladder last started (the newest rung 0
    marker)? The ladder already climbed for that reason once, and a stronger
    model did not help: five tickets climbed 4 to 6 times on it on
    2026-10-13."""
    seen: List[bool] = []
    for c in comments:
        body = str(c.get("body") or "")
        if any(m.group(1) == "0" for m in RUNG_RE.finditer(body)):
            seen = []
        if FAILED_RE.match(body):
            seen.append(NO_REASON in body)
    return bool(seen) and seen[-1] and sum(seen) >= 2


# lanes/run_lane.sh's reason when the agent edited a submodule (loom#1316).
SUBMODULE_EDIT_RE = re.compile(r"the change edits files inside submodule (\S+)")


def needs_other_repo(comments: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The submodule the ticket's newest failure says needs the change, else
    None. The change belongs in that submodule's own repo, which a push of
    this one cannot carry, so a stronger model fails the same way: three
    tickets needed such a split on 2026-10-21."""
    newest = None
    for c in comments:
        body = str(c.get("body") or "")
        if FAILED_RE.match(body):
            newest = body
    match = SUBMODULE_EDIT_RE.search(newest or "")
    return match.group(1) if match else None


def current_rung(issue: Dict[str, Any]) -> int:
    """The rung whose model and lane the ticket runs on now, 0 when none: a
    brief can start on a ladder model, and failing there tries that rung."""
    m = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
    model = m.group(1) if m else ""
    return next((n for n, (lanes, rung_model) in enumerate(CLIMB, 1)
                 if rung_model == model and _lane(issue) in lanes), 0)


def climb_rungs(tried: set, reached: int = 0) -> List[int]:
    """The rungs of CLIMB a ticket may still climb to, in order: each one not
    in `tried` above `reached`, the highest rung the ladder itself moved it
    to. Once it ran on the ladder's last rung or past it, every untried
    STRONG one instead, never back to a weaker model: with Opus the only
    strong model since 2026-10-21, a ticket Opus failed has none left."""
    if any(n >= len(LADDER) for n in tried | {reached}):
        return [n for n in range(1, len(CLIMB) + 1)
                if n not in tried and CLIMB[n - 1][1] in STRONG]
    return [n for n in range(reached + 1, len(CLIMB) + 1) if n not in tried]


def next_rung(tried: set, load: Dict[str, int], eligible: Optional[set] = None,
              reached: int = 0,
              lanes: Optional[Dict[int, Tuple[str, ...]]] = None) -> int:
    """The lowest rung of climb_rungs with a lane under RUNG_BUSY
    (docs/MUSE.md: a higher rung rather than a wait behind a busy lane that
    is up), else the one whose least-loaded lane holds the least, to wait
    there. 0 when none is left. Waiting on the lowest rung instead queued 33
    tickets on the one claude lane on 2026-10-13 while six other ladder slots
    sat idle. `lanes`, when given, maps each rung to the lanes that may serve
    it now (Factory._up_lanes): only those count toward its load, and a rung
    with none is passed over. Counting every CLIMB lane made rung 3 look free
    through the idle anthropic slots while they were out of credit."""
    untried = [n for n in climb_rungs(tried, reached)
               if (eligible is None or n in eligible) and (lanes is None or lanes.get(n))]
    if not untried:
        return 0
    least = {n: min(load.get(lane, 0)
                    for lane in (CLIMB[n - 1][0] if lanes is None else lanes[n]))
             for n in untried}
    free = [n for n in untried if least[n] < RUNG_BUSY]
    return free[0] if free else min(untried, key=lambda n: (least[n], n))


def pr_faults(details: Dict[str, Any], full: Dict[str, Any],
              runs: Sequence[Dict[str, Any]] = ()) -> List[str]:
    """What keeps a pull request from landing, in words for its ticket. A
    failing check is a red rollup or a failed run among `runs`, the newest
    run of each check (brain.newest_check_runs), which is what the fix brief
    quotes, so the factory and the brief name the same failures."""
    failed = any(r.get("conclusion") in brain.FAILED_CONCLUSIONS for r in runs)
    return [w for bad, w in ((failed or not brain._checks_green(details), "failing checks"),
                             (not brain._threads_resolved(details), "open review threads"),
                             (full.get("mergeable") is False, "a conflict")) if bad]


def chief_orphans(summary: str) -> Optional[List[int]]:
    """The tickets the plan check's `which epic` row says belong to no epic,
    when that row is the only one failing in its summary table; else None.
    A table row reads `| which epic | fail | #5, #9 belong to no epic - ... |`
    (services/_shared/personas/tpm/persona.py in the review bot's repo)."""
    failing = []
    for line in str(summary or "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[1].lower() == "fail":
            failing.append((cells[0].lower(), "|".join(cells[2:])))
    if len(failing) != 1 or failing[0][0] != EPIC_ROW:
        return None
    named = failing[0][1].partition(" belong to no epic")[0]
    return [int(n) for n in _ORPHAN_NUMBER_RE.findall(named)] or None


def acceptance_criteria(body: str) -> List[str]:
    """Checkbox criteria in the Acceptance criteria section."""
    found = []
    active = False
    for line in (body or "").splitlines():
        if line.strip() == "## Acceptance criteria":
            active = True
            continue
        if active and line.startswith("## "):
            break
        match = CRITERION_RE.match(line)
        if active and match:
            found.append(("x" if match.group(1).lower() == "x" else "") +
                         " " + match.group(2).strip())
    return found


def missing_criteria(ticket_body: str, pull_body: str) -> List[str]:
    """Ticket criteria that a lane PR leaves unticked, excluding live checks
    and operator actions (bus/criteria_gate.py): only the station's verifier
    or the operator can meet those, so a lane is never asked to tick them."""
    ticket = acceptance_criteria(ticket_body)
    pull = acceptance_criteria(pull_body)
    states = {}
    for criterion in pull:
        checked, _, text = criterion.partition(" ")
        clean = NOT_BUILT_RE.sub("", text).strip().casefold()
        states[clean] = (checked == "x", bool(NOT_BUILT_RE.search(text)))
    missing = []
    for criterion in ticket:
        _, _, text = criterion.partition(" ")
        if criteria_gate.kind(NOT_BUILT_RE.sub("", text)) != criteria_gate.CODE:
            continue
        clean = NOT_BUILT_RE.sub("", text).strip().casefold()
        checked, not_built = states.get(clean, (False, False))
        if not checked and not not_built:
            missing.append(text)
    return missing


def not_built_criteria(ticket_body: str, pull_body: str) -> List[str]:
    """Ticket criteria explicitly marked not built with a reason in a PR."""
    ticket = [item.partition(" ")[2] for item in acceptance_criteria(ticket_body)]
    marked = {NOT_BUILT_RE.sub("", item.partition(" ")[2]).strip().casefold()
              for item in acceptance_criteria(pull_body)
              if NOT_BUILT_RE.search(item.partition(" ")[2])}
    return [item for item in ticket
            if not item.strip().casefold().startswith("live:")
            and item.strip().casefold() in marked]


def is_code(path: str) -> bool:
    return path.lower().endswith(CODE_EXTS)


def is_test(path: str) -> bool:
    folders, _, name = path.lower().rpartition("/")
    return (bool({"test", "tests", "t", "spec", "__tests__"} & set(folders.split("/")))
            or name.startswith("test")
            or bool(TEST_NAME_RE.search(name)))


def lacks_test(paths: Sequence[str]) -> bool:
    """Does a change to these files change code and no test?"""
    return any(is_code(p) for p in paths) and not any(is_test(p) for p in paths)


def open_thread_ids(details: Dict[str, Any]) -> List[str]:
    threads = (details.get("reviewThreads") or {}).get("nodes") or []
    return [str(t.get("id")) for t in threads if not t.get("isResolved")]


def body_leak_hits(log: Sequence[str]) -> Optional[Dict[int, str]]:
    """{line of the scanned text: rule} when a leak-scan job log failed in its
    title-and-body section with a clean diff section; else None. The scanned
    text is the title on line 1 and the body from line 2."""
    marks = [l.strip() for l in log]
    if LEAK_TEXT_MARK not in marks or LEAK_DIFF_MARK not in marks:
        return None
    cut = len(marks) - 1 - marks[::-1].index(LEAK_TEXT_MARK)
    start = max((i for i in range(cut) if marks[i] == LEAK_DIFF_MARK), default=-1)
    diff, text = marks[start + 1:cut], log[cut + 1:]
    if (start < 0 or not any(l.startswith("leak guard: clean") for l in diff)
            or any(l.startswith(("BLOCKED", "FATAL")) for l in diff)):
        return None
    if not any(l.strip().startswith("BLOCKED") for l in text):
        return None
    hits = {int(m.group(1)): m.group(2) for m in map(LEAK_HIT_RE.match, text) if m}
    return hits or None


def refs_in_words(line: str, repo: str) -> str:
    """The line with each [owner/]name#N in words: "issue N of this repo" for
    the repo's own, "issue N of another repo" for any other."""
    owner, name = repo.lower().split("/", 1)

    def words(m: "re.Match[str]") -> str:
        own = m.group(2).lower() == name and (m.group(1) or owner).lower() == owner
        return "issue %s of %s" % (m.group(3), "this repo" if own else "another repo")
    return SCAN_REF_RE.sub(words, line)


def repair_leak_text(title: str, body: str, hits: Dict[int, str],
                     repo: str) -> Optional[Tuple[str, str, List[str]]]:
    """(title, body, what changed) with each flagged line rewritten in words,
    or dropped when it cannot be; None when a flag does not fit the text (a
    title that is not a reference, a line that is not there)."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if len(title.splitlines()) != 1:
        return None
    changed: List[str] = []
    dropped = set()
    for at, rule in sorted(hits.items()):
        refs = rule == LEAK_REF_RULE
        if at == 1:
            if not refs or not SCAN_REF_RE.search(title):
                return None
            title = refs_in_words(title, repo)
            changed.append("title: put a cross-repo issue reference in words")
        elif not 2 <= at <= len(lines) + 1:
            return None
        elif refs and SCAN_REF_RE.search(lines[at - 2]):
            lines[at - 2] = refs_in_words(lines[at - 2], repo)
            changed.append("body line %d: put a cross-repo issue reference in words" % (at - 1))
        else:
            dropped.add(at - 2)
            changed.append("body line %d: dropped (flagged as %s)" % (at - 1, rule))
    kept = [l for i, l in enumerate(lines) if i not in dropped]
    return title, "\n".join(kept), changed


def held_until(issue: Dict[str, Any]) -> float:
    """The ticket's Not before: time, 0 when it has none or it is unreadable."""
    try:
        return tickets.not_before(tickets.parse(issue.get("body"))[0]) or 0.0
    except ValueError:
        return 0.0


def landing_order(prs: List[Dict[str, Any]], tickets: Dict[int, Dict[str, Any]],
                  linked: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull requests of loom:urgent, loom:regression and loom:revert tickets
    first, then by their ticket's priority (P0 first), then the oldest."""
    urgent = {n for n, issue in tickets.items() if set(FIRST) & set(_labels(issue))}
    ticket_of = {int(pr["number"]): n for n, pr in linked.items()}

    def rank(pr: Dict[str, Any]) -> int:
        issue = tickets.get(ticket_of.get(int(pr["number"])) or 0)
        return PRIORITIES.index(priority_of(issue or {}))
    return sorted(prs, key=lambda p: (ticket_of.get(int(p["number"])) not in urgent,
                                      rank(p), str(p.get("created_at") or "")))


def with_model(body: str, model: str) -> str:
    """The ticket body with its Model: header line set to `model`, or to its
    stand-in when it is forbidden (tickets.allowed_model)."""
    model = tickets.allowed_model(model)
    head, sep, rest = body.partition("\n\n")
    if MODEL_LINE.search(head + "\n"):
        return MODEL_LINE.sub("Model: %s\n" % model, head + "\n", count=1).rstrip("\n") + sep + rest
    return "Model: %s\n%s" % (model, body)


def without_model(body: str) -> str:
    """The ticket body without its Model: header line, so the lane's own
    default model runs it."""
    head, sep, rest = body.partition("\n\n")
    return MODEL_LINE.sub("", head + "\n", count=1).rstrip("\n") + sep + rest


# The tier of a ticket whose Difficulty line is missing or names no tier.
# Such a ticket was in no tier, so no lane ever took it: one waited a day in
# the queue (2026-10-21). Medium's FALLBACK reaches both other tiers.
DEFAULT_DIFFICULTY = "medium"


def has_difficulty(issue: Dict[str, Any]) -> bool:
    """Whether the ticket's own Difficulty: line names a tier (difficulty_of
    gives DEFAULT_DIFFICULTY in place of one that does not)."""
    try:
        found = str(tickets.parse(issue.get("body"))[0].get("Difficulty", "")).strip().lower()
    except ValueError:
        return False
    return found in TIERS


def difficulty_of(issue: Dict[str, Any]) -> str:
    """The ticket's Difficulty: line, DEFAULT_DIFFICULTY when it has none
    readable or it names no tier."""
    if not has_difficulty(issue):
        return DEFAULT_DIFFICULTY
    return str(tickets.parse(issue.get("body"))[0]["Difficulty"]).strip().lower()


def lanes_of(difficulty: str, private: bool) -> Tuple[str, ...]:
    """The lanes of a difficulty tier a repo may run on, best first."""
    return tuple(x for x in TIERS.get(difficulty, ())
                 if not (private and x in PUBLIC_ONLY))


def tier_lanes(issue: Dict[str, Any], private: bool) -> Tuple[str, ...]:
    """The lanes of the ticket's difficulty tier it may run on, best first;
    none when it has no readable Difficulty: line."""
    return lanes_of(difficulty_of(issue), private)


def agy_takes_header(issue: Dict[str, Any], rung_only: bool = False) -> bool:
    """Whether agy can take the ticket's Model: header when it is kept."""
    match = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
    if not match:
        return True
    model = match.group(1)
    if rung_only and model not in (rung_model for _, rung_model in LADDER):
        return True
    from .station import lane_takes_model
    return lane_takes_model("agy", model)


def _placements(found: Dict[str, Dict[int, Dict[str, Any]]]) -> Dict[str, Tuple[str, str]]:
    """"OWNER/NAME#N" -> (state, lane) for each ticket, before a pass moves any."""
    return {"%s#%s" % (repo, number): (_state(issue), _lane(issue))
            for repo, issues in found.items() for number, issue in issues.items()}


class Factory:
    def __init__(self, gh: GitHub, repos: Sequence[str], now: float,
                 trusted: Sequence[str] = (),
                 briefers: Sequence[str] = (),
                 review_check: str = DEFAULT_REVIEW_CHECK,
                 review_bot: str = DEFAULT_REVIEW_BOT,
                 merge: Callable[..., Any] = brain.merge,
                 fix: Callable[..., Any] = brain.fix,
                 move: Callable[..., Any] = brain.move,
                 limited: Sequence[str] = (),
                 no_test_repos: Sequence[str] = (),
                 available: Optional[Sequence[str]] = None,
                 close: Optional[Callable[..., Any]] = None,
                 held: Sequence[str] = (),
                 model_limits: Optional[Dict[str, Dict[str, float]]] = None,
                 silent: Optional[Dict[str, float]] = None,
                 down: Sequence[str] = (),
                 limit_until: Optional[Dict[str, float]] = None,
                 benched: Sequence[Tuple[str, str]] = (),
                 known_waits: Optional[Dict[str, str]] = None,
                 human: Callable[..., Any] = brain.human,
                 place: Callable[..., Any] = brain.place_under_epic,
                 plan_check: str = DEFAULT_PLAN_CHECK,
                 scope: Optional[scope_fit.Ask] = None,
                 demoted: Optional[Dict[str, str]] = None,
                 operators: Optional[Sequence[str]] = None,
                 no_lane_since: Optional[Dict[str, float]] = None,
                 reroute_after: Optional[float] = None) -> None:
        self.gh = gh
        self.repos = list(repos)
        self.now = now
        self.trusted = {t.lower() for t in trusted}
        # LOOM_TRUSTED_BRIEFERS: their loom:ticket briefs are tickets here;
        # their comments and pull requests are not trusted.
        self.briefers = {b.lower() for b in briefers}
        self.review_check = review_check
        self.review_bot = review_bot
        self.merge, self.fix, self.move = merge, fix, move
        self.human = human
        # Puts a ticket that sits under no epic under one (brain.place_under_epic).
        self.place = place
        self.plan_check = plan_check
        self.close = close if close is not None else _default_close
        self.limited = set(limited)
        # Lanes whose fresh heartbeat says unavailable for another reason (an
        # expired key, a lane stopped on purpose): their briefed tickets step
        # aside as from a limit. Left to the BRIEFED_STALL move, three tickets
        # waited two hours on lanes whose key had expired (2026-10-19).
        self.down = set(down)
        # Lane slots demoted for bad work (bus/scorecard.py), and why: down
        # for build tickets until the operator clears them.
        self.demoted = dict(demoted or {})
        self.down |= set(self.demoted)
        # Lane -> epoch its limit lifts, from its heartbeat (retry_at).
        self.limit_until = dict(limit_until or {})
        # Lane -> {model: epoch its limit lifts}: a lane with a model ladder
        # is up while one model is limited, but not for that model.
        self.model_limits = dict(model_limits or {})
        # (provider, model) pairs benched for everyone after repeated failure
        # (bus/bench.py, loom#969): a lane of the provider does not run that
        # model, whatever the ticket pins, until the bench list lets it back.
        self.benched = [(str(p), str(m)) for p, m in benched]
        # Lanes whose heartbeat says available; None trusts every lane not
        # at a limit.
        self.available = None if available is None else set(available)
        # The tickets the station holds, "owner/repo#n": held for a factory
        # incident (they keep their running label and lane, but no lane is
        # working them) or refused this pass by the unproven-work gate.
        self.held = set(held)
        # Lane -> seconds since its last heartbeat, for each local lane
        # without a live one; silent_running is the lanes found holding a
        # running ticket past SILENT_MAX, with those tickets.
        self.silent = dict(silent or {})
        self.silent_running: Dict[str, List[Tuple[str, int]]] = {}
        # "OWNER/NAME#N" -> why this pass left that ticket where it is, for
        # the station to time (invariants.ticket_waiting). A ticket held by
        # its own Not before: line is waiting on purpose and is not here.
        self.waits: Dict[str, str] = {}
        # The same map as the last pass left it (the station's saved waits): a
        # ticket whose reason is unchanged is not announced again.
        self.known_waits = dict(known_waits or {})
        # "OWNER/NAME#N" -> when the ticket first waited with no lane that
        # takes it (the station's ticket-waits.json, no_lane_since): past
        # reroute_after it is rerouted, past NO_LANE_HANDOFF handed off.
        self.no_lane_since = dict(no_lane_since or {})
        self.reroute_after = no_lane_reroute() if reroute_after is None else reroute_after
        # The tickets whose no-lane wait ended this pass: a lane took them,
        # they were rerouted, or they went to the hand-off pool. The station
        # clears their no_lane_since.
        self.settled: set = set()
        # The operators whose answers on the dashboard (bus/answer.py) count:
        # LOOM_OPERATORS, else the person logins among the trusted ones.
        self.operators = [o.lower() for o in (operators if operators is not None
                                              else answer.operators(trusted=trusted))]
        self._private: Dict[str, bool] = {}
        self.no_test_repos = {r.lower() for r in no_test_repos}
        # Sends the scope check's prompt to a cheap model (bus/scope_fit.py);
        # None skips the check.
        self.scope = scope
        self.actions = 0
        self.load: Dict[str, int] = {}
        self.landed: set = set()
        self.updated: set = set()
        self._runs: Dict[str, List[Dict[str, Any]]] = {}
        # The tickets named by After: lines, and closed tickets whose lane
        # pull requests are still open, read once a pass.
        self._depends: Dict[str, Optional[Dict[str, Any]]] = {}
        self._merged_prs: Dict[str, Dict[int, int]] = {}

    # -- reads -----------------------------------------------------------
    def _trusted(self, item: Dict[str, Any]) -> bool:
        """Opened by a trusted login, and not assigned to another operator
        (docs/LANES.md, Two Looms on shared repos): that operator's Loom
        moves it, not this one. The station logs each such skip once."""
        login = str((item.get("user") or {}).get("login") or "").lower()
        return (bool(login) and login in self.trusted
                and tickets.owned_elsewhere(item, self.trusted) is None)

    def _trusted_ticket(self, item: Dict[str, Any]) -> bool:
        """A ticket a briefing login filed (tickets.brief_author_trusted),
        not assigned to another operator; a trusted briefer's own loom:ticket
        is a hand-off, whoever it is assigned to among them."""
        return (tickets.brief_author_trusted(item, self.trusted, self.briefers)
                and tickets.owned_elsewhere(item, self.trusted, self.briefers) is None)

    def _dependency(self, repo: str, number: int) -> Optional[Dict[str, Any]]:
        """The ticket an After: line names, read once a pass; None when it
        cannot be read, which keeps the hold."""
        key = "%s#%d" % (repo, number)
        if key not in self._depends:
            try:
                data = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
                self._depends[key] = data if isinstance(data, dict) else None
            except GitHubError as err:
                _say("could not read %s: %s" % (key, err))
                self._depends[key] = None
        return self._depends[key]

    def _ticket(self, repo: str, number: int) -> Optional[Dict[str, Any]]:
        """A trusted ticket issue, read once a pass; None when it cannot be
        read or a stranger opened it."""
        data = self._dependency(repo, number)
        return data if data is not None and self._trusted_ticket(data) else None

    def _tickets(self, repo: str) -> Dict[int, Dict[str, Any]]:
        data = self.gh.paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo)
        return {int(i["number"]): i for i in data
                if isinstance(i, dict) and "pull_request" not in i and "number" in i
                and self._trusted_ticket(i)}

    def _status_note(self, repo: str, number: int, text: str) -> None:
        """A routine event on the ticket's one status comment, edited in place
        (bus/status.py): a new comment would email the operator (loom#866)."""
        status.add(self.gh, repo, number, text, self.now, self.trusted)

    def _comments(self, repo: str, number: int) -> List[Dict[str, Any]]:
        """An issue's comments (a ticket's or a pull request's) from trusted
        logins only."""
        data = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        return [c for c in data if isinstance(c, dict) and self._trusted(c)]

    def _is_private(self, repo: str) -> bool:
        """Whether the repo is private; unreadable counts as private."""
        if repo not in self._private:
            data = self.gh.rest("GET", "/repos/%s" % repo).data
            self._private[repo] = not isinstance(data, dict) or data.get("private") is not False
        return self._private[repo]

    def _up(self, lane: str) -> bool:
        """A lane that is up and not at a limit."""
        return lane not in self.limited and (self.available is None or lane in self.available)

    def _serves(self, lane: str, model: str) -> bool:
        """Whether the lane can run this model now: no limit on it there and
        not benched for failing."""
        return (model not in self.model_limits.get(lane, {})
                and not bench.is_benched(self.benched, lane, model))

    def _free(self, lane: str) -> bool:
        """A lane that is up, not at a limit, and holds no briefed or
        running ticket."""
        return self._up(lane) and self.load.get(lane, 0) == 0

    def _tier_move(self, repo: str, issue: Dict[str, Any],
                   old: str = "", allow_remote: bool = True) -> Tuple[Optional[str], str]:
        """(lane, fallback) for a tier move off lane `old`: the first free lane
        of the ticket's tier, with fallback ""; else, when no lane of its tier
        is up, the first free lane of its FALLBACK tiers in order, with
        fallback saying so. (None, "") when it should wait."""
        private = self._is_private(repo)
        difficulty = difficulty_of(issue)
        agy_ok = agy_takes_header(issue, rung_only=True)
        own = [x for x in lanes_of(difficulty, private)
               if x != old and (allow_remote or x not in REMOTE_LANES)
               and (x != "agy" or agy_ok)]
        lane = next((x for x in own if self._free(x)), None)
        if lane is not None or any(self._up(x) for x in own):
            return lane, ""
        whole = own + ([old] if old in lanes_of(difficulty, private) and old not in own else [])
        if not all(self._out_hours(x) for x in whole):
            # A lane of the tier lifts within LIMIT_REQUEUE: wait for it
            # rather than leave the tier (loom#1224).
            return None, ""
        for tier in FALLBACK.get(difficulty, ()):
            fallback_lanes = [x for x in lanes_of(tier, private)
                              if x != old and (allow_remote or x not in REMOTE_LANES)
                              and (x != "agy" or agy_ok)]
            lane = next((x for x in fallback_lanes if self._free(x)), None)
            if lane is not None:
                return lane, "fallback: no %s lane is up" % difficulty
        return None, ""

    def _check_runs(self, repo: str, sha: str) -> List[Dict[str, Any]]:
        """The newest run of each check on the commit, all pages."""
        runs: List[Dict[str, Any]] = []
        for page in range(1, 11):
            data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?filter=all&per_page=100&page=%d"
                                % (repo, sha, page)).data or {}
            batch = [r for r in data.get("check_runs") or [] if isinstance(r, dict)]
            runs += batch
            if len(batch) < 100:
                break
        return brain.newest_check_runs(runs)

    def _files(self, repo: str, number: int) -> List[str]:
        """Every file path the pull request changes, all pages (GitHub lists
        at most 3000)."""
        paths: List[str] = []
        for page in range(1, 31):
            data = self.gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100&page=%d"
                                % (repo, number, page)).data or []
            batch = [str(f.get("filename") or "") for f in data if isinstance(f, dict)]
            paths += batch
            if len(batch) < 100:
                break
        return paths

    def _needs_test(self, repo: str, number: int, issue: Dict[str, Any]) -> bool:
        """Does this pull request change code and no test, in a repo and on a
        ticket that ask for tests?"""
        if repo.lower() in self.no_test_repos or REVERT_LABEL in _labels(issue):
            return False
        if TESTS_NONE_RE.search(str(issue.get("body") or "")):
            return False
        return lacks_test(self._files(repo, number))

    def _review_state(self, repo: str, number: int, sha: str, pushed_at: float) -> str:
        """"wait" while any check runs; "reviewed" once the review check has
        run on this commit; else "unreviewed", which never merges."""
        runs = self._runs[sha] = self._check_runs(repo, sha)
        if any(r.get("status") != "completed" for r in runs):
            return "wait"
        if any(r.get("name") == self.review_check for r in runs):
            return "reviewed"
        if self.now - pushed_at <= REVIEW_GRACE:
            # A fresh push: its checks and review may not have started yet,
            # and the open threads still belong to the last head.
            return "wait"
        _say("%s#%d has no %s on its head commit; it waits for the brain"
             % (repo, number, self.review_check))
        return "unreviewed"

    def _reviewed_since(self, sha: str, since: float) -> bool:
        """Has the review check finished on this commit at or after `since`?"""
        return any(r.get("name") == self.review_check and _ts(r.get("completed_at")) >= since
                   for r in self._runs.get(sha, []))

    # -- writes ----------------------------------------------------------
    def _threads(self, repo: str, number: int) -> List[Dict[str, Any]]:
        """The pull request's review threads, with their replies."""
        owner, name = repo.split("/", 1)
        data = self.gh.graphql(THREADS_QUERY, {"owner": owner, "name": name,
                                               "number": number}) or {}
        return ((((data.get("repository") or {}).get("pullRequest") or {})
                 .get("reviewThreads") or {}).get("nodes") or [])

    def _resolve_bot_fixed(self, repo: str, number: int) -> None:
        picked = bot_fixed(self._threads(repo, number), self.review_bot)
        for thread_id in picked:
            self.gh.graphql(RESOLVE_MUTATION, {"id": thread_id})
        if picked:
            _say("resolved %d thread(s) on %s#%d that the review bot marked fixed"
                 % (len(picked), repo, number))

    def _mark_sensitive(self, repo: str, number: int, full: Dict[str, Any]) -> None:
        """A sensitive-class pull request is never merged by the factory: mark
        it needs-brain so the brain reviews and merges it (docs/MUSE.md,
        Merge by change class). The mark is added once."""
        if "needs-brain" in _labels(full):
            return
        self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": ["needs-brain"]})
        self.actions += 1
        _say("%s#%d is a sensitive change; marked needs-brain for the brain" % (repo, number))

    def _follow_up(self, repo: str, number: int, ticket: int, sha: str,
                   picked: Sequence[Dict[str, Any]], rounds: int) -> int:
        """The open follow-up issue for this pull request's deferred findings,
        filed now unless an earlier pass already filed it."""
        title = DEFER_TITLE % number
        known = (title, DEFER_TITLE_LEGACY % (repo, number))
        for found in self.gh.paginate("/repos/%s/issues?labels=%s&state=open" % (repo, DEFER_LABEL)):
            if isinstance(found, dict) and found.get("title") in known and self._trusted(found):
                return int(found["number"])
        body = ("Review findings deferred from #%d (ticket #%d) when it landed: the "
                "review check passed on %s after %d fix round(s), and these medium or "
                "low findings were still open. The brain rewrites this into a ticket "
                "or closes it.\n\n%s\n" % (number, ticket, sha[:12], rounds,
                                             "\n".join(finding_line(t) for t in picked)))
        brain._check_written_text(title + "\n" + body)
        made = self.gh.rest("POST", "/repos/%s/issues" % repo,
                            {"title": title, "body": body, "labels": [DEFER_LABEL]}).data or {}
        return int(made.get("number") or 0)

    def _defer_minor(self, repo: str, number: int, ticket: int, sha: str,
                     comments: Sequence[Dict[str, Any]], immediate: bool = False) -> bool:
        """Defer the open medium and low findings of a reviewed, green pull
        request, then resolve them. True when it did. With `immediate` (docs,
        tests, or a revert) they defer at once. The follow-up issue is not
        filed here: the pull request has not merged, and may never
        (settle_deferred files it once it does)."""
        threads = self._threads(repo, number)
        rounds = fixes_of(comments)
        picked = deferrable(threads, self.review_bot, asked_threads(comments), rounds, immediate)
        if not picked:
            return False
        ids = ",".join(str(t.get("id")) for t in picked)
        notify.post(self.gh, repo, ticket, DEFERRED_MARKER % (number, sha[:12], rounds, ids)
                    + "\nMedium or low review findings on #%d are deferred; the follow-up "
                    "issue is filed once it merges." % number, notify.RECORD)
        reply = ("Deferred: a medium or low finding still open after %d fix round(s), "
                 "with the review check passed on %s. A follow-up issue is filed once "
                 "this pull request merges." % (rounds, sha[:12]))
        for t in picked:
            self.gh.graphql(REPLY_MUTATION, {"id": str(t.get("id")), "body": reply})
            self.gh.graphql(RESOLVE_MUTATION, {"id": str(t.get("id"))})
        self.actions += 1
        _say("deferred %d finding(s) on %s#%d until it merges" % (len(picked), repo, number))
        return True

    def settle_deferred(self, repo: str, issue: Dict[str, Any]) -> None:
        """File the follow-up issue for findings deferred on a ticket's pull
        request, once that pull request has merged and only for the findings
        still standing on the merged head (a thread the later commits made
        outdated is gone). Once per pull request."""
        number = int(issue["number"])
        comments = self._comments(repo, number)
        text = "\n".join(str(c.get("body") or "") for c in comments)
        for pr, sha, rounds, ids in DEFERRED_RE.findall(text):
            pr_number = int(pr)
            if DEFERRED_FILED_MARKER % pr_number in text:
                continue
            data = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, pr_number)).data or {}
            if not data.get("merged_at") and not data.get("merged"):
                continue
            wanted = set(ids.split(","))
            standing = [t for t in self._threads(repo, pr_number)
                        if str(t.get("id")) in wanted and not t.get("isOutdated")]
            if standing:
                self._follow_up(repo, pr_number, number, sha, standing, int(rounds))
            notify.post(self.gh, repo, number, DEFERRED_FILED_MARKER % pr_number
                        + "\nDeferred findings of #%d: %d still standing on the merged head."
                        % (pr_number, len(standing)), notify.RECORD)
            self.actions += 1
            _say("settled the deferred findings of %s#%d (%d filed)"
                 % (repo, pr_number, len(standing)))

    def _close_unmerged_follow_ups(self, repo: str) -> None:
        """Close, as not planned, each open follow-up issue of a pull request
        that closed unmerged: its findings never landed."""
        for found in self.gh.paginate("/repos/%s/issues?labels=%s&state=open" % (repo, DEFER_LABEL)):
            if not isinstance(found, dict) or not self._trusted(found):
                continue
            title = str(found.get("title") or "")
            match = DEFER_TITLE_RE.match(title)
            if not match:
                continue
            pr = self.gh.rest("GET", "/repos/%s/pulls/%s" % (repo, match.group(1))).data or {}
            if pr.get("state") != "closed" or pr.get("merged_at") or pr.get("merged"):
                continue
            if self.actions >= MAX_ACTIONS:
                return
            body = str(found.get("body") or "").rstrip()
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, int(found["number"])), {
                "state": "closed", "state_reason": "not_planned",
                "body": body + "\n\nClosed: #%s closed without merging, so these findings "
                "never landed." % match.group(1)})
            self.actions += 1
            _say("closed %s#%s: pull request #%s closed unmerged"
                 % (repo, found["number"], match.group(1)))

    def _tried(self, issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
               failed_here: bool = True, footer: Optional[Tuple[str, str]] = None) -> Tuple[set, int]:
        """(the rungs the ticket has tried, the highest rung the ladder itself
        reached). Tried: those the ladder moved it to and it was claimed on,
        the one it failed on (`failed_here`), and every rung whose model one
        of its claims ran (ran_rungs). Only the ladder's own climbs make a
        lower rung a move down (climb_rungs)."""
        climbed = tried_rungs(comments)
        if failed_here:
            climbed |= {current_rung(issue)} - {0}
        return climbed | ran_rungs(issue, comments, footer), max(climbed, default=0)

    def _rung_lanes(self, repo: str, rung: int) -> Tuple[str, ...]:
        """The lanes of a CLIMB rung that may take this repo's work: a lane
        that takes public repos only never takes a private repo's."""
        lanes = CLIMB[rung - 1][0]
        if self._is_private(repo):
            lanes = tuple(lane for lane in lanes if lane not in PUBLIC_ONLY)
        return lanes

    def _up_lanes(self, repo: str) -> Dict[int, Tuple[str, ...]]:
        """Each CLIMB rung's lanes that may serve it now for this repo: up,
        not at a limit, and with no limit on the rung's model there."""
        return {n: tuple(lane for lane in self._rung_lanes(repo, n)
                         if self._up(lane) and self._serves(lane, CLIMB[n - 1][1]))
                for n in range(1, len(CLIMB) + 1)}

    def _rungs_left(self, repo: str, issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
                    footer: Optional[Tuple[str, str]] = None) -> List[int]:
        """The rungs the ticket may still climb to that have a lane for its
        repo, free or not; [] once every such rung was tried."""
        return [n for n in climb_rungs(*self._tried(issue, comments, footer=footer))
                if self._rung_lanes(repo, n)]

    def _climb(self, repo: str, issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
               why: str, head: str = "Repeatedly stuck", failed_here: bool = True,
               skip: int = 0, footer: Optional[Tuple[str, str]] = None) -> Optional[str]:
        """Put the ticket on its next rung (climb_rungs): its Model: line and
        lane label, and a rung marker. The rung it ran on counts as tried when
        it failed there. Returns the lane, or None when no rung it may climb
        to has a lane up: every such rung was tried (_rungs_left says which),
        or each is down or at a limit and the ticket waits."""
        number = int(issue["number"])
        if RUNAWAY in _labels(issue):
            return None  # held until a brain clears the label
        tried, reached = self._tried(issue, comments, failed_here, footer)
        up = self._up_lanes(repo)
        rung = next_rung(tried | ({skip} - {0}), self.load, {n for n in up if up[n]}, reached,
                         lanes=up)
        if not rung:
            return None
        lanes, model = up[rung], CLIMB[rung - 1][1]
        lane = pick_lane(lanes, self.load)
        if rung == len(LADDER):
            _say("%s#%d is on the last rung of the model ladder (%s on %s): one more "
                 "failure leaves it past the ladder and its lane pull request stalls"
                 % (repo, number, model, lane))
        body = with_model(str(issue.get("body") or ""), model)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
        kept = [x for x in _labels(issue) if not x.startswith("lane:")] + ["lane:" + lane]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]
        self._status_note(repo, number, "%s\n%s (%s): the factory moved this ticket up the "
                          "model ladder to rung %d, %s on lane %s." % (
                              RUNG_MARKER % rung, head, why, rung, model, lane))
        self.load[lane] = self.load.get(lane, 0) + 1
        return lane

    def _give_up(self, repo: str, issue: Dict[str, Any], why: str,
                 ran: Sequence[str] = ()) -> None:
        """Leave the ticket failed for the brain, naming the ladder models it
        ran on (`ran`). Only once _rungs_left is empty: every rung with a lane
        for its repo was tried, or is weaker than a strong model that was."""
        number = int(issue["number"])
        if RUNAWAY in _labels(issue):
            return
        if _state(issue) != "failed":
            self.move(self.gh, repo, number, "failed",
                      note=GAVE_UP + " (%s; ran on %s). "
                           "The brain rewrites this brief smaller, or asks the "
                           "operator one exact question." % (why, ", ".join(ran) or "none"))
        _say("%s#%d is past the last rung; left failed for the brain" % (repo, number))

    def _trusted_open(self, repo: str, pr: Dict[str, Any],
                      open_threads: Sequence[str]) -> List[Dict[str, Any]]:
        """The pull request's open threads that may count toward stuck
        (trusted_threads); read only when it has open threads at all."""
        if not open_threads:
            return []
        return trusted_threads(self._threads(repo, int(pr["number"])), self.review_bot,
                               self.trusted)

    def _hand_off_failed(self, repo: str, issue: Dict[str, Any]) -> None:
        """Put a ticket whose fix rounds stopped past the ladder in the
        hand-off pool (HANDOFF_LABEL), left loom:failed; _hand_off is the
        no-lane route, which leaves it queued."""
        names = _labels(issue)
        if HANDOFF_LABEL in names:
            return
        kept = names + [HANDOFF_LABEL]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, int(issue["number"])),
                     {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]

    def _close_past_ladder_pr(self, repo: str, pr: Dict[str, Any]) -> None:
        """Close an open lane pull request whose ticket is past the last rung of
        the model ladder, naming why, so the lane does not stall on it
        indefinitely. The brain re-briefs the work as a new loom:ticket."""
        number = int(pr["number"])
        comment = ("Closed by the station: its ticket is past the last rung of the "
                   "model ladder and is left loom:failed for the brain, which re-briefs "
                   "the work as a new loom:ticket.")
        self.close(self.gh, repo, number, comment)
        self.actions += 1
        _say("closed %s#%d: its ticket is past the model ladder" % (repo, number))

    def _send_back(self, repo: str, pr: Dict[str, Any], issue: Dict[str, Any],
                   comments: List[Dict[str, Any]], why: str,
                   open_threads: Sequence[str] = (), checks_failing: bool = False,
                   no_test: bool = False, unticked: Sequence[str] = (),
                   ticked: Sequence[str] = ()) -> None:
        number = int(issue["number"])
        held = SEND_BACK_HOLDS & set(_labels(issue))
        if held:
            # A person has it (no more rounds, no more comments), or the round
            # cap, a hold or a past-the-ladder give-up put it with the brain:
            # another round is a comment nobody asked for.
            _say("%s#%d is held (%s); not sent back for %s"
                 % (repo, number, ", ".join(sorted(held)), why))
            return
        if checks_failing and self._past_no_change(repo, issue, comments):
            return
        age = self.now - _ts(pr.get("created_at"))
        threads = self._trusted_open(repo, pr, open_threads)
        if stuck(comments, open_threads, checks_failing, age, self.now, no_test, unticked,
                 threads):
            footer = self._footer(repo, pr)
            if self._climb(repo, issue, comments, why, footer=footer) is None:
                if RUNAWAY in _labels(issue):
                    return
                if self._rungs_left(repo, issue, comments, footer):
                    # Not past the ladder: no lane of an untried rung is up.
                    # It waits, with no comment, and climbs on a later pass.
                    # An answered thread is judged by the review bot instead:
                    # it re-reviews the head with the reply in view.
                    asked = self._ask_reviewer(repo, pr)
                    note = ("; the review bot was asked to judge the answer on #%d"
                            % int(pr["number"])) if asked else ""
                    self._waiting(repo, number, "no lane is up for its next rung")
                    _say("%s#%d waits: no lane is up for its next rung%s"
                         % (repo, number, note))
                    return
                tried, _ = self._tried(issue, comments, footer=footer)
                self._close_past_ladder_pr(repo, pr)
                self._give_up(repo, issue, why, [CLIMB[n - 1][1] for n in sorted(tried)
                                                 if 1 <= n <= len(CLIMB)])
                self._hand_off_failed(repo, issue)
                return
        lane = None
        # A ticket with no lane label (a silent-lane requeue drops it, and the
        # run's pull request opens after; loom#1095, 2026-10-21) failed every
        # fix brief on "exactly one lane label": it moves like a remote one.
        if not _lane(issue) or _lane(issue) in REMOTE_LANES:
            lane, _ = self._tier_move(repo, issue, allow_remote=False)
            if lane is None:
                lane = LADDER[0][0][0]
        notes = TEST_NOTES if no_test else FIX_NOTES
        if unticked:
            try:
                # Criteria are ticket text: one the written-text check refuses
                # (a closing keyword, a path) must not stop the send-back.
                brain._check_written_text(unticked_notes(unticked))
                notes = unticked_notes(unticked)
            except ValueError as err:
                _say("%s#%d: unticked criteria not quoted in the fix note (%s)"
                     % (repo, number, err))
        if ticked:
            try:
                brain._check_written_text(ticked_notes(ticked))
                notes = ticked_notes(ticked)
            except ValueError as err:
                _say("%s#%d: ticked criteria not quoted in the fix note (%s)"
                     % (repo, number, err))
        if lane is not None:
            sent = self.fix(self.gh, repo, int(pr["number"]), (repo, number), notes, lane=lane)
        else:
            sent = self.fix(self.gh, repo, int(pr["number"]), (repo, number), notes)
        if sent is False:
            # brain.fix found nothing new to say and wrote nothing.
            _say("%s#%d not sent back for #%d (%s): the brief would add nothing"
                 % (repo, pr["number"], number, why))
            return
        self.actions += 1
        _say("sent %s#%d back for #%d (%s)" % (repo, pr["number"], number, why))

    def _past_no_change(self, repo: str, issue: Dict[str, Any],
                        comments: Sequence[Dict[str, Any]]) -> bool:
        """After NO_CHANGE_LIMIT fix rounds in a row that ended with no commit
        on one failing check, hand the ticket to a person with one comment
        naming the check (2026-10-19: 19 rounds, 19 emails, no fix). The
        loom:human label stops every later round and comment."""
        stalled = sorted(n for n, k in no_change_rounds(comments).items()
                         if k >= NO_CHANGE_LIMIT)
        if not stalled:
            return False
        number = int(issue["number"])
        names = ", ".join("`%s`" % n for n in stalled)
        self.human(self.gh, repo, number,
                   "Kind: decision\n%d fix rounds in a row made no change for the failing %s "
                   "%s, so another round would do the same. Decide whether the check is stale "
                   "or what the lane should change." % (NO_CHANGE_LIMIT, "check" if len(stalled) == 1
                                                         else "checks", names))
        self.actions += 1
        _say("%s#%d has gone to a person: no change for %s" % (repo, number, names))
        return True

    def _ask_reviewer(self, repo: str, pr: Dict[str, Any]) -> bool:
        """Ask the review bot to judge a lane's answer on a pull request whose
        ticket has no lane up for its next rung. It re-reviews the head with
        the reply in view: it clears the head if the answer holds, and keeps
        the finding if it does not. The ask happens once per head commit, so
        a ticket that waits does not ask again and again. True when it asked.
        """
        number = int(pr["number"])
        sha = str((pr.get("head") or {}).get("sha") or "")
        if not sha:
            return False
        marker = IMPROVE_MARKER % sha
        if any(marker in str(c.get("body") or "") for c in self._comments(repo, number)):
            return False  # this head was asked already
        if not answered_threads(self._threads(repo, number), self.trusted):
            return False  # nothing was answered: the finding stands on its own
        notify.post(self.gh, repo, number, "%s\n\n%s" % (review_again(), marker),
                    notify.REVIEW_REQUEST)
        self.actions += 1
        return True

    # -- the three kinds of stuck ----------------------------------------
    def _footer(self, repo: str, pr: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
        """The runner footer's (lane, model) of a trusted author's pull request
        whose head is in `repo`, else None: anyone can open a pull request
        that names a ticket, and its footer must never steer the ladder."""
        if (pr is None or not self._trusted(pr)
                or ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo):
            return None
        return footer_run(pr.get("body"))

    def _is_lane_pr(self, pr: Dict[str, Any]) -> bool:
        """A trusted, non-draft pull request on a loom/ branch."""
        if pr.get("draft") or not self._trusted(pr):
            return False
        return str((pr.get("head") or {}).get("ref") or "").startswith("loom/")

    def _merged_pull(self, repo: str, ticket_number: int) -> Optional[int]:
        """The newest pull request number that merged this ticket, or None."""
        if repo not in self._merged_prs:
            merged: Dict[int, int] = {}
            for pr in self.gh.paginate("/repos/%s/pulls?state=closed" % repo):
                if not isinstance(pr, dict):
                    continue
                if pr.get("merged_at") is None and not pr.get("merged"):
                    continue
                assoc = self._association(repo, pr)
                if assoc and assoc[0] == repo:
                    t_num = assoc[1]
                    if t_num not in merged or int(pr["number"]) > merged[t_num]:
                        merged[t_num] = int(pr["number"])
            self._merged_prs[repo] = merged
        return self._merged_prs[repo].get(ticket_number)

    def _close_pr(self, repo: str, pr: Dict[str, Any], superseded_by: int) -> None:
        """Close an open lane pull request and comment with the PR that superseded it."""
        number = int(pr["number"])
        comment = "Superseded by #%d." % superseded_by
        self.close(self.gh, repo, number, comment)
        self.actions += 1
        _say("closed %s#%d (superseded by #%d)" % (repo, number, superseded_by))

    def _close_all_but(self, repo: str, prs: Sequence[Dict[str, Any]], keep: int,
                       closed: set) -> None:
        """Close every pull request in prs except keep, naming keep in the comment."""
        for pr in prs:
            num = int(pr["number"])
            if num == keep:
                continue
            if self.actions >= MAX_ACTIONS:
                return
            self._close_pr(repo, pr, keep)
            closed.add(num)

    def _close_ticket_group(self, repo: str, item: Dict[str, Any],
                            ticket_map: Dict[int, Dict[str, Any]],
                            prs: Sequence[Dict[str, Any]], closed: set) -> None:
        """Close the superseded lane pull requests of one ticket (item names it)."""
        ticket_number = int(item["number"])
        ticket = ticket_map.get(ticket_number) or self._ticket(repo, ticket_number)
        if ticket and _state(ticket) in ("merged", "shipped", "verified"):
            merged = self._merged_pull(repo, ticket_number)
            if merged is not None:
                if "loom:regression" in _labels(ticket):
                    old = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, merged)).data
                    if not isinstance(old, dict) or not old.get("merged_at"):
                        return
                    events = self.gh.paginate("/repos/%s/issues/%d/events"
                                              % (repo, ticket_number))
                    if tickets.merged_before_regression_requeue(
                            ticket, old.get("merged_at"), events):
                        return
                self._close_all_but(repo, prs, merged, closed)
        elif len(prs) > 1:
            newest = max(prs, key=lambda p: (_ts(p.get("created_at")), int(p["number"])))
            self._close_all_but(repo, prs, int(newest["number"]), closed)

    def _close_superseded_prs(self, repo: str, open_prs: Sequence[Dict[str, Any]],
                              tickets: Dict[int, Dict[str, Any]]) -> set:
        """Close lane pull requests superseded by a newer one or a ticket merge."""
        lane_prs: Dict[int, List[Dict[str, Any]]] = {}
        for pr in open_prs:
            if not self._is_lane_pr(pr):
                continue
            assoc = self._association(repo, pr)
            if assoc and assoc[0] == repo:
                lane_prs.setdefault(assoc[1], []).append(pr)
        closed: set = set()
        for ticket_number, prs in lane_prs.items():
            if self.actions >= MAX_ACTIONS:
                break
            self._guarded(self._close_ticket_group, repo, {"number": ticket_number},
                          tickets, prs, closed)
        return closed

    def _lane_pr_ticket(self, repo: str, pr: Dict[str, Any],
                        tickets: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The loom:pr ticket of a trusted, non-draft lane pull request."""
        if not self._is_lane_pr(pr):
            return None
        association = self._association(repo, pr)
        issue = tickets.get(association[1]) if association else None
        return issue if issue is not None and _state(issue) == "pr" else None

    def _association(self, repo: str, pr: Dict[str, Any]) -> Optional[Tuple[str, int]]:
        try:
            return brain._ticket_association(repo, pr)
        except brain.OtherRepoTicket:
            # By design, not a refusal: an infra image-pin pull request ships
            # another repo's ticket. It has no ticket here; a line for it
            # every pass would bury the real refusals.
            return None
        except ValueError as err:
            _say("no ticket for %s#%s: %s" % (repo, pr.get("number"), err))
            return None

    def pull_request(self, repo: str, pr: Dict[str, Any],
                     tickets: Dict[int, Dict[str, Any]]) -> None:
        issue = self._lane_pr_ticket(repo, pr, tickets)
        if issue is None:
            return
        number = int(pr["number"])
        full = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data or {}
        sha = str((full.get("head") or {}).get("sha") or "")
        if not sha or full.get("mergeable") is None:
            return
        sensitive = class_of(full) == "sensitive"
        if sensitive:
            # A change to the code that merges, closes or deploys never lands
            # on the factory's say-so alone: it is marked for the brain, and
            # only its merge waits. A conflict, a failing check or a high
            # finding still goes back to its lane like any pull request
            # (2026-10-14: sensitive conflicts waited on hand-sent fix briefs).
            self._mark_sensitive(repo, number, full)
        review = self._review_state(repo, number, sha, _ts(full.get("updated_at")))
        if review == "wait":
            return
        comments = self._comments(repo, int(issue["number"]))
        ended = no_change_at(comments)
        if (ended and not self._reviewed_since(sha, ended)
                and self.now - ended <= REVIEW_GRACE):
            return  # the review the lane asked for has not answered yet
        self._judge(repo, number, full, issue, comments, review, sensitive)

    def _judge(self, repo: str, number: int, full: Dict[str, Any], issue: Dict[str, Any],
               comments: List[Dict[str, Any]], review: str, sensitive: bool = False) -> None:
        """Land, defer minor findings and land, rerun a flaky job, or send back.
        A sensitive pull request is judged the same way but never landed."""
        sha = str((full.get("head") or {}).get("sha") or "")
        self._resolve_bot_fixed(repo, number)
        details = brain._pr_graphql(self.gh, repo, number)
        runs = self._runs.get(sha, [])
        faults = pr_faults(details, full, runs)
        # Medium and low findings never send a pull request back on their own
        # (2026-10-14: 64% of 361 fix rounds were review threads, most of them
        # medium; "high-complexity" alone was 220 of ~1000 findings). They go
        # to the follow-up issue at once, for every class; high and critical
        # findings and failing checks still send it back.
        if (faults == ["open review threads"] and review == "reviewed"
                and self._defer_minor(repo, number, int(issue["number"]), sha, comments,
                                      True)):
            details = brain._pr_graphql(self.gh, repo, number)
            faults = pr_faults(details, full, runs)
        if faults == ["failing checks"] and (
                self._split_pr(repo, number, full, issue, comments, sha)
                or self._repair_body_leak(repo, number, full, issue, comments, sha)
                or self._repair_ticket_epic(repo, number, issue, comments, sha)
                or self._repair_plan_body(repo, number, full, issue, comments, sha)
                or self._run_again(repo, number, sha)
                or self._review_again(repo, number, full, sha)):
            return
        if faults:
            self._send_back(repo, full, issue, comments,
                            ", ".join(faults), open_thread_ids(details), "failing checks" in faults)
        elif review == "reviewed":
            self._land_or_ask_test(repo, number, full, issue, comments, sensitive)

    def _split_summary(self, issue: Dict[str, Any], sha: str) -> Optional[str]:
        """The review check's summary when it is the only failing check and
        failed on a note asking for a split (bus/split.py), else None."""
        if "loom:human" in _labels(issue):
            return None
        failed = [r for r in self._runs.get(sha, []) if r.get("conclusion") not in GREEN]
        if len(failed) != 1 or failed[0].get("name") != self.review_check:
            return None
        output = failed[0].get("output") or {}
        title, summary = str(output.get("title") or ""), str(output.get("summary") or "")
        return summary if split.asks_split(title, summary) else None

    def _file_parts(self, repo: str, number: int, issue: Dict[str, Any],
                    summary: str) -> Optional[List[int]]:
        """File the parts of a split as queued tickets and return their
        numbers, or None, with nothing filed, when the note leaves nothing to
        split on or the text of a part is refused by the written-text check."""
        ticket = int(issue["number"])
        body = str(issue.get("body") or "")
        criteria = acceptance_criteria(body)
        parts = split.plan(summary, criteria)
        if parts is None:
            _say("%s#%d: the review asks for a split but names nothing to split on"
                 % (repo, number))
            return None
        headers = split.header_lines(body)
        named = split.epic_of(body, ticket)
        placed = self._epic_with_room(repo, named, len(parts))
        epic = placed or named
        drafts = []
        for index, part in enumerate(parts, start=1):
            name = "%s (part %d of %d)" % (str(issue.get("title") or "").strip(),
                                           index, len(parts))
            drafts.append((name[:100], split.ticket_body(
                ticket, number, headers, part, index, len(parts), epic, criteria)))
        try:
            for name, text in drafts:
                brain._check_written_text(name + "\n" + text)
        except ValueError as err:
            # Ticket text the written-text check refuses must not stop the pass,
            # nor leave half the parts filed; the usual fix round handles it.
            _say("%s#%d: split ticket text refused (%s)" % (repo, number, err))
            return None
        numbers = []
        for name, text in drafts:
            made = self.gh.rest("POST", "/repos/%s/issues" % repo, {
                "title": name, "body": text, "labels": ["loom:ticket", "loom:queued"]}).data or {}
            if placed:
                self._link_filed(repo, placed, made, text)
            numbers.append(int(made["number"]))
        return numbers

    def _epic_with_room(self, repo: str, named: int, room: int) -> Optional[int]:
        """The epic a ticket filed beside issue `named` belongs under: that
        epic while it is open with room for `room` more tickets, else the
        goal's next epic (loom#951, loom#1294). None when no goal can be
        found: the filing still goes ahead, under the issue it names."""
        try:
            return brain.placement_epic(self.gh, repo, named, room)
        except (GitHubError, ValueError) as err:
            _say("%s: no epic with room beside #%d, filing under it: %s" % (repo, named, err))
            return None

    def _link_filed(self, repo: str, epic: int, made: Dict[str, Any], text: str) -> None:
        """Link a ticket the factory just filed as the epic's sub-issue, best
        effort: its Part of line already names the epic."""
        try:
            brain.file_under_epic(self.gh, repo, epic, dict(made, body=text))
        except GitHubError as err:
            _say("%s#%s: not linked to epic #%d: %s" % (repo, made.get("number"), epic, err))

    def _split_pr(self, repo: str, number: int, full: Dict[str, Any],
                  issue: Dict[str, Any], comments: List[Dict[str, Any]], sha: str) -> bool:
        """When the review check is the only failing check and failed on a note
        asking for the pull request to be split, file the parts as tickets and
        close the pull request with a pointer, instead of another fix round
        that cannot split it (loom#1218). Any other failing check or finding
        goes back as before. False leaves the pull request to the usual
        paths, as does a note that leaves nothing to split on."""
        summary = self._split_summary(issue, sha)
        if summary is None:
            return False
        ticket = int(issue["number"])
        marker = split.SPLIT_MARKER % number
        done = next((str(c.get("body") or "") for c in comments
                     if marker in str(c.get("body") or "")), None)
        if done is None:
            filed = self._file_parts(repo, number, issue, summary)
            if filed is None:
                return False
            done = ("%s\nPull request #%d failed the review check only on a note asking for it "
                    "to be split, and no fix round can split it, so it is closed and this "
                    "ticket is split into %s." % (
                        marker, number, ", ".join("#%d" % n for n in filed)))
            notify.post(self.gh, repo, ticket, done, notify.NOTE)
        names = ", ".join(re.findall(r"#\d+", done.partition("split into")[2])) or "new tickets"
        self.close(self.gh, repo, number,
                   "Closed by the station: the review asked for this change to be split and "
                   "no fix round can do it. The work continues as %s." % names)
        self._relabel(repo, issue, brain._moved_labels(_labels(issue), "closed-unmerged", None))
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, ticket),
                     {"state": "closed", "state_reason": "not_planned"})
        self.actions += 1
        _say("split %s#%d into %s and closed it" % (repo, number, names))
        return True

    def _repair_body_leak(self, repo: str, number: int, full: Dict[str, Any],
                          issue: Dict[str, Any], comments: List[Dict[str, Any]],
                          sha: str) -> bool:
        """Rewrite the flagged lines of the pull request's text when every
        failing check is a leak scan that failed on that text alone; once per
        head commit. False leaves the pull request to the usual paths."""
        marker = BODY_LEAK_MARKER % sha
        if any(marker in str(c.get("body") or "") for c in comments):
            return False
        failed = [r for r in self._runs.get(sha, []) if r.get("conclusion") not in GREEN]
        if not failed or not all(LEAK_CHECK_RE.search(str(r.get("name") or ""))
                                 and (r.get("app") or {}).get("slug") == "github-actions"
                                 for r in failed):
            return False
        hits: Dict[int, str] = {}
        for run in failed:
            try:
                log = brain._job_log(self.gh, repo, int(run["id"]))
            except (GitHubError, KeyError, TypeError, ValueError):
                return False
            found = body_leak_hits(log or [])
            if found is None:
                return False
            hits.update(found)
        fixed = repair_leak_text(str(full.get("title") or ""), str(full.get("body") or ""),
                                 hits, repo)
        if fixed is None:
            return False
        title, body, changed = fixed
        self.gh.rest("PATCH", "/repos/%s/pulls/%d" % (repo, number),
                     {"title": title, "body": body})
        notify.post(self.gh, repo, int(issue["number"]),
                    "%s\nThe leak scan on #%d failed on its title and body only, "
                    "which the runner writes from this ticket, so the factory "
                    "edited the text instead of sending it back:\n%s"
                    % (marker, number, "\n".join("- " + c for c in changed)),
                    notify.REPAIR)
        self.actions += 1
        _say("rewrote %d flagged line(s) of %s#%d's text after a body-only leak scan"
             % (len(changed), repo, number))
        return True

    def _repair_ticket_epic(self, repo: str, number: int, issue: Dict[str, Any],
                            comments: List[Dict[str, Any]], sha: str) -> bool:
        """Put the ticket under an epic when the plan check fails only on its
        `which epic` row for this pull request's own ticket. The missing epic
        is on the ticket, which a lane cannot edit, so a fix round would loop
        with nothing to change. Once per head commit; after that the pull
        request waits for the plan check to read the ticket again, still with
        no lane round. True whenever the failure is that row alone, so the
        usual paths never see it."""
        failed = [r for r in self._runs.get(sha, []) if r.get("conclusion") not in GREEN]
        if len(failed) != 1 or failed[0].get("name") != self.plan_check:
            return False
        ticket = int(issue["number"])
        if chief_orphans(str((failed[0].get("output") or {}).get("summary") or "")) != [ticket]:
            return False
        marker = EPIC_REPAIR_MARKER % sha
        if any(marker in str(c.get("body") or "") for c in comments):
            _say("%s#%d waits: its ticket #%d was put under an epic and %s has not read it again"
                 % (repo, number, ticket, self.plan_check))
            return True
        try:
            epic = int(self.place(self.gh, repo, issue))
        except (GitHubError, ValueError) as err:
            _say("%s#%d: ticket #%d could not be put under an epic (%s); held, no lane round"
                 % (repo, number, ticket, err))
            return True
        notify.post(self.gh, repo, ticket,
                    "%s\n%s failed on pull request #%d only because this ticket belonged to no "
                    "epic, which a lane cannot fix, so the factory put it under epic #%d instead "
                    "of sending the pull request back." % (marker, self.plan_check, number, epic),
                    notify.REPAIR)
        self.actions += 1
        _say("put %s#%d under epic #%d after %s held #%d" % (repo, ticket, epic, self.plan_check, number))
        return True

    def _repair_plan_body(self, repo: str, number: int, full: Dict[str, Any],
                          issue: Dict[str, Any], comments: List[Dict[str, Any]],
                          sha: str) -> bool:
        """When the plan check is the only failing check and the body fails
        it (bus/hunt_plan.problems), rebuild the failing parts from the
        ticket with bus/pr_repair.py and edit the body, which makes the review
        bot read it again, instead of a fix round that runs an agent on code
        that cannot change the body (loom#1837). Once per head commit, kept
        on the ticket's status comment, which sends no email. When the ticket
        itself lacks the parts, the ticket is held for the brain with one
        note, edited in place, naming them; no lane round is sent. False
        leaves the pull request to the usual paths: another failing check, a
        body that passes (the check failed on something else), a body with no
        runner footer to rebuild from, or a repair the text checks refuse."""
        failed = [r for r in self._runs.get(sha, []) if r.get("conclusion") not in GREEN]
        if len(failed) != 1 or failed[0].get("name") != self.plan_check:
            return False
        body = str(full.get("body") or "")
        ticket = int(issue["number"])
        marker = PLAN_REPAIR_MARKER % sha
        repaired_at = max((_ts(c.get("created_at")) for c in comments
                           if marker in str(c.get("body") or "")), default=0.0)
        if repaired_at:
            if (_ts(failed[0].get("completed_at")) < repaired_at
                    and self.now - repaired_at <= REVIEW_GRACE):
                _say("%s#%d waits: its body was rebuilt from ticket #%d and %s has not read "
                     "it again" % (repo, number, ticket, self.plan_check))
                return True
            return False  # read again and still failing: the usual paths
        if not hunt_plan.problems(body):
            return False
        fields = pr_repair.footer_fields(body)
        if fields is None:
            return False
        name, lane, model = fields
        found = pr_repair.plan_repair(tickets.render_brief(repo, issue, lane),
                                      str(full.get("title") or ""), body, lane, model, name,
                                      repo=repo, public=not self._is_private(repo))
        if found["reason"]:
            _say("%s#%d: plan body repair refused (%s)" % (repo, number, found["reason"]))
            return False
        if found["changed"]:
            self.gh.rest("PATCH", "/repos/%s/pulls/%d" % (repo, number), {"body": found["body"]})
            self._status_note(repo, ticket, "%s\n%s failed on pull request #%d only on its "
                              "body (%s), which the runner writes from this ticket, so the "
                              "factory rebuilt it from the ticket instead of sending a lane "
                              "round." % (marker, self.plan_check, number,
                                          "; ".join(found["before"])))  # type: ignore[arg-type]
            self._lift_plan_hold(repo, number, issue, comments)
            self.actions += 1
            _say("rebuilt %s#%d's body from ticket #%d after a body-only %s failure"
                 % (repo, number, ticket, self.plan_check))
            return True
        self._hold_plan(repo, number, issue, comments,
                        pr_repair.ticket_gaps(list(found["after"])))  # type: ignore[arg-type]
        return True

    @staticmethod
    def _plan_note(comments: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The ticket's plan-hold note (bus/pr_repair.PLAN_NOTE_MARKER), a
        real comment the runner or the factory wrote, or None."""
        return next((c for c in comments if c.get("id") and not c.get(status.EXPANDED)
                     and str(c.get("body") or "").startswith(pr_repair.PLAN_NOTE_MARKER)), None)

    def _hold_plan(self, repo: str, number: int, issue: Dict[str, Any],
                   comments: Sequence[Dict[str, Any]], gaps: List[str]) -> None:
        """Hold the ticket for the brain: needs-brain, and the one note naming
        what the ticket lacks, posted once and edited only when it changes.
        The note records whether this hold added needs-brain, so lifting it
        never takes off a label set for another reason."""
        ticket = int(issue["number"])
        note = self._plan_note(comments)
        set_label = bool(note is not None and pr_repair.hold_set_label(str(note.get("body") or "")))
        wrote = False
        if "needs-brain" not in _labels(issue):
            self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, ticket),
                         {"labels": ["needs-brain"]})
            issue["labels"] = list(issue.get("labels") or []) + [{"name": "needs-brain"}]
            set_label = wrote = True
        text = pr_repair.hold_note(number, self.plan_check, gaps, set_label)
        if note is None:
            notify.post(self.gh, repo, ticket, text, notify.HELD)
            wrote = True
        elif str(note.get("body") or "").strip() != text.strip():
            self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(note["id"])),
                         {"body": text})
            wrote = True
        if wrote:
            self.actions += 1
            _say("held %s#%d for the brain: its body fails %s and ticket #%d lacks %s"
                 % (repo, number, self.plan_check, ticket, "; ".join(gaps)))

    def _lift_plan_hold(self, repo: str, number: int, issue: Dict[str, Any],
                        comments: Sequence[Dict[str, Any]]) -> None:
        """After a repair: say in the plan-hold note that the hold is lifted,
        and take needs-brain off only when that note records the hold added
        it (pr_repair.PLAN_SET_LABEL); a label set for another reason stays."""
        note = self._plan_note(comments)
        if note is None or str(note.get("body") or "").strip() == pr_repair.lifted_note(number):
            return
        if (pr_repair.hold_set_label(str(note.get("body") or ""))
                and "needs-brain" in _labels(issue)):
            self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/needs-brain" % (repo, int(issue["number"])),
                         ok=(404,))
            issue["labels"] = [x for x in issue.get("labels") or []
                               if (x.get("name") if isinstance(x, dict) else x) != "needs-brain"]
        self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(note["id"])),
                     {"body": pr_repair.lifted_note(number)})

    def _land_or_ask_test(self, repo: str, number: int, full: Dict[str, Any],
                          issue: Dict[str, Any], comments: List[Dict[str, Any]],
                          sensitive: bool = False) -> None:
        """Land a clean, reviewed pull request, or send it back when it
        changes code without a test. A clean sensitive one waits for the brain."""
        missing = missing_criteria(str(issue.get("body") or ""), str(full.get("body") or ""))
        ticked = criteria_gate.lane_ticks(str(issue.get("body") or ""), str(full.get("body") or ""))
        if ticked:
            # Only the station's verifier or the operator ticks a live or
            # operator criterion (bus/criteria_gate.py): a lane's tick on one
            # read as evidence it had been checked (2026-10-22 audit).
            self._send_back(repo, full, issue, comments,
                            "a live or operator criterion is ticked; only the verifier "
                            "ticks it: " + "; ".join(ticked), ticked=ticked)
        elif missing:
            self._send_back(repo, full, issue, comments,
                            "unticked acceptance criteria; build these or mark each "
                            "not built with a reason: " + "; ".join(missing),
                            unticked=missing)
        elif self._needs_test(repo, number, issue):
            self._send_back(repo, full, issue, comments,
                            "code changed without a test", no_test=True)
        elif (not sensitive and not self._operator_holds(repo, number, full, issue, comments)
              and not self._risk_holds(repo, number, full, issue, comments)):
            self._file_criterion_follow_up(repo, number, issue, full)
            self._land(repo, number, full, issue, comments)

    def _operator_holds(self, repo: str, number: int, full: Dict[str, Any],
                        issue: Dict[str, Any], comments: Sequence[Dict[str, Any]] = ()) -> bool:
        """Whether the pull request waits for the operator instead of landing
        (bus/operator_hold.py): it or its ticket declares operator steps
        before merge, it changes placement, or it deletes or mostly replaces
        a doc its ticket does not name. The ticket goes
        to loom:human with one exact ask, once; the pull request never lands
        on the factory's say-so, whatever later passes find. The operator's
        own answer to that ask (bus/answer.py) decides instead: Merge lifts
        this hold only, so every other gate still runs; Hold keeps it held
        and asks nothing more."""
        why = operator_hold.reasons(self.gh, repo, full, str(issue.get("body") or ""),
                                    operator_hold.pull_files(self.gh, repo, number))
        if not why:
            return False
        settled = self._operator_verdict(repo, number, full, issue, comments)
        if settled is not None:
            return settled
        outcome = operator_hold.hold(self.gh, repo, int(issue["number"]), number, why,
                                     self.human)
        if outcome == "asked":
            self.actions += 1
            _say("held %s#%d for the operator: %s" % (repo, number, "; ".join(why)))
        elif outcome != "already":
            _say("held %s#%d for the operator, but %s" % (repo, number, outcome))
        return True

    def _risk_holds(self, repo: str, number: int, full: Dict[str, Any],
                    issue: Dict[str, Any], comments: Sequence[Dict[str, Any]] = ()) -> bool:
        """Whether the pull request's risk tier (bus/merge_tier.py) is one
        Loom may not merge on its own: its ticket goes to loom:human with one
        ask, once, and the operator merges it. A revert ticket's own pull
        request never waits here when merge_tier.revert_refusal proves it. bus.brain merge refuses the same tiers, and
        adds the review and hourly-cap rules for the tiers it may merge."""
        try:
            entries = operator_hold.pull_files(self.gh, repo, number)
        except GitHubError as err:
            # No file list, no tier: it waits for the next pass, unmerged.
            _say("%s#%d waits: its changed files could not be read for the risk tier (%s)"
                 % (repo, number, err))
            return True
        if merge_tier.is_revert_ticket(full, issue):
            refused = merge_tier.revert_refusal(self.gh, repo, full, issue, entries)
            if not refused:
                return False
            _say("%s#%d is not exempt as a revert: %s" % (repo, number, refused))
        tier, why = merge_tier.classify(repo, entries, str(issue.get("body") or ""),
                                        change_class.class_of(full) == "sensitive")
        if tier in merge_tier.auto_tiers():
            return False
        settled = self._operator_verdict(repo, number, full, issue, comments)
        if settled is not None:
            return settled
        reason = merge_tier.operator_why(tier, why)
        outcome = operator_hold.hold(self.gh, repo, int(issue["number"]), number, [reason],
                                     self.human)
        if outcome == "asked":
            self.actions += 1
            _say("held %s#%d for the operator: %s" % (repo, number, reason))
        elif outcome != "already":
            _say("held %s#%d for the operator, but %s" % (repo, number, outcome))
        return True

    def _operator_verdict(self, repo: str, number: int, full: Dict[str, Any],
                          issue: Dict[str, Any],
                          comments: Sequence[Dict[str, Any]]) -> Optional[bool]:
        """The operator's answer to the hold ask on this pull request
        (answer.pr_verdict), as the hold's outcome: False (Merge: the hold is
        lifted and the other gates decide), True (Hold: held, no new ask), or
        None (no answer: the hold asks as before). Each verdict is written to
        the shadow ledger as the operator's."""
        verdict = answer.pr_verdict(list(comments), number, self.trusted, self.operators)
        if verdict is None:
            return None
        sha = str((full.get("head") or {}).get("sha") or "")
        answer.record_verdict(repo, number, verdict, sha, (repo, int(issue["number"])),
                              source="bus.factory")
        if answer.is_merge(verdict):
            _say("%s#%d: the operator %s answered merge; its operator hold is lifted and its "
                 "other gates decide" % (repo, number, verdict.login))
            return False
        _say("%s#%d stays held: the operator %s answered hold" % (repo, number, verdict.login))
        return True

    def _settle_answers(self, repo: str) -> None:
        """Read each answer the dashboard posted (an open issue labelled
        loom:answer-pending, bus/answer.py) and take the label off: the
        answer is now the operator's newest comment, which the brief, the
        stale-brief check and the merge gates read. A Hold on a lane pull
        request held for the operator puts loom:human back, so it keeps
        waiting on a person. An issue whose label no operator answer backs
        loses the label too."""
        items = self.gh.paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
                                 % (repo, quote(answer.ANSWERED_LABEL, safe="")))
        for issue in [i for i in items if isinstance(i, dict)][:ANSWERS_PER_PASS]:
            if self.actions >= MAX_ACTIONS:
                return
            number = int(issue.get("number") or 0)
            comments = self._comments(repo, number)
            found = answer.newest(comments, self.trusted, self.operators)
            if found is None:
                _say("%s#%d is labelled %s, but no operator answer backs it; label removed"
                     % (repo, number, answer.ANSWERED_LABEL))
            elif answer.is_hold(found) and answer.held_pr(_last_ask(comments)) is not None:
                if "loom:human" not in _labels(issue):
                    self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                                 {"labels": ["loom:human"]})
                _say("%s#%d: the operator %s answered hold; it keeps waiting on a person"
                     % (repo, number, found.login))
            else:
                _say("%s#%d: read the operator %s's answer (%s)"
                     % (repo, number, found.login, found.choice))
            self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                         % (repo, number, quote(answer.ANSWERED_LABEL, safe="")), ok=(404,))
            self.actions += 1

    def _file_criterion_follow_up(self, repo: str, number: int,
                                  issue: Dict[str, Any], full: Dict[str, Any]) -> None:
        """File a ticket for criteria explicitly marked not built in a lane PR."""
        criteria = not_built_criteria(str(issue.get("body") or ""),
                                      str(full.get("body") or ""))
        if not criteria:
            return
        title = "Complete criteria left from PR #%d" % number
        if any(isinstance(found, dict) and found.get("title") == title
               for found in self.gh.paginate("/repos/%s/issues?labels=loom:ticket" % repo)):
            return
        number = int(issue["number"])
        named = split.epic_of(str(issue.get("body") or ""), number)
        placed = self._epic_with_room(repo, named, 1)
        epic = placed or named
        body = ("Difficulty: medium\n\n## Why\nThe lane pull request marked these ticket criteria as not built.\n\n"
                "## What\nComplete the remaining acceptance criteria.\n\n## Acceptance criteria\n" +
                "\n".join("- [ ] " + item for item in criteria) +
                "\n\n## Size\nS\n\n## Out of scope\n- None\n\nPart of #%d\n" % epic)
        made = self.gh.rest("POST", "/repos/%s/issues" % repo, {
            "title": title, "body": body, "labels": ["loom:ticket", "loom:queued"]}).data or {}
        if placed:
            self._link_filed(repo, placed, made, body)

    def _run_again(self, repo: str, number: int, sha: str) -> bool:
        """Re-run the failed Actions jobs on this commit once. False when a
        review check failed (its findings are real), when a failed check is
        not an Actions job, or when the jobs already had their second run."""
        failed = [r for r in self._runs.get(sha, [])
                  if r.get("conclusion") in ("failure", "timed_out", "cancelled")]
        if not failed or any(r.get("name") == self.review_check for r in failed):
            return False
        ids = set()
        for run in failed:
            m = ACTIONS_RUN_RE.search(str(run.get("details_url") or ""))
            if not m:
                return False
            ids.add(m.group(1))
        fresh = [i for i in sorted(ids) if int((self.gh.rest(
            "GET", "/repos/%s/actions/runs/%s" % (repo, i)).data or {}).get("run_attempt") or 1) < 2]
        if len(fresh) != len(ids):
            return False
        for run_id in fresh:
            self.gh.rest("POST", "/repos/%s/actions/runs/%s/rerun-failed-jobs" % (repo, run_id))
        self.actions += 1
        _say("re-ran failed jobs on %s#%d once before sending it back" % (repo, number))
        return True

    def _review_again(self, repo: str, number: int, full: Dict[str, Any],
                      sha: str) -> bool:
        """Ask the review bot to look at this head again, once per head
        commit, when the only failing check is a partial review a lane cannot
        fix by editing code. False when a marker for this head is already on
        the pull request (a second partial result goes to the usual paths)."""
        failed = [r for r in self._runs.get(sha, [])
                  if r.get("conclusion") not in GREEN]
        review = [r for r in failed if r.get("name") == self.review_check]
        if len(review) != 1 or len(failed) != 1:
            return False
        output = review[0].get("output") or {}
        if not degraded_review(str(output.get("title") or ""),
                               str(output.get("summary") or "")):
            return False
        marker = REVIEW_AGAIN_MARKER % sha
        for comment in self.gh.paginate(
                "/repos/%s/issues/%d/comments" % (repo, number)):
            if marker in str(comment.get("body") or ""):
                return False
        notify.post(self.gh, repo, number, "%s\n%s" % (marker, REVIEW_AGAIN),
                    notify.REVIEW_REQUEST)
        self.actions += 1
        _say("asked the review bot to review %s#%d again after a partial review"
             % (repo, number))
        return True

    def _behind(self, repo: str, full: Dict[str, Any]) -> int:
        base = str((full.get("base") or {}).get("ref") or "")
        head = str((full.get("head") or {}).get("sha") or "")
        data = self.gh.rest("GET", "/repos/%s/compare/%s...%s"
                            % (repo, quote(base, safe=""), head)).data or {}
        return int(data.get("behind_by") or 0)

    def _land(self, repo: str, number: int, full: Dict[str, Any],
              issue: Dict[str, Any], comments: List[Dict[str, Any]]) -> None:
        """Merge a ready pull request that is up to date with its base, one a
        repo a pass; bring one that is behind up to date instead. A pull
        request that cannot be brought up to date cleanly goes back for a
        fix round, as a conflict does."""
        if repo in self.landed:
            return
        if self._behind(repo, full):
            if repo in self.updated:
                return
            self.updated.add(repo)
            sha = str((full.get("head") or {}).get("sha") or "")
            done = self.gh.rest("PUT", "/repos/%s/pulls/%d/update-branch" % (repo, number),
                                {"expected_head_sha": sha}, ok=(422,))
            if done.status == 422:
                self._send_back(repo, full, issue, comments,
                                "could not be brought up to date with its base")
                # keep repo in updated to retry on next pass
                return
            # reset updated for future merges
            self.updated.remove(repo)
            self.actions += 1
            _say("brought %s#%d up to date with its base; it lands once its checks "
                 "and review pass on the new head" % (repo, number))
            return
        if self._scope_holds(repo, number, full, issue):
            return
        if self._merge(repo, number, issue, str((full.get("head") or {}).get("sha") or "")):
            self.landed.add(repo)

    def _scope_holds(self, repo: str, number: int, full: Dict[str, Any],
                     issue: Dict[str, Any]) -> bool:
        """Whether a cheap model says the pull request's files do not fit its
        ticket, so it is held for the brain instead of landing. Advisory: no
        route, no answer or an unreadable one lets it land. Asked only of a
        public repo (a private repo's text stays off a free route), once per
        head commit: a hold leaves a marker comment that later passes read
        instead of asking again."""
        if self.scope is None or self._is_private(repo):
            return False
        sha = str((full.get("head") or {}).get("sha") or "")
        marker = SCOPE_HOLD_MARKER % sha
        if any(marker in str(c.get("body") or "") for c in self._comments(repo, number)):
            return True
        verdict = scope_fit.judge(self.scope, str(issue.get("body") or ""),
                                  self._files(repo, number))
        if verdict.kind == scope_fit.UNKNOWN:
            _say("scope check of %s#%d gave no answer (%s); it lands as before"
                 % (repo, number, verdict.reason))
            return False
        if verdict.kind == scope_fit.FITS:
            return False
        notify.post(self.gh, repo, number, (
            "%s\nHeld for the brain: a scope check asked whether this pull request's "
            "changed files fit the ticket's What section and the repos and resources "
            "it names, and the answer was no: %s\nThe check is advisory. The brain or "
            "a person reads the diff against the ticket and merges it, or sends it "
            "back." % (marker, verdict.reason)), notify.HELD)
        if "needs-brain" not in _labels(full):
            self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                         {"labels": ["needs-brain"]})
        self._status_note(repo, int(issue["number"]), (
            "Pull request #%d is held for the brain: its changed files may not fit this "
            "ticket (%s)." % (number, verdict.reason)))
        self.actions += 1
        _say("held %s#%d for the brain: its files may not fit the ticket (%s)"
             % (repo, number, verdict.reason))
        return True

    def _merge(self, repo: str, number: int, issue: Dict[str, Any], sha: str = "") -> bool:
        ticket = (repo, int(issue["number"]))
        try:
            try:
                self.merge(self.gh, repo, number, ticket)
            except GitHubError as err:
                if err.status != 405 or not self._dismiss_bot_change_requests(repo, number, sha):
                    raise
                self.merge(self.gh, repo, number, ticket)
        except (ValueError, GitHubError) as err:
            _say("did not merge %s#%d: %s" % (repo, number, err))
            return False
        self.actions += 1
        _say("merged %s#%d for #%d" % (repo, number, issue["number"]))
        return True

    def _dismiss_bot_change_requests(self, repo: str, number: int, sha: str) -> bool:
        """Dismiss the review bot's change requests that its own review check
        superseded, and say whether any was: each one submitted before that
        check last passed on the head commit `sha` (only a ready pull request
        reaches _merge). The review bot leaves the change request of a failing
        review in place after its own later review of the same head passes,
        and the merge was refused every pass (2026-10-21). A person's change
        request, another bot's, and one newer than the passing check stay."""
        passed = [_ts(r.get("completed_at")) for r in self._runs.get(sha, [])
                  if r.get("name") == self.review_check and r.get("conclusion") in GREEN]
        if not sha or not passed:
            return False
        cleared = max(passed)
        bot = {self.review_bot.lower(), self.review_bot.lower() + "[bot]"}
        dismissed = 0
        for review in self.gh.paginate("/repos/%s/pulls/%d/reviews" % (repo, number)):
            user = review.get("user") or {}
            if (review.get("state") != "CHANGES_REQUESTED" or user.get("type") != "Bot"
                    or str(user.get("login") or "").lower() not in bot
                    or _ts(review.get("submitted_at")) >= cleared):
                continue
            self.gh.rest("PUT", "/repos/%s/pulls/%d/reviews/%d/dismissals"
                         % (repo, number, int(review["id"])),
                         {"event": "DISMISS", "message":
                          "Superseded: %s passed on this head commit after this review. "
                          "The factory dismisses the review bot's change request that "
                          "its own later review cleared." % self.review_check})
            dismissed += 1
        if dismissed:
            self.actions += 1
            _say("dismissed %d superseded review-bot change request(s) on %s#%d"
                 % (dismissed, repo, number))
        return bool(dismissed)

    def failed(self, repo: str, issue: Dict[str, Any],
               prs: Dict[int, Dict[str, Any]]) -> None:
        number = int(issue["number"])
        comments = self._comments(repo, number)
        if repeated_no_reason(comments):
            self._hold_no_reason(repo, issue)
            return
        other = needs_other_repo(comments)
        if other:
            self._hold_other_repo(repo, issue, other)
            return
        pr = prs.get(number)
        if self._not_work(repo, issue, comments, pr):
            return
        footer = self._footer(repo, pr)
        lane = self._climb(repo, issue, comments, "it failed on lane %s" % _lane(issue),
                           footer=footer)
        if lane is None:
            if self._rungs_left(repo, issue, comments, footer):
                return  # waits: no lane of an untried rung is up yet
            # Past the last rung of the model ladder: leave the ticket failed
            # for the brain, and close its open lane pull request so the lane
            # does not stall on it indefinitely. The brain re-briefs the work
            # as a new loom:ticket.
            if pr is not None:
                self._close_past_ladder_pr(repo, pr)
            self._give_up(repo, issue, "it failed on lane %s" % _lane(issue))
            return
        if pr is not None:
            # Back to loom:pr so the fix brief reuses its open pull request.
            kept = [x for x in _labels(issue) if x != "loom:failed"] + ["loom:pr"]
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
            self.fix(self.gh, repo, int(pr["number"]), (repo, number), FAILED_RUN_NOTES)
        else:
            self.move(self.gh, repo, number, "briefed", lane=lane)
        self.actions += 1
        _say("re-briefed failed %s#%d on %s" % (repo, number, lane))

    @staticmethod
    def _gave_up_since_claim(comments: Sequence[Dict[str, Any]]) -> bool:
        """Whether _give_up's note came after the ticket's newest claim."""
        gave_up = False
        for c in comments:
            body = str(c.get("body") or "")
            if CLAIMED_RE.fullmatch(body.strip()):
                gave_up = False
            elif GAVE_UP in body:
                gave_up = True
        return gave_up

    def _not_work(self, repo: str, issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
                  pr: Optional[Dict[str, Any]]) -> bool:
        """A failed ticket whose newest claim ended [transient], [limit],
        [factory] or [auth] did no work for a stronger model to redo: it
        climbs nothing, and goes back where it was on its current rung and
        model, to loom:pr when it has a pull request, else to the queue.
        Of 227 climbs since 2026-10-14, 43 followed such an ending
        (loom#1395). True when it moved the ticket. A runaway or
        needs-brain ticket stays held for the brain."""
        klass = newest_ending_class(comments)
        names = _labels(issue)
        if klass == "work" or RUNAWAY in names or "needs-brain" in names:
            return False
        if self._gave_up_since_claim(comments):
            # _give_up left it failed past the ladder: moving it back would
            # only have the next pass fail it again.
            return False
        number = int(issue["number"])
        if pr is not None:
            kept = [x for x in names if x != "loom:failed"] + ["loom:pr"]
            where = "back to loom:pr; its pull request is unchanged"
        else:
            kept = [x for x in brain._moved_labels(names, "queued", None)
                    if not x.startswith("lane:")]
            where = "back in the queue"
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]
        self._status_note(repo, number,
                          "Not a failure (the newest run ended [%s]): the lane did no work on "
                          "this ticket, so the factory did not climb the model ladder. The "
                          "ticket is %s, on its current rung." % (klass, where))
        self.actions += 1
        _say("%s#%d failed with a [%s] ending; %s, no rung spent" % (repo, number, klass, where))
        return True

    def _hold_no_reason(self, repo: str, issue: Dict[str, Any]) -> None:
        """Leave a ticket failed for the brain instead of climbing a second
        time for a run that committed nothing and said nothing, once
        (needs-brain says it was reported)."""
        names = _labels(issue)
        if "needs-brain" in names:
            return
        number = int(issue["number"])
        notify.post(self.gh, repo, number,
                    "Lane %s made no commit and wrote no reason, as a run on this ticket did "
                    "before. The factory climbed the model ladder once for that and does not "
                    "climb a second time for the same reason: the brain reads the run logs and "
                    "rewrites the brief, closes the ticket, or escalates it once more with its "
                    "reason." % _lane(issue), notify.HELD)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": names + ["needs-brain"]})
        self.actions += 1
        _say("%s#%d failed again with no commit and no reason; left for the brain"
             % (repo, number))

    def _hold_other_repo(self, repo: str, issue: Dict[str, Any], submodule: str) -> None:
        """Leave a ticket failed for the brain to split, once (needs-brain says
        it was reported), when its change needed files inside a submodule."""
        names = _labels(issue)
        if "needs-brain" in names:
            return
        number = int(issue["number"])
        self._status_note(repo, number, (
            "Not climbing the model ladder: lane %s's change needed files inside "
            "submodule %s, which a push of this repo cannot carry, and a stronger model "
            "cannot change that. The brain splits the ticket: the %s part as a ticket "
            "in that repo, and an After: line here naming it." % (_lane(issue), submodule, submodule)))
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": names + ["needs-brain"]})
        self.actions += 1
        _say("%s#%d needs a change in submodule %s; left for the brain to split"
             % (repo, number, submodule))

    def briefed(self, repo: str, issue: Dict[str, Any]) -> None:
        if self.now < held_until(issue):
            return  # held by its Not before: line; nobody is late yet
        if "%s#%s" % (repo, issue["number"]) in self.held:
            # The station holds it (a factory incident, or the unproven-work
            # gate this pass): no lane is late, so it is neither spread nor
            # moved as stalled. Moving it rotated loom#633 through every rung
            # with a comment each time while the gate held it (2026-10-18).
            return
        waiting = self.held_by(repo, issue)
        if waiting:
            # Waiting for another ticket is not this one's fault: it is not
            # stalled, so it climbs nothing, and only a long hold is reported.
            self._tell_held(repo, issue, waiting)
            return
        if (_lane(issue) in self.limited or _lane(issue) in self.down
                or not self._serves(_lane(issue), issue_model(issue))):
            self._step_aside(repo, issue)
            return
        if not self._spread(repo, issue):
            self._unclaimed(repo, issue)

    def running(self, repo: str, issue: Dict[str, Any]) -> None:
        """A running ticket whose lane has sent no heartbeat for SILENT_MAX
        moves like a transient failure but is no failure: a free lane of its
        tier (of its model, for a Model: ticket), else back to the queue. It
        takes no rung marker and none of its automatic retries. A remote lane
        is left to the remote heartbeat rules, a held ticket to the station."""
        old = _lane(issue)
        number = int(issue["number"])
        silent = self.silent.get(old, 0)
        if (old in REMOTE_LANES or silent < SILENT_MAX
                or "%s#%d" % (repo, number) in self.held):
            return
        self.silent_running.setdefault(old, []).append((repo, number))
        model = issue_model(issue)
        lane = (self._queued_model_lane(repo, model) if model != "unspecified"
                else self._tier_move(repo, issue, old, allow_remote=False)[0])
        said = ("Not a failure (lane %s has sent no heartbeat for %d minutes): "
                % (old, silent // 60))
        if lane is None:
            self._status_note(repo, number,
                              said + "no lane of its tier is free, so the ticket goes back to the queue.")
            kept = stale_claims.without_own_claim(
                [x for x in _labels(issue) if x != "loom:running" and not x.startswith("lane:")],
                brain.trusted_authors())
            self._relabel(repo, issue, kept + ["loom:queued"])
            _say("queued %s#%d: lane %s sent no heartbeat for %d minutes"
                 % (repo, number, old, silent // 60))
        else:
            self._status_note(repo, number, said + "the factory moved this ticket to lane "
                              "%s, %s." % (lane, "in its difficulty tier"
                                           if model == "unspecified" else "which takes its model"))
            kept = brain._moved_labels(_labels(issue), "briefed", lane)
            self._relabel(repo, issue, kept)
            self.load[lane] = self.load.get(lane, 0) + 1
            _say("moved running %s#%d off silent lane %s to %s" % (repo, number, old, lane))
        self.load[old] = self.load.get(old, 0) - 1
        self.actions += 1

    def _waiting(self, repo: str, number: Any, reason: str) -> None:
        """Record why this pass leaves the ticket where it is (self.waits)."""
        self.waits["%s#%s" % (repo, number)] = reason

    def _wait_said(self, repo: str, number: Any, reason: str, message: str) -> None:
        """Record the wait and say `message` only when the ticket starts waiting
        for this reason, not on each pass it repeats it: 716 identical stall
        lines in two days hid the ones that mattered (loom#1280)."""
        key = "%s#%s" % (repo, number)
        if self.known_waits.get(key) != reason:
            _say(message)
        self._waiting(repo, number, reason)

    def _relabel(self, repo: str, issue: Dict[str, Any], labels: List[str]) -> None:
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, issue["number"]),
                     {"labels": labels})
        issue["labels"] = [{"name": x} for x in labels]

    def held_by(self, repo: str, issue: Dict[str, Any]) -> List[tickets.After]:
        """The After: tickets of this one that are not merged yet."""
        try:
            headers, _ = tickets.parse(issue.get("body"))
        except ValueError:
            return []  # an unusable brief is the station's to refuse
        return tickets.after_holds(repo, headers, self._dependency)[0]

    def _tell_held(self, repo: str, issue: Dict[str, Any],
                   waiting: Sequence[tickets.After]) -> None:
        """Report a ticket held by After: for more than HOLD_STUCK to the
        brain, once (needs-brain says it was reported)."""
        names = _labels(issue)
        if "needs-brain" in names:
            return
        number = int(issue["number"])
        since = self._briefed_since(repo, issue, self._comments(repo, number))
        if not since or self.now - since < HOLD_STUCK:
            return
        waiting_for = ", ".join(a.short for a in waiting)
        notify.post(self.gh, repo, number,
                    "Held %.0f hours by After: waiting for %s. The station never claims a "
                    "ticket whose After: tickets are not merged, so this one is not late and "
                    "not stuck: the brain checks that %s is still coming, re-briefs this "
                    "ticket without it, or closes it." % (
                        (self.now - since) / 3600, waiting_for,
                        waiting_for if len(waiting) == 1 else "they are"), notify.HELD)
        self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": ["needs-brain"]})
        self.actions += 1
        _say("%s#%d has waited %.0f hours for %s; reported to the brain"
             % (repo, number, (self.now - since) / 3600, waiting_for))

    def _briefed_since(self, repo: str, issue: Dict[str, Any],
                       comments: List[Dict[str, Any]]) -> float:
        """When the ticket's wait on its lane began: the newest of its last
        loom:briefed label, any lane: label, and the factory's own stall-move
        note, or its hold's end if later; 0 when it never was briefed. A
        moved ticket thus waits a full BRIEFED_STALL on its new lane."""
        events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, int(issue["number"])))
        named = [e for e in events if isinstance(e, dict) and e.get("event") == "labeled"]
        since = max((_ts(e.get("created_at")) for e in named
                     if (e.get("label") or {}).get("name") == "loom:briefed"), default=0.0)
        if not since:
            return since
        moved = [_ts(e.get("created_at")) for e in named
                 if str((e.get("label") or {}).get("name") or "").startswith("lane:")]
        moved += [_ts(c.get("created_at")) for c in comments
                  if STALL_MOVE_RE.match(str(c.get("body") or ""))]
        return max([since, held_until(issue)] + moved)

    def _stall_lanes(self, repo: str, issue: Dict[str, Any], rung: int) -> List[str]:
        """The other available lanes a stalled ticket may move to: those of
        its rung, or of its difficulty tier. Never up the ladder."""
        old = _lane(issue)
        pool = self._rung_lanes(repo, rung) if rung else tier_lanes(issue, self._is_private(repo))
        return [x for x in pool if x != old and self._up(x)
                and (not rung or self._serves(x, CLIMB[rung - 1][1]))
                and (x != "agy" or agy_takes_header(issue))]

    def _unclaimed(self, repo: str, issue: Dict[str, Any]) -> None:
        """A ticket briefed BRIEFED_STALL with no lane claiming it moves to the
        least-loaded other lane of its tier (or of its rung), with no rung
        marker: it never ran, so it has not failed on any model. With no such
        lane it stays briefed, and the stall is said once, when it starts."""
        number = int(issue["number"])
        comments = self._comments(repo, number)
        since = self._briefed_since(repo, issue, comments)
        if not since or self.now - since < BRIEFED_STALL:
            return
        if any(CLAIM_RE.match(str(c.get("body") or "")) and _ts(c.get("created_at")) >= since
               for c in comments):
            return
        old = _lane(issue)
        rung = current_rung(issue)
        lanes = self._stall_lanes(repo, issue, rung)
        if not lanes:
            kind = "rung" if rung else "tier"
            self._wait_said(repo, number, "stalled on lane %s: no other lane of its %s is available"
                            % (old, kind),
                            "%s#%d stalled on lane %s: no other lane of its %s is available"
                            % (repo, number, old, kind))
            return
        lane = pick_lane(lanes, self.load)
        waiting, there = self.load.get(old, 0), self.load.get(lane, 0)
        if self._up(old) and there and there >= waiting - 1:
            # A move to a lane with as long a wait only restarts the stall
            # clock: three tickets bounced codex-2 -> codex -> codex-2 for six
            # hours unclaimed (2026-10-21). It moves to a free or shorter lane.
            self._wait_said(repo, number, "stalled on lane %s: another lane of its %s waits as long"
                            % (old, "rung" if rung else "tier"),
                            "%s#%d stalled on lane %s: lane %s waits as long (%d vs %d)"
                            % (repo, number, old, lane, there, waiting))
            return
        self._move_stalled(repo, issue, lane, (self.now - since) / 3600, rung)

    def _move_stalled(self, repo: str, issue: Dict[str, Any], lane: str,
                      hours: float, rung: int) -> None:
        """Relabel a stalled ticket onto `lane` and say why; no rung marker."""
        number, old = int(issue["number"]), _lane(issue)
        kept = [x for x in _labels(issue) if not x.startswith("lane:")] + ["lane:" + lane]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]
        self._status_note(repo, number,
                          "Not a failure (briefed %.0f hours on lane %s with no claim): the "
                          "factory moved this ticket to lane %s, %s." % (
                              hours, old, lane,
                              "on the same rung" if rung else "in its difficulty tier"))
        self.load[old] = self.load.get(old, 0) - 1
        self.load[lane] = self.load.get(lane, 0) + 1
        self.actions += 1
        _say("moved stalled %s#%d to %s" % (repo, number, lane))

    def _spread(self, repo: str, issue: Dict[str, Any]) -> bool:
        """A ladder ticket waiting behind more than RUNG_BUSY others on its
        lane moves to an untried rung with a free lane that is up. Not a
        failure; the rung it leaves stays on its record. True when it moved.
        A lane down or at a limit holds no tickets, so counting it made a rung
        look free whose only up lane was busy: one ticket flipped between
        rung 2 on codex-2 and rung 3 on claude 8 times in 90 minutes and was
        never claimed (2026-10-20). The target's least-loaded up lane must be
        another lane holding fewer than this lane less one, so a move always
        shortens the wait and the next pass cannot move it back. A rung with one
        lane up and more than ONE_LANE_MAX waiting also moves its overflow to a
        rung that is busy but holds three or more fewer (loom#1280)."""
        here = current_rung(issue)
        old = _lane(issue)
        waiting = self.load.get(old, 0)
        if not here or waiting <= RUNG_BUSY:
            return False
        comments = self._comments(repo, int(issue["number"]))
        tried, reached = self._tried(issue, comments, failed_here=False)
        up = self._up_lanes(repo)
        # A rung with one lane up sends its overflow to a rung with fewer
        # waiting even when that one is busy too, so it holds a bounded few.
        overflow = len(up[here]) <= 1 and waiting > ONE_LANE_MAX

        def shorter(lanes: Tuple[str, ...]) -> bool:
            lane = pick_lane(lanes, self.load)
            load = self.load.get(lane, 0)
            if lane == old or load >= waiting - 1:
                return False
            # Overflow moves only to a rung with three or more fewer waiting,
            # so two busy rungs never trade a ticket back and forth.
            return load < RUNG_BUSY or (overflow and load + 2 < waiting)

        if not any(shorter(up[n]) for n in climb_rungs(tried | {here}, reached) if up[n]):
            return False
        lane = self._climb(repo, issue, comments, "%d tickets wait on lane %s"
                           % (self.load.get(old, 0), old), head="Not a failure",
                           failed_here=False, skip=here)
        if lane is None:
            return False
        self.load[old] = self.load.get(old, 0) - 1
        self.actions += 1
        _say("moved %s#%d off busy lane %s to %s" % (repo, issue["number"], old, lane))
        return True

    def _out_hours(self, lane: str) -> bool:
        """Whether the lane takes no work for hours: not available, down, or
        at a limit that lifts past LIMIT_REQUEUE or has no known end."""
        if lane in self.limited:
            return self.limit_until.get(lane, 0) == 0 or self._out_long(lane)
        return lane in self.down or (self.available is not None and lane not in self.available)

    def _out_long(self, lane: str) -> bool:
        """Whether the lane will take no work for over LIMIT_REQUEUE: down, or
        at a limit whose heartbeat says it lifts past that."""
        if lane in self.down and lane not in self.limited:
            return True
        return self.limit_until.get(lane, 0) > self.now + LIMIT_REQUEUE

    def _rung_waits_long(self, repo: str, rung: int) -> bool:
        """Whether no lane of the rung serves its model within RUNG_LIMIT_WAIT:
        each is down, at a limit lifting past it, or holds the model limited
        past it. A lane at a limit with no known end is not counted as a long
        wait. A whole-lane limit (out of credit: no limited_models) once never
        counted, and nine tickets waited a day on codex and codex-2 while other
        lanes had room (2026-10-21)."""
        model = CLIMB[rung - 1][1]
        lanes = self._rung_lanes(repo, rung)
        later = self.now + RUNG_LIMIT_WAIT
        return bool(lanes) and all(
            lane in self.down
            or (lane in self.limited and self.limit_until.get(lane, 0) > later)
            or self.model_limits.get(lane, {}).get(model, 0) > later
            for lane in lanes)

    def _aside_why(self, lane: str) -> str:
        """Why a ticket steps off the lane: demoted (bus/scorecard.py), down,
        or at a limit."""
        if lane in self.demoted:
            return "demoted (%s; the operator clears it)" % self.demoted[lane]
        return "down" if lane in self.down and lane not in self.limited else "at a limit"

    def _step_aside(self, repo: str, issue: Dict[str, Any]) -> None:
        """Move a ticket off a lane at a limit (or down) to the first free lane of its
        difficulty tier (or of a FALLBACK tier when none of its tier is up),
        or a ladder ticket to a free lane of its rung, with no rung marker.
        A ladder ticket whose rung serves no lane within RUNG_LIMIT_WAIT
        climbs to its next untried rung instead. With none free it stays where
        it is, for the lane to take once it is back."""
        number = int(issue["number"])
        if RUNAWAY in _labels(issue) or "needs-brain" in _labels(issue):
            return  # held for the brain: it moves nothing (loom#1224)
        old = _lane(issue)
        why = self._aside_why(old)
        rung = current_rung(issue)
        fallback = ""
        if rung:
            lane = next((x for x in self._rung_lanes(repo, rung) if x != old and self._free(x)
                         and self._serves(x, CLIMB[rung - 1][1])), None)
        else:
            lane, fallback = self._tier_move(repo, issue, old, allow_remote=False)
        if lane is None and rung and self._rung_waits_long(repo, rung):
            comments = self._comments(repo, number)
            tried_lanes = {match.group(1) for comment in comments
                           for match in [CLAIMED_RE.match(str(comment.get("body") or ""))]
                           if match}
            free_lane = next((candidate for candidate in lanes_of(
                difficulty_of(issue), self._is_private(repo))
                if lane_kind(candidate) == "free" and candidate not in tried_lanes
                and self._free(candidate)), None)
            if free_lane is not None:
                body = without_model(str(issue.get("body") or ""))
                self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
                kept = [x for x in _labels(issue) if not x.startswith("lane:")] \
                    + ["lane:" + free_lane]
                self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                             {"labels": kept})
                issue["body"] = body
                issue["labels"] = [{"name": x} for x in kept]
                self._status_note(repo, number,
                                  "Not a failure (lane %s is %s for over %d hours): the factory "
                                  "moved this ticket to free lane %s; its paid rung remains "
                                  "untried." % (old, why, RUNG_LIMIT_WAIT // 3600, free_lane))
                self.load[old] = self.load.get(old, 0) - 1
                self.load[free_lane] = self.load.get(free_lane, 0) + 1
                self.actions += 1
                _say("moved %s#%d off lane %s to free lane %s" % (
                    repo, number, old, free_lane))
                return
            climbed = self._climb(repo, issue, comments,
                                  "lane %s is %s for over %d hours" % (
                                      old, why, RUNG_LIMIT_WAIT // 3600),
                                  head="Not a failure", failed_here=False, skip=rung)
            if climbed is not None:
                self.load[old] = self.load.get(old, 0) - 1
                self.actions += 1
                _say("climbed %s#%d off lane %s, %s, to %s" % (repo, number, old, why, climbed))
                return
        if lane is None and not rung and self._out_long(old):
            # Briefed to a lane that will not take it for hours, it is in no
            # queue: the queue took each lane of its tier as it freed, and two
            # tickets sat 18 hours on lanes at a monthly limit (loom#1224).
            until = self.limit_until.get(old)
            self._status_note(repo, number, "Not a failure (lane %s is %s): no lane of its "
                              "tier is free, so the ticket goes back to the queue and takes "
                              "the next one in its turn." % (
                                  old, "at a limit until %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
                                  if until else why))
            kept = [x for x in brain._moved_labels(_labels(issue), "queued", None)
                    if not x.startswith("lane:")]
            self._relabel(repo, issue, kept)
            self.load[old] = self.load.get(old, 0) - 1
            self.actions += 1
            _say("queued %s#%d off lane %s, %s" % (repo, number, old, why))
            return
        if lane is None:
            self._waiting(repo, number, "on lane %s, %s: no free lane in its %s"
                          % (old, why, "rung" if rung else "tier"))
            _say("%s#%d waits on lane %s, %s: no free lane in its %s"
                 % (repo, number, old, why, "rung" if rung else "tier"))
            return
        body = str(issue.get("body") or "")
        if not rung:
            body = without_model(body)
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
            issue["body"] = body
        kept = [x for x in _labels(issue) if not x.startswith("lane:")] + ["lane:" + lane]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]
        if why == "at a limit" and self.limit_until.get(old):
            why += " until " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.limit_until[old]))
        self._status_note(repo, number,
                          "Not a failure (lane %s is %s): the factory moved this "
                          "ticket to lane %s, %s." % (
                              old, why, lane, "on the same rung" if rung else
                              fallback or "in its difficulty tier"))
        self.load[old] = self.load.get(old, 0) - 1
        self.load[lane] = self.load.get(lane, 0) + 1
        self.actions += 1
        _say("moved %s#%d off lane %s, %s, to %s%s" % (
            repo, number, old, why, lane, " (%s)" % fallback if fallback else ""))

    # -- lane load -------------------------------------------------------
    def count_load(self, tickets: Optional[Dict[str, Dict[int, Dict[str, Any]]]] = None) -> None:
        """Count the briefed and running tickets on each lane, reading every
        repo's tickets unless they are given. A ticket held for a factory
        incident is not load: counted, it kept its idle lane from every
        queued ticket until the incident closed (2026-10-18)."""
        if tickets is None:
            tickets = {repo: self._tickets(repo) for repo in self.repos}
        for repo, issue in ((r, i) for r, found in tickets.items() for i in found.values()):
            if "%s#%s" % (repo, issue["number"]) in self.held:
                continue
            if (_state(issue) in ("briefed", "running")
                    or (_state(issue) == "queued" and _lane(issue) in REMOTE_LANES
                        and self._up(_lane(issue)))):
                self.load[_lane(issue)] = self.load.get(_lane(issue), 0) + 1

    def _queued_model_lane(self, repo: str, model: str, busy_ok: bool = False) -> Optional[str]:
        """The first lane that takes `model` and is free; with `busy_ok`, the
        first that is merely up and not limited for the model, busy or not."""
        from .station import model_lanes, needs_public_repo
        return next((candidate for candidate in model_lanes(model)
                     if (self._up(candidate) and self._serves(candidate, model)
                         if busy_ok else self._free(candidate))
                     and (not needs_public_repo(candidate, model)
                          or not self._is_private(repo))), None)

    def _equivalent_lane(self, repo: str, model: str) -> Optional[Tuple[str, str, str]]:
        """(lane, model, why) from the model's EQUIVALENTS row for a public
        repo when every other lane that takes the model is at a limit or down
        and a lane of the row is free; None otherwise, so a merely busy lane
        is waited for."""
        if model not in EQUIVALENTS or self._is_private(repo):
            return None
        from .station import model_lanes
        lanes, equivalent, why = EQUIVALENTS[model]
        if any(self._up(x) for x in model_lanes(model) if x not in lanes):
            return None
        lane = next((x for x in lanes if self._free(x)), None)
        return (lane, equivalent, why) if lane is not None else None

    def assign_queued(self) -> None:
        """Give queued tickets free tier lanes (or FALLBACK lanes when none of
        the tier is up) across repos in tickets.work_order: FIRST labels,
        then priority, then the oldest. A pass that gives none a lane says
        how many wait and why the first does."""
        found_by_repo = {repo: self._tickets(repo) for repo in self.repos}
        self.count_load(found_by_repo)
        before = _placements(found_by_repo)
        # A ticket in the hand-off pool is the second operator's session's.
        queued = [(repo, issue) for repo, found in found_by_repo.items() for issue in found.values()
                  if _state(issue) == "queued" and HANDOFF_LABEL not in _labels(issue)]
        waits = [self._assign_queued_ticket(repo, issue)
                 for repo, issue in sorted(queued, key=lambda item: tickets.work_order(*item))]
        if waits and all(waits):
            _say("assigned 0 of %d queued: %s" % (len(waits), waits[0]))
        self._no_lane_moves(found_by_repo, before)

    def _assign_queued_ticket(self, repo: str, issue: Dict[str, Any]) -> str:
        """Give one queued ticket a lane: "" when it got one, else why it waits."""
        ref = "%s#%s" % (repo, issue["number"])
        held = self._queued_hold(repo, issue)
        if held:
            return "%s %s" % (ref, held)
        model_match = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
        if model_match:
            return self._assign_queued_model(repo, issue, model_match.group(1))
        assigned = _lane(issue)
        if assigned in REMOTE_LANES and not self._is_private(repo) and self._up(assigned):
            # A live remote assignment stays queued for its pull worker.
            self._waiting(repo, issue["number"], "remote lane %s has not pulled it" % assigned)
            return "%s waits for remote lane %s to pull it" % (ref, assigned)
        if assigned in REMOTE_LANES:
            lane, fallback = self._tier_move(repo, issue, old=assigned)
        else:
            lane, fallback = self._tier_move(repo, issue)
        if lane is None:
            self._waiting(repo, issue["number"], "no free lane in tier %s" % difficulty_of(issue))
            return "%s waits: no free lane in tier %s" % (ref, difficulty_of(issue))
        tier = difficulty_of(issue) + (", " + fallback if fallback else "")
        if not self._guarded(self._assign_queued_one, repo, issue, lane, tier):
            self._waiting(repo, issue["number"], "giving it a lane failed")
            return "%s: giving it lane %s failed" % (ref, lane)
        return ""

    def _queued_hold(self, repo: str, issue: Dict[str, Any]) -> str:
        """Why a queued ticket gets no lane this pass, or ""."""
        if self.now < held_until(issue) or self._host_only(repo, issue):
            return "is held"
        held = self._brief_wait(repo, issue)
        if held:
            return "waits: " + held
        # A ticket held by After: is never briefed: the station refuses the
        # claim each pass, and the lane it was given counts it as load and
        # sits idle until the other ticket merges (2026-10-22).
        waiting = self.held_by(repo, issue) if re.search(
            r"^After:", str(issue.get("body") or ""), re.M) else []
        if waiting:
            reason = "waits for %s to merge (After:)" % ", ".join(a.short for a in waiting)
            self._waiting(repo, issue["number"], reason)
            return reason
        return ""

    def _brief_wait(self, repo: str, issue: Dict[str, Any]) -> str:
        """Why a queued ticket gets no lane, or "": a stale ticket waits for
        the operator's answer (loom:human), and a health ticket for its cause
        (bus/brief_check.py). Reads nothing."""
        if "loom:human" in _labels(issue):
            reason = "waits for a person (loom:human)"
        elif brief_check.health_hold(issue):
            reason = "health ticket names no cause"
        else:
            return ""
        self._waiting(repo, issue["number"], reason)
        return reason

    def _host_only(self, repo: str, issue: Dict[str, Any]) -> bool:
        """Whether every acceptance criterion of the queued ticket is a check
        on a live host or network (operator_hold.host_only): it gets no lane
        and goes to loom:human with one ask, once. 2026-10-22: such a ticket
        was laned, the lane replaced a README to have a diff, and it merged."""
        criteria = operator_hold.host_only(str(issue.get("body") or ""))
        if not criteria:
            return False
        if "loom:human" in _labels(issue):
            return True
        try:
            self.human(self.gh, repo, int(issue["number"]), operator_hold.host_only_ask(criteria))
        except ValueError as err:
            # The ask was refused (its quoted criteria, say); the ticket still
            # gets no lane, and the log says why it waits with no ask.
            _say("held queued %s#%s for a person, but the ask was refused: %s"
                 % (repo, issue["number"], err))
            return True
        issue["labels"] = list(issue.get("labels") or []) + [{"name": "loom:human"}]
        self.actions += 1
        _say("held queued %s#%s for a person: every criterion is a host check"
             % (repo, issue["number"]))
        return True

    def _assign_queued_model(self, repo: str, issue: Dict[str, Any], model: str) -> str:
        """Give a queued ticket pinned to `model` a lane that takes it, or an
        equivalent's: "" when it got one, else why it waits."""
        ref = "%s#%s" % (repo, issue["number"])
        equivalent = self._equivalent_lane(repo, model)
        if equivalent is not None:
            if not self._guarded(self._assign_equivalent, repo, issue, model, *equivalent):
                return "%s: giving it lane %s failed" % (ref, equivalent[0])
            return ""
        lane = self._queued_model_lane(repo, model)
        if lane is None and set(FIRST) & set(_labels(issue)):
            # A lane that always has another fix round waiting is never free,
            # so a pinned regression ticket sat queued for days (loom#1300,
            # 2026-10-21). Brief it on an up lane; the station claims FIRST
            # tickets before the lane's other work.
            lane = self._queued_model_lane(repo, model, busy_ok=True)
        if lane is None:
            why = "%s waits: no free lane takes model %s" % (ref, model)
            self._waiting(repo, issue["number"], "no free lane takes model %s" % (
                model if tickets.MODEL_RE.fullmatch(model) and len(model) <= 64
                else "(its Model: line names no model id)"))
            _say(why)
            return why
        if not self._guarded(self._assign_queued_one, repo, issue, lane, "model %s" % model):
            return "%s: giving it lane %s failed" % (ref, lane)
        return ""

    def _assign_equivalent(self, repo: str, issue: Dict[str, Any], pinned: str,
                           lane: str, model: str, why: str) -> None:
        """Brief a queued ticket on `lane` with the equivalent `model` written
        to its Model: line, so its claim records the model that ran. The note
        carries EQUIVALENT_MARKER, which leaves the pinned rung untried."""
        number = int(issue["number"])
        self.move(self.gh, repo, number, "briefed", lane=lane, model=model)
        self._status_note(repo, number,
                          "%s\nThe station gave this queued ticket lane %s with model %s in "
                          "place of %s (equivalent: %s)." % (
                              EQUIVALENT_MARKER % model, lane, model, pinned, why))
        issue["labels"] = [{"name": x} for x in brain._moved_labels(
            _labels(issue), "briefed", lane)]
        issue["body"] = with_model(str(issue.get("body") or ""), model)
        self.load[lane] = self.load.get(lane, 0) + 1
        _say("gave queued %s#%d lane %s with %s in place of %s"
             % (repo, number, lane, model, pinned))

    def _assign_queued_one(self, repo: str, issue: Dict[str, Any], lane: str,
                           tier: str) -> None:
        number = int(issue["number"])
        if lane in REMOTE_LANES:
            self.move(self.gh, repo, number, "queued", lane=lane)
            self._status_note(repo, number,
                              "The station reserved this queued ticket for remote lane %s (tier %s)."
                              % (lane, tier))
            issue["labels"] = [{"name": x} for x in brain._moved_labels(
                _labels(issue), "queued", lane)]
            self.load[lane] = self.load.get(lane, 0) + 1
            _say("reserved queued %s#%d for remote lane %s (tier %s)"
                 % (repo, number, lane, tier))
            return
        self.move(self.gh, repo, number, "briefed", lane=lane)
        self._status_note(repo, number,
                          "The station gave this queued ticket lane %s (tier %s)." % (lane, tier))
        issue["labels"] = [{"name": x} for x in brain._moved_labels(
            _labels(issue), "briefed", lane)]
        self.load[lane] = self.load.get(lane, 0) + 1
        _say("gave queued %s#%d lane %s (tier %s)" % (repo, number, lane, tier))

    # -- tickets no lane takes (loom#1831) --------------------------------
    def _no_lane_moves(self, found: Dict[str, Dict[int, Dict[str, Any]]],
                       before: Dict[str, Tuple[str, str]]) -> None:
        """End the no-lane waits this pass can end. A ticket a lane took
        (LANED_STATES, or a new lane while briefed) is settled. One that has
        waited with no lane (no_lane) for reroute_after is rerouted to another
        ladder rung with a free lane in its tier (_reroute); with none, by
        NO_LANE_HANDOFF it goes to the hand-off pool (_hand_off)."""
        for repo, issues in found.items():
            for number, issue in issues.items():
                key = "%s#%s" % (repo, number)
                placed = (_state(issue), _lane(issue))
                if (placed[0] in LANED_STATES or HANDOFF_LABEL in _labels(issue)
                        or (placed[0] == "briefed" and placed != before.get(key))):
                    self.settled.add(key)
        for key, reason in sorted(self.waits.items()):
            age = self.now - self.no_lane_since.get(key, self.now)
            if not no_lane(reason) or age < self.reroute_after:
                continue
            repo, _, number = key.rpartition("#")
            issue = found.get(repo, {}).get(int(number)) if number.isdigit() else None
            if issue is None or {RUNAWAY, "needs-brain", HANDOFF_LABEL} & set(_labels(issue)):
                continue  # held for the brain, or already handed off
            if self.actions >= MAX_ACTIONS:
                return
            moved: List[bool] = []
            if not self._guarded(self._reroute_into, repo, issue, reason, age, moved):
                continue
            if not (moved and moved[0]):
                if age < NO_LANE_HANDOFF or not self._guarded(
                        self._hand_off, repo, issue, reason, age):
                    continue
            self.waits.pop(key, None)
            self.settled.add(key)

    def _reroute_into(self, repo: str, issue: Dict[str, Any], reason: str, age: float,
                      moved: List[bool]) -> None:
        """_reroute for _guarded, which keeps no return value: appends it."""
        moved.append(self._reroute(repo, issue, reason, age))

    def _reroute(self, repo: str, issue: Dict[str, Any], reason: str, age: float) -> bool:
        """Move a queued or briefed ticket pinned to a model no lane takes to
        the first rung climb_rungs allows (never down from a strong model)
        that has a free lane in the ticket's difficulty tier, for its repo
        (PUBLIC_ONLY) and up for the rung's model (_up_lanes: not at a limit,
        not benched, not out of credit). The note carries the rung marker, as
        a "Not a failure" climb does. True when it moved."""
        number, state, old = int(issue["number"]), _state(issue), _lane(issue)
        match = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
        if state not in ("queued", "briefed") or not match:
            return False  # an unpinned ticket's tier moves are the queue's own
        here = next((n for n, (_, model) in enumerate(CLIMB, 1) if model == match.group(1)), 0)
        tier = set(lanes_of(difficulty_of(issue), self._is_private(repo)))
        up = self._up_lanes(repo)
        free = {rung: [x for x in lanes if x in tier and x != old and self._free(x)]
                for rung, lanes in up.items() if rung != here}
        if not any(free.values()):
            return False  # nothing to move to: no comment read, every pass
        tried, reached = self._tried(issue, self._comments(repo, number), failed_here=False)
        found = next(((rung, free[rung][0]) for rung in climb_rungs(tried | ({here} - {0}), reached)
                      if free.get(rung)), None)
        if found is None:
            return False
        rung, lane = found
        model = CLIMB[rung - 1][1]
        body = with_model(str(issue.get("body") or ""), model)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
        issue["body"] = body
        self._status_note(repo, number, "%s\nNot a failure (no lane took it for %.0f hours: %s): "
                          "the factory rerouted this ticket to rung %d, %s on lane %s." % (
                              RUNG_MARKER % rung, age / 3600, reason, rung, model, lane))
        if state == "queued":
            self._assign_queued_one(repo, issue, lane, "%s, rerouted to model %s" % (
                difficulty_of(issue), model))
        else:
            self._relabel(repo, issue, [x for x in _labels(issue) if not x.startswith("lane:")]
                          + ["lane:" + lane])
            self.load[old] = self.load.get(old, 0) - 1
            self.load[lane] = self.load.get(lane, 0) + 1
        self.actions += 1
        _say("rerouted %s#%d after %.0f hours with no lane (%s): %s on lane %s"
             % (repo, number, age / 3600, reason, model, lane))
        return True

    def _hand_off(self, repo: str, issue: Dict[str, Any], reason: str, age: float) -> None:
        """Put a ticket no lane took for NO_LANE_HANDOFF, with no reroute
        allowed, in the hand-off pool: queued with no lane and HANDOFF_LABEL,
        so it leaves its goal's in-flight count (brain._in_flight) and the
        factory moves it no more. One note on its status comment, edited in
        place; a handed-off ticket is never handed off again."""
        number, old = int(issue["number"]), _lane(issue)
        if _state(issue) not in ("queued", "briefed"):
            return
        kept = [x for x in brain._moved_labels(_labels(issue), "queued", None)
                if not x.startswith("lane:")] + [HANDOFF_LABEL]
        self._relabel(repo, issue, kept)
        if old:
            self.load[old] = self.load.get(old, 0) - 1
        self._status_note(repo, number, (
            "No lane of this Loom took this ticket in %.0f hours (%s), and no other lane or "
            "model rung its tier allows is free. It leaves the goal's in-flight count for the "
            "hand-off pool (%s): the second operator's session may claim it. Taking the label "
            "off returns it to this Loom's queue.") % (age / 3600, reason, HANDOFF_LABEL))
        self.actions += 1
        _say("handed off %s#%d after %.0f hours with no lane (%s)"
             % (repo, number, age / 3600, reason))

    def tier_lane(self, repo: str, difficulty: str) -> Optional[str]:
        """The first free lane of the difficulty tier the repo may use, else
        its least-loaded lane that is up and not at a limit; None when the
        tier has none. count_load first."""
        lanes = lanes_of(difficulty, self._is_private(repo))
        free = next((x for x in lanes if self._free(x)), None)
        if free is not None:
            return free
        up = [x for x in lanes if self._up(x)]
        return pick_lane(up, self.load) if up else None

    # -- the pass --------------------------------------------------------
    def run(self) -> None:
        tickets = {repo: self._tickets(repo) for repo in self.repos}
        self.count_load(tickets)
        before = _placements(tickets)
        for repo in self.repos:
            self._repo_pass(repo, tickets[repo])
        self._no_lane_moves(tickets, before)

    def _repo_pass(self, repo: str, tickets: Dict[int, Dict[str, Any]]) -> None:
        # GitHub.paginate asks for 100 a page and follows every page.
        all_prs = [p for p in self.gh.paginate("/repos/%s/pulls?state=open" % repo)
                   if isinstance(p, dict)]
        self._guarded(lambda r, _: self._settle_answers(r), repo, {})
        closed_numbers = self._close_superseded_prs(repo, all_prs, tickets)
        prs = [p for p in all_prs if self._trusted(p) and int(p.get("number") or 0) not in closed_numbers]
        linked = {a[1]: pr for pr in prs for a in [self._association(repo, pr)] if a}
        for pr in landing_order(prs, tickets, linked):
            if self.actions >= MAX_ACTIONS:
                return
            self._guarded(self.pull_request, repo, pr, tickets)
        self._guarded(lambda r, _: self._close_unmerged_follow_ups(r), repo, {})
        steps = {"failed": lambda r, i: self.failed(r, i, linked), "briefed": self.briefed,
                 "running": self.running, "merged": self.settle_deferred,
                 "shipped": self.settle_deferred, "verified": self.settle_deferred}
        for issue in list(tickets.values()):
            step = steps.get(_state(issue))
            if self.actions >= MAX_ACTIONS:
                return
            if HANDOFF_LABEL in _labels(issue) and _state(issue) in (
                    "queued", "briefed", "running", "failed"):
                continue  # the second operator's session has it (docs/BRAIN.md)
            if step is not None:
                self._guarded(step, repo, issue)

    @staticmethod
    def _guarded(step: Callable[..., None], repo: str, item: Dict[str, Any], *rest: Any) -> bool:
        """One item's failure is logged and never stops the others; False
        when the step failed."""
        try:
            step(repo, item, *rest)
        except (GitHubError, ValueError, KeyError, TypeError) as err:
            _say("%s on %s#%s failed: %s" % (getattr(step, "__name__", "step"), repo,
                                             item.get("number"), err))
            return False
        return True


def due(state_dir: str, now: float) -> bool:
    """True at most every FACTORY_CHECK seconds; records the run."""
    path = os.path.join(state_dir, "factory.json")
    try:
        with open(path) as fh:
            last = float(json.load(fh).get("ran", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        last = 0.0
    # A small negative age (the clock stepped back) still waits; a stored time
    # far in the future (a clock that was wrong) is ignored, so the factory
    # is never shut out until that time comes round.
    if -FACTORY_CHECK < now - last < FACTORY_CHECK:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"ran": now}, fh)
    os.replace(tmp, path)
    return True


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["credit-slots"]:
        print("\n".join(SHARED_CREDIT))
    else:
        sys.exit("usage: python3 -m bus.factory credit-slots")
