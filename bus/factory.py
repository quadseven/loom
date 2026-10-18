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
  when it is stuck: a review thread survived a fix round, checks still fail
  after a round that was asked to fix them, or the pull request is older
  than STUCK_AGE. A review that finds new things is progress, and a conflict
  is mechanical (the fix brief's runner merges the base), so neither climbs.
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
- The review bot replies "Fixed in <sha>" on a thread whose flagged code
  changed, but GitHub does not let its app resolve threads, so the thread
  stays open. Such a thread, with nobody writing after the bot's reply, is
  resolved here before the pull request is judged; otherwise it would count
  as a finding that survived the round (77 of 218 open threads on
  2026-10-13 were the bot's own "Fixed in" threads).
- A round where the lane made no change and explained why waits for the
  review it asked for, up to REVIEW_GRACE, before the round is judged.
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
  waits where it is for its lane to come back.
- When no lane of a ticket's tier is up, a limit move or a queued assignment
  falls back to a free lane of the tiers FALLBACK lists (medium: hard, then
  easy), and says so in its note. The ticket's Difficulty: line is kept, so
  its next move takes its own tier again once a lane of it is up.
- Past the last rung the ticket is left failed for the brain, with a note.
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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import brain, change_class, tickets
from .github import GitHub, GitHubError
from .labels import REMOTE_LANES
from .tickets import FIX_MARKER, PRIORITIES, priority_of

FACTORY_CHECK = 300
STUCK_AGE = 6 * 3600
BRIEFED_STALL = 2 * 3600
# A ticket held by its After: line this long, with none of the tickets it
# waits for merged, is reported to the brain once like a stuck ticket.
HOLD_STUCK = 48 * 3600
# A head commit with no review check this long after its push is logged once
# per pass as waiting on a review that may never come; it is never merged.
REVIEW_GRACE = 1800
# At most this many merges, send-backs and climbs per pass.
MAX_ACTIONS = 12
DEFAULT_REVIEW_CHECK = "Grug - Elder"
# The review bot's login as GraphQL reports it (an app, without "[bot]").
DEFAULT_REVIEW_BOT = "grug-tribe"
BOT_FIXED_PREFIX = "Fixed in "

ANTHROPIC = ("anthropic",) + tuple("anthropic-%d" % n for n in range(2, 9))
# The slots that draw on one API credit: an empty credit holds all of them.
# run_lane.sh reads this set (`python3 -m bus.factory credit-slots`).
SHARED_CREDIT = ANTHROPIC
# The operator's ladder, 2026-10-12: "sonnet 5.5, or codex sol or opus 5.5
# worst case". Each rung lists the lanes that serve it, best first.
LADDER: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("claude",), "claude-sonnet-5-5"),
    (("codex", "codex-2"), "gpt-6-sol"),
    (("claude",) + ANTHROPIC, "claude-opus-5-5"),
)
# The lanes a ticket may move to when its lane is at a limit, by its
# Difficulty: line, best first (docs/MUSE.md, Lane choice; the doc test reads
# this table). A limit is not a failure, so the move stays in the tier and
# never climbs the ladder: on 2026-10-13 a private repo's easy ticket on a
# hot spark lane went up the ladder to claude, the operator's subscription.
SPARKS = ("spark", "spark-2", "spark-3", "spark-4")
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
    "easy": SPARKS + ("muse", "agy", "opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4",
                      "openrouter", "workersai", "zen", "zai"),
    "medium": SPARKS + ("muse", "agy", "codex", "codex-2", "opencode-go", "opencode-go-2",
                        "opencode-go-3", "opencode-go-4") + ANTHROPIC,
    # agy runs Gemini 4 Argon on the operator's Google plan since 2026-10-15:
    # Artificial Analysis Intelligence Index 53 at high effort, beside Claude
    # Opus 5.5 high (54) and GPT-6.1 Sol high (50), so it takes hard work on
    # public repos (agy stays public only).
    "hard": SPARKS + ANTHROPIC + ("claude", "agy", "codex", "codex-2"),
}
# The strongest model each lane has, for a hard ticket with no Model: line
# (bus/tickets.py render_brief writes it into the brief, so run_lane.sh runs
# it instead of the lane default). Artificial Analysis Intelligence Index at
# high effort: Gemini 4 Argon 53, GPT-6.1 Sol 50; the codex lanes default to
# gpt-6-luna, so a hard ticket on codex ran luna until 2026-10-17. Medium and
# easy tickets keep the lane default.
HARD_MODELS: Dict[str, str] = {"codex": "gpt-6-sol", "codex-2": "gpt-6-sol",
                               "agy": "gemini-4-argon-high"}
