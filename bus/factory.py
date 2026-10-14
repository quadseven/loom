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
  loom:urgent and loom:regression tickets land first, then the oldest.
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
  is never merged here: it is marked needs-brain and left for the brain.
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
- A failed ticket climbs the ladder, and so does a ticket left briefed for
  BRIEFED_STALL with no lane claiming it.
- Each pass gives the oldest queued tickets free lanes in their difficulty
  tier, leaving held tickets and tickets without a readable tier queued.
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
from .tickets import FIX_MARKER

FACTORY_CHECK = 300
STUCK_AGE = 6 * 3600
BRIEFED_STALL = 2 * 3600
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
TIERS: Dict[str, Tuple[str, ...]] = {
    # The operator's priority (2026-10-14): Muse and agy first on public
    # repos, so their allowances are spent before the rest. Both are public
    # only, so a private repo's ticket still starts at spark or codex.
    "easy": ("muse", "agy", "spark", "spark-2", "opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4",
             "openrouter", "workersai", "zen", "zai"),
    "medium": ("muse", "agy", "codex", "codex-2", "opencode-go", "opencode-go-2", "opencode-go-3",
               "opencode-go-4") + ANTHROPIC,
    "hard": ANTHROPIC + ("claude", "codex", "codex-2"),
}
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
# bus/station.py's note when a fix round ended without a commit.
NO_CHANGE_RE = re.compile(r"^Lane \S+ made no change for the fix")
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
FIRST = ("loom:urgent", "loom:regression", REVERT_LABEL)

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
    newest rung 0 marker). A rung skipped because its lanes were busy is not
    tried: on 2026-10-13 a restart wave filled the claude and codex lanes,
    tickets spilled straight to rung 3 and gave up after one more round."""
    tried: set = set()
    for c in comments:
        for m in RUNG_RE.finditer(str(c.get("body") or "")):
            rung = int(m.group(1))
            tried = set() if rung == 0 else tried | {rung}
    return tried


def current_rung(issue: Dict[str, Any]) -> int:
    """The rung whose model and lane the ticket runs on now, 0 when none: a
    brief can start on a ladder model, and failing there tries that rung."""
    m = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
    model = m.group(1) if m else ""
    return next((n for n, (lanes, rung_model) in enumerate(LADDER, 1)
                 if rung_model == model and _lane(issue) in lanes), 0)


def next_rung(tried: set, load: Dict[str, int], eligible: Optional[set] = None) -> int:
    """The lowest untried rung with a lane under RUNG_BUSY (docs/MUSE.md: a
    higher rung rather than a wait behind a busy lane), else the untried rung
    whose least-loaded lane holds the least, to wait there. 0 once every rung
    was tried. Waiting on the lowest rung instead queued 33 tickets on the
    one claude lane on 2026-10-13 while six other ladder slots sat idle."""
    untried = [n for n in range(1, len(LADDER) + 1)
               if n not in tried and (eligible is None or n in eligible)]
    if not untried:
        return 0
    least = {n: min(load.get(lane, 0) for lane in LADDER[n - 1][0]) for n in untried}
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
    first, then the oldest."""
    urgent = {n for n, issue in tickets.items() if set(FIRST) & set(_labels(issue))}
    ticket_of = {int(pr["number"]): n for n, pr in linked.items()}
    return sorted(prs, key=lambda p: (ticket_of.get(int(p["number"])) not in urgent,
                                      str(p.get("created_at") or "")))


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