# A model pin another lane may serve with an equivalent model while every
# lane of the pin is at a limit or down (not merely busy): the lanes, the
# model they run, and why it is equivalent. Public repos only, as agy is.
# On 2026-10-17 every Anthropic lane was out of credit for hours while five
# tickets pinned to claude-opus-5-5 (index 54) waited; Gemini 4 Argon high
# is 53.
EQUIVALENTS: Dict[str, Tuple[Tuple[str, ...], str, str]] = {
    "claude-opus-5-5": (("agy",), "gemini-4-argon-high",
                        "Gemini 4 Argon high, index 53, while every Anthropic lane is out"),
}


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


# The ladder's rungs and then Gemini 4 Argon high on agy (rung 4, public repos
# only, as agy is). On 2026-10-17 a ticket Opus had failed five times was
# given up on while gpt-6-sol and Gemini 4 Argon were never tried.
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
PUBLIC_ONLY = ("zen", "zai", "agy", "opencode-go-4", "muse")
# A rung whose least-loaded lane already holds this many briefed or running
# tickets is passed over for the next one (docs/MUSE.md: "a higher rung rather
# than a wait behind a busy lane"). The last rung takes the work regardless.
RUNG_BUSY = 2
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
# bus/station.py's note on a failed run, and the runner's reason when a run
# committed nothing and its agent wrote no reason why (lanes/run_lane.sh).
FAILED_RE = re.compile(r"^Lane \S+ failed: ")
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
                 ' pullRequest(number:$number) { reviewThreads(first:100) { nodes { id isResolved path'
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
DEFER_TITLE = "Deferred review findings from %s#%d"
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
# The scanner's cross-repo reference shape: [owner/]name#N.
SCAN_REF_RE = re.compile(r"(?<![\w/-])(?:([a-z0-9][a-z0-9._-]*)/)?"
                         r"([a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9])#(\d+)\b", re.I)
BODY_LEAK_MARKER = "<!-- loom:body-leak-repaired %s -->"
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
    brain._check_written_text(comment)
    gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body": comment})
    gh.rest("PATCH", "/repos/%s/pulls/%d" % (repo, number), {"state": "closed"})


def _ts(value: Any) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


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
          no_test: bool = False) -> bool:
    """Did the last fix round fail to move this pull request? True when a
    thread it was asked to fix is still open, when checks it was asked to fix
    still fail, when it was asked for a test and still has none, or when the
    pull request is older than STUCK_AGE and has not moved on the ladder for
    STUCK_AGE (age alone climbs once per window, not on every round)."""
    if age > STUCK_AGE and now - last_climb(comments) > STUCK_AGE:
        return True
    last = next((str(c.get("body") or "") for c in reversed(comments)
                 if str(c.get("body") or "").startswith(FIX_MARKER)), None)
    if last is None:
        return False
    if set(FIX_THREAD_RE.findall(last)) & set(open_threads):
        return True
    if no_test and NO_TEST_HEADING in last:
        return True
    return checks_failing and bool(FIX_CHECK_RE.search(last))


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


def bot_fixed(threads: Sequence[Dict[str, Any]], bot: str) -> List[str]:
    return [str(t.get("id")) for t in threads if bot_marked_fixed(t, bot)]


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
    with no fix round."""
    still = [t for t in threads if not t.get("isResolved")]

    def minor(t: Dict[str, Any]) -> bool:
        root = _end(t, "root", 0)
        return (brain.by_bot(root, bot) and bool(MINOR_RE.match(str(root.get("body") or "")))
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
    else on its lane's configured model setting (bus/brain.py _lane_model),
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
            ran = footer[1] if footer and footer[0] == lane else brain._lane_model(lane)
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


def _attempt_rows(comments: Sequence[Any], reset_at: Any = None) -> List[List[Any]]:
    """Each claim as [epoch, time, lane, reason it failed], oldest first, from
    the claim and "Lane X failed:" comments. An attempt takes the failure that
    followed it, or "" when none was recorded. With `reset_at` given, a
    comment it matches starts a new segment: only the last segment's claims
    are returned."""
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
        failure = FAILED_RE.match(body)
        if failure and found and not found[-1][3]:
            found[-1][3] = body[failure.end():]
    return found


def attempts(comments: Sequence[Any], current: str = "") -> List[Tuple[int, str, str, str]]:
    """Each claim as (epoch, time, lane, reason it failed), oldest first, from
    the claim and "Lane X failed:" comments. An attempt takes the failure that
    followed it, or "" when none was recorded; with `current` given, the last
    attempt is the one being reported and takes that reason."""
    found = _attempt_rows(comments)
    if found and current:
        found[-1][3] = current
    return [(a[0], a[1], a[2], a[3]) for a in found]


def _is_brief(body: str) -> bool:
    """Whether the comment assigns the ticket to a lane: a brief."""
    return any(rx.search(body) for rx in BRIEF_RES)


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
                    ran: Optional[Dict[str, str]] = None) -> str:
    """The comment a flagged ticket carries: why, and its attempt table, each
    attempt with the model it ran (`ran`, from attempt_models), else `model`."""
    ran = ran or {}
    lines = ["Runaway: %s." % trigger, "| Time | Lane | Model | Reason |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s |" % (stamp, lane, ran.get(stamp) or model, _cell(reason))
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
    STRONG one instead: a ticket Opus failed goes on to gpt-6-sol and Gemini 4
    Argon high, never back to claude-sonnet-5-5."""
    if any(n >= len(LADDER) for n in tried | {reached}):
        return [n for n in range(1, len(CLIMB) + 1)
                if n not in tried and CLIMB[n - 1][1] in STRONG]
    return [n for n in range(reached + 1, len(CLIMB) + 1) if n not in tried]


def next_rung(tried: set, load: Dict[str, int], eligible: Optional[set] = None,
              reached: int = 0) -> int:
    """The lowest rung of climb_rungs with a lane under RUNG_BUSY
    (docs/MUSE.md: a higher rung rather than a wait behind a busy lane), else
    the one whose least-loaded lane holds the least, to wait there. 0 when
    none is left. Waiting on the lowest rung instead queued 33 tickets on the
    one claude lane on 2026-10-13 while six other ladder slots sat idle."""
    untried = [n for n in climb_rungs(tried, reached) if eligible is None or n in eligible]
    if not untried:
        return 0
    least = {n: min(load.get(lane, 0) for lane in CLIMB[n - 1][0]) for n in untried}
    free = [n for n in untried if least[n] < RUNG_BUSY]
    return free[0] if free else min(untried, key=lambda n: (least[n], n))


def pr_faults(details: Dict[str, Any], full: Dict[str, Any]) -> List[str]:
    """What keeps a pull request from landing, in words for its ticket."""
    return [w for bad, w in ((not brain._checks_green(details), "failing checks"),
                             (not brain._threads_resolved(details), "open review threads"),
                             (full.get("mergeable") is False, "a conflict")) if bad]


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
    """The ticket body with its Model: header line set to `model`."""
    head, sep, rest = body.partition("\n\n")
    if MODEL_LINE.search(head + "\n"):
        return MODEL_LINE.sub("Model: %s\n" % model, head + "\n", count=1).rstrip("\n") + sep + rest
    return "Model: %s\n%s" % (model, body)


def without_model(body: str) -> str:
    """The ticket body without its Model: header line, so the lane's own
    default model runs it."""
    head, sep, rest = body.partition("\n\n")
    return MODEL_LINE.sub("", head + "\n", count=1).rstrip("\n") + sep + rest