class Factory:
    def __init__(self, gh: GitHub, repos: Sequence[str], now: float,
                 trusted: Sequence[str] = (),
                 review_check: str = DEFAULT_REVIEW_CHECK,
                 review_bot: str = DEFAULT_REVIEW_BOT,
                 merge: Callable[..., Any] = brain.merge,
                 fix: Callable[..., Any] = brain.fix,
                 move: Callable[..., Any] = brain.move,
                 limited: Sequence[str] = (),
                 no_test_repos: Sequence[str] = (),
                 available: Optional[Sequence[str]] = None) -> None:
        self.gh = gh
        self.repos = list(repos)
        self.now = now
        self.trusted = {t.lower() for t in trusted}
        self.review_check = review_check
        self.review_bot = review_bot
        self.merge, self.fix, self.move = merge, fix, move
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

    # -- reads -----------------------------------------------------------
    def _trusted(self, item: Dict[str, Any]) -> bool:
        login = str((item.get("user") or {}).get("login") or "").lower()
        return bool(login) and login in self.trusted

    def _tickets(self, repo: str) -> Dict[int, Dict[str, Any]]:
        data = self.gh.paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo)
        return {int(i["number"]): i for i in data
                if isinstance(i, dict) and "pull_request" not in i and "number" in i
                and self._trusted(i)}

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
        own = [x for x in lanes_of(difficulty, private)
               if x != old and (allow_remote or x not in REMOTE_LANES)]
        lane = next((x for x in own if self._free(x)), None)
        if lane is not None or any(self._up(x) for x in own):
            return lane, ""
        for tier in FALLBACK.get(difficulty, ()):
            fallback_lanes = [x for x in lanes_of(tier, private)
                              if x != old and (allow_remote or x not in REMOTE_LANES)]
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

    def _climb(self, repo: str, issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
               why: str, head: str = "Repeatedly stuck", failed_here: bool = True,
               skip: int = 0) -> Optional[str]:
        """Put the ticket on its next rung: its Model: line and lane label, and
        a rung marker. The rung it ran on counts as tried when it failed
        there. Returns the lane, or None once every rung was tried."""
        number = int(issue["number"])
        tried = tried_rungs(comments)
        if failed_here:
            tried |= {current_rung(issue)} - {0}
        available_lanes = {lane for lanes, _ in LADDER for lane in lanes
                           if lane not in self.limited
                           and (self.available is None or lane in self.available)}
        eligible = {n for n, (lanes, _) in enumerate(LADDER, 1)
                    if any(lane in available_lanes for lane in lanes)}
        rung = next_rung(tried | ({skip} - {0}), self.load, eligible)
        if not rung:
            return None
        lanes, model = LADDER[rung - 1]
        lanes = tuple(lane for lane in lanes if lane in available_lanes)
        if not lanes:
            return None
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

    def _give_up(self, repo: str, issue: Dict[str, Any], why: str) -> None:
        number = int(issue["number"])
        if _state(issue) != "failed":
            self.move(self.gh, repo, number, "failed",
                      note="The factory tried every rung of the model ladder (%s). "
                           "The brain rewrites this brief smaller, or asks the "
                           "operator one exact question." % why)
        _say("%s#%d is past the last rung; left failed for the brain" % (repo, number))

    def _send_back(self, repo: str, pr: Dict[str, Any], issue: Dict[str, Any],
                   comments: List[Dict[str, Any]], why: str,
                   open_threads: Sequence[str] = (), checks_failing: bool = False,
                   no_test: bool = False) -> None:
        number = int(issue["number"])
        age = self.now - _ts(pr.get("created_at"))
        if stuck(comments, open_threads, checks_failing, age, self.now, no_test):
            if self._climb(repo, issue, comments, why) is None:
                self._give_up(repo, issue, why)
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
    def _lane_pr_ticket(self, repo: str, pr: Dict[str, Any],
                        tickets: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The loom:pr ticket of a trusted, non-draft lane pull request."""
        if pr.get("draft") or not self._trusted(pr):
            return None
        if not str((pr.get("head") or {}).get("ref") or "").startswith("loom/"):
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
        if class_of(full) == "sensitive":
            # A change to the code that merges, closes or deploys never lands
            # on the factory's say-so alone: mark it for the brain and stop.
            self._mark_sensitive(repo, number, full)
            return
        review = self._review_state(repo, number, sha, _ts(full.get("updated_at")))
        if review == "wait":
            return
        comments = self._comments(repo, int(issue["number"]))
        ended = no_change_at(comments)
        if (ended and not self._reviewed_since(sha, ended)
                and self.now - ended <= REVIEW_GRACE):
            return  # the review the lane asked for has not answered yet
        self._judge(repo, number, full, issue, comments, review)

    def _judge(self, repo: str, number: int, full: Dict[str, Any], issue: Dict[str, Any],
               comments: List[Dict[str, Any]], review: str) -> None:
        """Land, defer minor findings and land, rerun a flaky job, or send back."""
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
            self._land_or_ask_test(repo, number, full, issue, comments)

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
                          issue: Dict[str, Any], comments: List[Dict[str, Any]]) -> None:
        """Land a clean, reviewed pull request, or send it back when it
        changes code without a test."""
        if self._needs_test(repo, number, issue):
            self._send_back(repo, full, issue, comments,
                            "code changed without a test", no_test=True)
        else:
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
        lane = self._climb(repo, issue, comments, "it failed on lane %s" % _lane(issue))
        if lane is None:
            return  # left for the brain; _give_up said so once
        pr = prs.get(number)
        if pr is not None:
            # Back to loom:pr so the fix brief reuses its open pull request.
            kept = [x for x in _labels(issue) if x != "loom:failed"] + ["loom:pr"]
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
            self.fix(self.gh, repo, int(pr["number"]), (repo, number), FIX_NOTES)
        else:
            self.move(self.gh, repo, number, "briefed", lane=lane)
        self.actions += 1
        _say("re-briefed failed %s#%d on %s" % (repo, number, lane))

    def briefed(self, repo: str, issue: Dict[str, Any]) -> None:
        if self.now < held_until(issue):
            return  # held by its Not before: line; nobody is late yet
        if _lane(issue) in self.limited:
            self._step_aside(repo, issue)
            return
        if not self._spread(repo, issue):
            self._unclaimed(repo, issue)

    def _briefed_since(self, repo: str, issue: Dict[str, Any]) -> float:
        """When the ticket was last labeled briefed, or its hold ended if
        later; 0 when it never was."""
        events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, int(issue["number"])))
        since = max((_ts(e.get("created_at")) for e in events if isinstance(e, dict)
                     and e.get("event") == "labeled"
                     and (e.get("label") or {}).get("name") == "loom:briefed"), default=0.0)
        return max(since, held_until(issue)) if since else since

    def _unclaimed(self, repo: str, issue: Dict[str, Any]) -> None:
        """A ticket briefed BRIEFED_STALL with no lane claiming it climbs."""
        number = int(issue["number"])
        since = self._briefed_since(repo, issue)
        if not since or self.now - since < BRIEFED_STALL:
            return
        comments = self._comments(repo, number)
        if any(CLAIM_RE.match(str(c.get("body") or "")) and _ts(c.get("created_at")) >= since
               for c in comments):
            return
        hours = (self.now - since) / 3600
        why = "briefed %.0f hours on lane %s with no claim" % (hours, _lane(issue))
        lane = self._climb(repo, issue, comments, why, failed_here=False)
        if lane is None:
            self._give_up(repo, issue, why)
            return
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
        tried = tried_rungs(comments) | {here}
        if not any(min(self.load.get(lane, 0) for lane in LADDER[n - 1][0]) < RUNG_BUSY
                   for n in range(1, len(LADDER) + 1) if n not in tried):
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
            lane = next((x for x in LADDER[rung - 1][0] if x != old and self._free(x)), None)
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

    def assign_queued(self) -> None:
        """Give queued tickets free tier lanes (or FALLBACK lanes when none of
        the tier is up), oldest first across repos."""
        found_by_repo = {repo: self._tickets(repo) for repo in self.repos}
        self.count_load(found_by_repo)
        queued = [(repo, issue) for repo, found in found_by_repo.items() for issue in found.values()
                  if _state(issue) == "queued"]
        for repo, issue in sorted(queued, key=lambda item: (
                str(item[1].get("created_at") or ""), item[0], int(item[1]["number"]))):
            if self.now < held_until(issue):
                continue
            model_match = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
            if model_match:
                model = model_match.group(1)
                lane = self._queued_model_lane(repo, model)
                if lane is None:
                    _say("%s#%s waits: no free lane takes model %s"
                         % (repo, issue["number"], model))
                    continue
                self._guarded(self._assign_queued_one, repo, issue, lane,
                              "model %s" % model)
                continue
            assigned = _lane(issue)
            if assigned in REMOTE_LANES and not self._is_private(repo) and self._up(assigned):
                continue  # A live remote assignment stays queued for its pull worker.
            if assigned in REMOTE_LANES:
                lane, fallback = self._tier_move(repo, issue, old=assigned)
            else:
                lane, fallback = self._tier_move(repo, issue)
            if lane is None:
                continue
            tier = difficulty_of(issue) + (", " + fallback if fallback else "")
            self._guarded(self._assign_queued_one, repo, issue, lane, tier)

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
        prs = [p for p in self.gh.paginate("/repos/%s/pulls?state=open" % repo)
               if isinstance(p, dict) and self._trusted(p)]
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
    def _guarded(step: Callable[..., None], repo: str, item: Dict[str, Any], *rest: Any) -> None:
        """One item's failure is logged and never stops the others."""
        try:
            step(repo, item, *rest)
        except (GitHubError, ValueError, KeyError, TypeError) as err:
            _say("%s on %s#%s failed: %s" % (getattr(step, "__name__", "step"), repo,
                                             item.get("number"), err))


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