def difficulty_of(issue: Dict[str, Any]) -> str:
    """The ticket's Difficulty: line, "" when it has none readable."""
    try:
        return str(tickets.parse(issue.get("body"))[0].get("Difficulty", ""))
    except ValueError:
        return ""


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
                 close: Optional[Callable[..., Any]] = None) -> None:
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
        self.close = close if close is not None else _default_close
        self.limited = set(limited)
        # Lanes whose heartbeat says available; None trusts every lane not
        # at a limit.
        self.available = None if available is None else set(available)
        self._private: Dict[str, bool] = {}
        self.no_test_repos = {r.lower() for r in no_test_repos}
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

    def _comments(self, repo: str, number: int) -> List[Dict[str, Any]]:
        """The ticket's comments from trusted logins only."""
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
        for tier in FALLBACK.get(difficulty, ()):
            fallback_lanes = [x for x in lanes_of(tier, private)
                              if x != old and (allow_remote or x not in REMOTE_LANES)
                              and (x != "agy" or agy_ok)]
            lane = next((x for x in fallback_lanes if self._free(x)), None)
            if lane is not None:
                return lane, "fallback: no %s lane is up" % difficulty
        return None, ""

    def _check_runs(self, repo: str, sha: str) -> List[Dict[str, Any]]:
        """Every check run on the commit, all pages."""
        runs: List[Dict[str, Any]] = []
        for page in range(1, 11):
            data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100&page=%d"
                                % (repo, sha, page)).data or {}
            batch = [r for r in data.get("check_runs") or [] if isinstance(r, dict)]
            runs += batch
            if len(batch) < 100:
                break
        return runs

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
    def _resolve_bot_fixed(self, repo: str, number: int) -> None:
        owner, name = repo.split("/", 1)
        data = self.gh.graphql(THREADS_QUERY, {"owner": owner, "name": name,
                                               "number": number}) or {}
        threads = ((((data.get("repository") or {}).get("pullRequest") or {})
                    .get("reviewThreads") or {}).get("nodes") or [])
        picked = bot_fixed(threads, self.review_bot)
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
        title = DEFER_TITLE % (repo, number)
        for found in self.gh.paginate("/repos/%s/issues?labels=%s&state=open" % (repo, DEFER_LABEL)):
            if isinstance(found, dict) and found.get("title") == title and self._trusted(found):
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
        request to a follow-up issue, then resolve them. True when it did.
        With `immediate` (docs, tests, or a revert) they defer at once."""
        owner, name = repo.split("/", 1)
        data = self.gh.graphql(THREADS_QUERY, {"owner": owner, "name": name,
                                               "number": number}) or {}
        threads = ((((data.get("repository") or {}).get("pullRequest") or {})
                    .get("reviewThreads") or {}).get("nodes") or [])
        rounds = fixes_of(comments)
        picked = deferrable(threads, self.review_bot, asked_threads(comments), rounds, immediate)
        if not picked:
            return False
        follow = self._follow_up(repo, number, ticket, sha, picked, rounds)
        if not follow:
            return False
        reply = ("Deferred to #%d: a medium or low finding still open after %d fix "
                 "round(s), with the review check passed on %s." % (follow, rounds, sha[:12]))
        for t in picked:
            self.gh.graphql(REPLY_MUTATION, {"id": str(t.get("id")), "body": reply})
            self.gh.graphql(RESOLVE_MUTATION, {"id": str(t.get("id"))})
        self.actions += 1
        _say("deferred %d finding(s) on %s#%d to #%d" % (len(picked), repo, number, follow))
        return True

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
        up = {n: tuple(lane for lane in self._rung_lanes(repo, n) if self._up(lane))
              for n in range(1, len(CLIMB) + 1)}
        rung = next_rung(tried | ({skip} - {0}), self.load, {n for n in up if up[n]}, reached)
        if not rung:
            return None
        lanes, model = up[rung], CLIMB[rung - 1][1]
        lane = pick_lane(lanes, self.load)
        body = with_model(str(issue.get("body") or ""), model)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
        kept = [x for x in _labels(issue) if not x.startswith("lane:")] + ["lane:" + lane]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {
            "body": "%s\n%s (%s): the factory moved this ticket up the "
                    "model ladder to rung %d, %s on lane %s." % (
                        RUNG_MARKER % rung, head, why, rung, model, lane)})
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
                      note="The factory tried every rung of the model ladder (%s; ran on %s). "
                           "The brain rewrites this brief smaller, or asks the "
                           "operator one exact question." % (why, ", ".join(ran) or "none"))
        _say("%s#%d is past the last rung; left failed for the brain" % (repo, number))

    def _send_back(self, repo: str, pr: Dict[str, Any], issue: Dict[str, Any],
                   comments: List[Dict[str, Any]], why: str,
                   open_threads: Sequence[str] = (), checks_failing: bool = False,
                   no_test: bool = False) -> None:
        number = int(issue["number"])
        age = self.now - _ts(pr.get("created_at"))
        if stuck(comments, open_threads, checks_failing, age, self.now, no_test):
            footer = self._footer(repo, pr)
            if self._climb(repo, issue, comments, why, footer=footer) is None:
                if RUNAWAY in _labels(issue):
                    return
                if self._rungs_left(repo, issue, comments, footer):
                    # Not past the ladder: no lane of an untried rung is up.
                    # It waits, with no comment, and climbs on a later pass.
                    _say("%s#%d waits: no lane is up for its next rung" % (repo, number))
                    return
                tried, _ = self._tried(issue, comments, footer=footer)
                self._give_up(repo, issue, why, [CLIMB[n - 1][1] for n in sorted(tried)
                                                 if 1 <= n <= len(CLIMB)])
                return
        lane = None
        if _lane(issue) in REMOTE_LANES:
            lane, _ = self._tier_move(repo, issue, allow_remote=False)
            if lane is None:
                lane = LADDER[0][0][0]
        if lane is not None:
            self.fix(self.gh, repo, int(pr["number"]), (repo, number),
                     TEST_NOTES if no_test else FIX_NOTES, lane=lane)
        else:
            self.fix(self.gh, repo, int(pr["number"]), (repo, number),
                     TEST_NOTES if no_test else FIX_NOTES)
        self.actions += 1
        _say("sent %s#%d back for #%d (%s)" % (repo, pr["number"], number, why))

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
        faults = pr_faults(details, full)
        # Medium and low findings never send a pull request back on their own
        # (2026-10-14: 64% of 361 fix rounds were review threads, most of them
        # medium; "high-complexity" alone was 220 of ~1000 findings). They go
        # to the follow-up issue at once, for every class; high and critical
        # findings and failing checks still send it back.
        if (faults == ["open review threads"] and review == "reviewed"
                and self._defer_minor(repo, number, int(issue["number"]), sha, comments,
                                      True)):
            details = brain._pr_graphql(self.gh, repo, number)
            faults = pr_faults(details, full)
        if faults == ["failing checks"] and (
                self._repair_body_leak(repo, number, full, issue, comments, sha)
                or self._run_again(repo, number, sha)):
            return
        if faults:
            self._send_back(repo, full, issue, comments,
                            ", ".join(faults), open_thread_ids(details), "failing checks" in faults)
        elif review == "reviewed":
            self._land_or_ask_test(repo, number, full, issue, comments, sensitive)

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
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, int(issue["number"])),
                     {"body": "%s\nThe leak scan on #%d failed on its title and body only, "
                              "which the runner writes from this ticket, so the factory "
                              "edited the text instead of sending it back:\n%s"
                              % (marker, number, "\n".join("- " + c for c in changed))})
        self.actions += 1
        _say("rewrote %d flagged line(s) of %s#%d's text after a body-only leak scan"
             % (len(changed), repo, number))
        return True

    def _land_or_ask_test(self, repo: str, number: int, full: Dict[str, Any],
                          issue: Dict[str, Any], comments: List[Dict[str, Any]],
                          sensitive: bool = False) -> None:
        """Land a clean, reviewed pull request, or send it back when it
        changes code without a test. A clean sensitive one waits for the brain."""
        if self._needs_test(repo, number, issue):
            self._send_back(repo, full, issue, comments,
                            "code changed without a test", no_test=True)
        elif not sensitive:
            self._land(repo, number, full, issue)

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

    def _behind(self, repo: str, full: Dict[str, Any]) -> int:
        base = str((full.get("base") or {}).get("ref") or "")
        head = str((full.get("head") or {}).get("sha") or "")
        data = self.gh.rest("GET", "/repos/%s/compare/%s...%s"
                            % (repo, quote(base, safe=""), head)).data or {}
        return int(data.get("behind_by") or 0)

    def _land(self, repo: str, number: int, full: Dict[str, Any],
              issue: Dict[str, Any]) -> None:
        """Merge a ready pull request that is up to date with its base, one a
        repo a pass; bring one that is behind up to date instead."""
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
                _say("could not bring %s#%d up to date; it waits for the next pass" % (repo, number))
                return
            self.actions += 1
            _say("brought %s#%d up to date with its base; it lands once its checks "
                 "and review pass on the new head" % (repo, number))
            return
        if self._merge(repo, number, issue):
            self.landed.add(repo)

    def _merge(self, repo: str, number: int, issue: Dict[str, Any]) -> bool:
        try:
            self.merge(self.gh, repo, number, (repo, int(issue["number"])))
        except (ValueError, GitHubError) as err:
            _say("did not merge %s#%d: %s" % (repo, number, err))
            return False
        self.actions += 1
        _say("merged %s#%d for #%d" % (repo, number, issue["number"]))
        return True

    def failed(self, repo: str, issue: Dict[str, Any],
               prs: Dict[int, Dict[str, Any]]) -> None:
        number = int(issue["number"])
        comments = self._comments(repo, number)
        if repeated_no_reason(comments):
            self._hold_no_reason(repo, issue)
            return
        pr = prs.get(number)
        lane = self._climb(repo, issue, comments, "it failed on lane %s" % _lane(issue),
                           footer=self._footer(repo, pr))
        if lane is None:
            return  # it stays failed: for a free lane, or for the brain
        if pr is not None:
            # Back to loom:pr so the fix brief reuses its open pull request.
            kept = [x for x in _labels(issue) if x != "loom:failed"] + ["loom:pr"]
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
            self.fix(self.gh, repo, int(pr["number"]), (repo, number), FIX_NOTES)
        else:
            self.move(self.gh, repo, number, "briefed", lane=lane)
        self.actions += 1
        _say("re-briefed failed %s#%d on %s" % (repo, number, lane))

    def _hold_no_reason(self, repo: str, issue: Dict[str, Any]) -> None:
        """Leave a ticket failed for the brain instead of climbing a second
        time for a run that committed nothing and said nothing, once
        (needs-brain says it was reported)."""
        names = _labels(issue)
        if "needs-brain" in names:
            return
        number = int(issue["number"])
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body":
            "Lane %s made no commit and wrote no reason, as a run on this ticket did "
            "before. The factory climbed the model ladder once for that and does not "
            "climb a second time for the same reason: the brain reads the run logs and "
            "rewrites the brief, closes the ticket, or escalates it once more with its "
            "reason." % _lane(issue)})
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": names + ["needs-brain"]})
        self.actions += 1
        _say("%s#%d failed again with no commit and no reason; left for the brain"
             % (repo, number))

    def briefed(self, repo: str, issue: Dict[str, Any]) -> None:
        if self.now < held_until(issue):
            return  # held by its Not before: line; nobody is late yet
        waiting = self.held_by(repo, issue)
        if waiting:
            # Waiting for another ticket is not this one's fault: it is not
            # stalled, so it climbs nothing, and only a long hold is reported.
            self._tell_held(repo, issue, waiting)
            return
        if _lane(issue) in self.limited:
            self._step_aside(repo, issue)
            return
        if not self._spread(repo, issue):
            self._unclaimed(repo, issue)

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
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body":
            "Held %.0f hours by After: waiting for %s. The station never claims a "
            "ticket whose After: tickets are not merged, so this one is not late and "
            "not stuck: the brain checks that %s is still coming, re-briefs this "
            "ticket without it, or closes it." % (
                (self.now - since) / 3600, waiting_for,
                waiting_for if len(waiting) == 1 else "they are")})
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
                and (x != "agy" or agy_takes_header(issue))]

    def _unclaimed(self, repo: str, issue: Dict[str, Any]) -> None:
        """A ticket briefed BRIEFED_STALL with no lane claiming it moves to the
        least-loaded other lane of its tier (or of its rung), with no rung
        marker: it never ran, so it has not failed on any model. With no such
        lane it stays briefed, and the stall is said once per pass."""
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
            _say("%s#%d stalled on lane %s: no other lane of its %s is available"
                 % (repo, number, old, "rung" if rung else "tier"))
            return
        self._move_stalled(repo, issue, pick_lane(lanes, self.load),
                           (self.now - since) / 3600, rung)

    def _move_stalled(self, repo: str, issue: Dict[str, Any], lane: str,
                      hours: float, rung: int) -> None:
        """Relabel a stalled ticket onto `lane` and say why; no rung marker."""
        number, old = int(issue["number"]), _lane(issue)
        kept = [x for x in _labels(issue) if not x.startswith("lane:")] + ["lane:" + lane]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {
            "body": "Not a failure (briefed %.0f hours on lane %s with no claim): the "
                    "factory moved this ticket to lane %s, %s." % (
                        hours, old, lane,
                        "on the same rung" if rung else "in its difficulty tier")})
        self.load[old] = self.load.get(old, 0) - 1
        self.load[lane] = self.load.get(lane, 0) + 1
        self.actions += 1
        _say("moved stalled %s#%d to %s" % (repo, number, lane))

    def _spread(self, repo: str, issue: Dict[str, Any]) -> bool:
        """A ladder ticket waiting behind more than RUNG_BUSY others on its
        lane moves to an untried rung with a free lane. Not a failure; the
        rung it leaves stays on its record. True when it moved."""
        here = current_rung(issue)
        old = _lane(issue)
        if not here or self.load.get(old, 0) <= RUNG_BUSY:
            return False
        comments = self._comments(repo, int(issue["number"]))
        tried, reached = self._tried(issue, comments, failed_here=False)
        if not any(min(self.load.get(lane, 0) for lane in CLIMB[n - 1][0]) < RUNG_BUSY
                   for n in climb_rungs(tried | {here}, reached) if self._rung_lanes(repo, n)):
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

    def _step_aside(self, repo: str, issue: Dict[str, Any]) -> None:
        """Move a ticket off a lane at a limit to the first free lane of its
        difficulty tier (or of a FALLBACK tier when none of its tier is up),
        or a ladder ticket to a free lane of its rung, with no rung marker.
        With none free it stays where it is, for the lane to take once it is
        back."""
        number = int(issue["number"])
        old = _lane(issue)
        rung = current_rung(issue)
        fallback = ""
        if rung:
            lane = next((x for x in self._rung_lanes(repo, rung) if x != old and self._free(x)), None)
        else:
            lane, fallback = self._tier_move(repo, issue, old, allow_remote=False)
        if lane is None:
            _say("%s#%d waits on lane %s, at a limit: no free lane in its %s"
                 % (repo, number, old, "rung" if rung else "tier"))
            return
        body = str(issue.get("body") or "")
        if not rung:
            body = without_model(body)
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
            issue["body"] = body
        kept = [x for x in _labels(issue) if not x.startswith("lane:")] + ["lane:" + lane]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {
            "body": "Not a failure (lane %s is at a limit): the factory moved this "
                    "ticket to lane %s, %s." % (
                        old, lane, "on the same rung" if rung else
                        fallback or "in its difficulty tier")})
        self.load[old] = self.load.get(old, 0) - 1
        self.load[lane] = self.load.get(lane, 0) + 1
        self.actions += 1
        _say("moved %s#%d off lane %s, at a limit, to %s%s" % (
            repo, number, old, lane, " (%s)" % fallback if fallback else ""))

    # -- lane load -------------------------------------------------------
    def count_load(self, tickets: Optional[Dict[str, Dict[int, Dict[str, Any]]]] = None) -> None:
        """Count the briefed and running tickets on each lane, reading every
        repo's tickets unless they are given."""
        if tickets is None:
            tickets = {repo: self._tickets(repo) for repo in self.repos}
        for issue in (i for found in tickets.values() for i in found.values()):
            if (_state(issue) in ("briefed", "running")
                    or (_state(issue) == "queued" and _lane(issue) in REMOTE_LANES
                        and self._up(_lane(issue)))):
                self.load[_lane(issue)] = self.load.get(_lane(issue), 0) + 1

    def _queued_model_lane(self, repo: str, model: str) -> Optional[str]:
        from .station import model_lanes, needs_public_repo
        return next((candidate for candidate in model_lanes(model)
                     if self._free(candidate)
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
        queued = [(repo, issue) for repo, found in found_by_repo.items() for issue in found.values()
                  if _state(issue) == "queued"]
        waits = [self._assign_queued_ticket(repo, issue)
                 for repo, issue in sorted(queued, key=lambda item: tickets.work_order(*item))]
        if waits and all(waits):
            _say("assigned 0 of %d queued: %s" % (len(waits), waits[0]))

    def _assign_queued_ticket(self, repo: str, issue: Dict[str, Any]) -> str:
        """Give one queued ticket a lane: "" when it got one, else why it waits."""
        ref = "%s#%s" % (repo, issue["number"])
        if self.now < held_until(issue):
            return "%s is held" % ref
        model_match = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
        if model_match:
            return self._assign_queued_model(repo, issue, model_match.group(1))
        assigned = _lane(issue)
        if assigned in REMOTE_LANES and not self._is_private(repo) and self._up(assigned):
            # A live remote assignment stays queued for its pull worker.
            return "%s waits for remote lane %s to pull it" % (ref, assigned)
        if assigned in REMOTE_LANES:
            lane, fallback = self._tier_move(repo, issue, old=assigned)
        else:
            lane, fallback = self._tier_move(repo, issue)
        if lane is None:
            return "%s waits: no free lane in tier %s" % (ref, difficulty_of(issue) or "(none)")
        tier = difficulty_of(issue) + (", " + fallback if fallback else "")
        if not self._guarded(self._assign_queued_one, repo, issue, lane, tier):
            return "%s: giving it lane %s failed" % (ref, lane)
        return ""

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
        if lane is None:
            why = "%s waits: no free lane takes model %s" % (ref, model)
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
        self.move(self.gh, repo, number, "briefed", lane=lane, model=model,
                  note="%s\nThe station gave this queued ticket lane %s with model %s in "
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
            self.move(self.gh, repo, number, "queued", lane=lane,
                      note="The station reserved this queued ticket for remote lane %s (tier %s)."
                           % (lane, tier))
            issue["labels"] = [{"name": x} for x in brain._moved_labels(
                _labels(issue), "queued", lane)]
            self.load[lane] = self.load.get(lane, 0) + 1
            _say("reserved queued %s#%d for remote lane %s (tier %s)"
                 % (repo, number, lane, tier))
            return
        self.move(self.gh, repo, number, "briefed", lane=lane,
                  note="The station gave this queued ticket lane %s (tier %s)." %
                       (lane, tier))
        issue["labels"] = [{"name": x} for x in brain._moved_labels(
            _labels(issue), "briefed", lane)]
        self.load[lane] = self.load.get(lane, 0) + 1
        _say("gave queued %s#%d lane %s (tier %s)" % (repo, number, lane, tier))

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
        for repo in self.repos:
            self._repo_pass(repo, tickets[repo])

    def _repo_pass(self, repo: str, tickets: Dict[int, Dict[str, Any]]) -> None:
        # GitHub.paginate asks for 100 a page and follows every page.
        all_prs = [p for p in self.gh.paginate("/repos/%s/pulls?state=open" % repo)
                   if isinstance(p, dict)]
        closed_numbers = self._close_superseded_prs(repo, all_prs, tickets)
        prs = [p for p in all_prs if self._trusted(p) and int(p.get("number") or 0) not in closed_numbers]
        linked = {a[1]: pr for pr in prs for a in [self._association(repo, pr)] if a}
        for pr in landing_order(prs, tickets, linked):
            if self.actions >= MAX_ACTIONS:
                return
            self._guarded(self.pull_request, repo, pr, tickets)
        steps = {"failed": lambda r, i: self.failed(r, i, linked), "briefed": self.briefed}
        for issue in list(tickets.values()):
            step = steps.get(_state(issue))
            if self.actions >= MAX_ACTIONS:
                return
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
