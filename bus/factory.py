"""The factory pass: lane pull requests and stuck tickets move on their own.

The brain reads diffs, plans, briefs and verifies. It must not be the only
thing that moves finished work: on 2026-10-13 it left 29 lane pull requests
unmerged for 4 to 12 hours, because its tick budget ran out before it reached
their review threads, and their tickets filled every goal's cap. This pass
runs from the station at most every FACTORY_CHECK seconds and needs no model:

- A lane pull request whose checks have all finished, whose review check
  (LOOM_REVIEW_CHECK) has run on its head commit, and that is mergeable,
  green and free of unresolved threads is merged with bus.brain.merge (all
  of that function's own checks apply), and its ticket moves to loom:merged.
  Without the review check on its head commit it is never merged: every
  change is reviewed (the operator's rule), so it waits for the brain.
- One with a failing check, an unresolved thread or a conflict goes back to
  a lane as a fix brief (bus.brain.fix). The first send-back of a young pull
  request stays on the ticket's lane. Every later one, and any pull request
  older than STUCK_AGE, climbs the model ladder (docs/MUSE.md).
- Landing keeps the default branch green and nothing stale. A ready pull
  request that is behind its base is brought up to date first (GitHub's
  update-branch), and lands only once its checks and review pass again on
  the new head. Each repo lands at most one pull request a pass, so the
  next one is checked against the base that includes it. Pull requests of
  loom:urgent tickets land first, then the oldest.
- A pull request whose only fault is a failed Actions job gets that job run
  once more before it is sent back, since one flaky run should not cost a
  lane round.
- A failed ticket climbs the ladder, and so does a ticket left briefed for
  BRIEFED_STALL with no lane claiming it.
- Past the last rung the ticket is left failed for the brain, with a note.

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

from . import brain, tickets
from .github import GitHub, GitHubError
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

ANTHROPIC = ("anthropic",) + tuple("anthropic-%d" % n for n in range(2, 9))
# The operator's ladder, 2026-10-12: "sonnet 5.5, or codex sol or opus 5.5
# worst case". Each rung lists the lanes that serve it, best first.
LADDER: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("claude",), "claude-sonnet-5-5"),
    (("codex", "codex-2"), "gpt-6-sol"),
    (("claude",) + ANTHROPIC, "claude-opus-5-5"),
)
# A rung whose least-loaded lane already holds this many briefed or running
# tickets is passed over for the next one (docs/MUSE.md: "a higher rung rather
# than a wait behind a busy lane"). The last rung takes the work regardless.
RUNG_BUSY = 2
RUNG_MARKER = "<!-- loom:rung %d -->"
RUNG_RE = re.compile(r"<!-- loom:rung (\d+) -->")
CLAIM_RE = re.compile(r"^Claimed for lane ")
ACTIONS_RUN_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")
MODEL_LINE = re.compile(r"^Model:[^\n]*\n?", re.M)
GREEN = ("success", "neutral", "skipped")

FIX_NOTES = """The factory sent this pull request back: it has failing checks, open \
review threads or a conflict with the default branch, listed below.

- Bring the branch up to date with the default branch first if it conflicts.
- Fix each finding that is valid against the current code, with a test that \
fails without the fix where the finding is about behavior.
- Where a finding is not valid, change the code or a comment so the reason is \
plain to the next reader, and say why in the commit message.
- Keep the whole suite green, and change nothing the threads and checks do not \
ask for."""


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


def _lane(issue: Dict[str, Any]) -> str:
    return next((x[5:] for x in _labels(issue) if x.startswith("lane:")), "")


def rung_of(comments: Sequence[Dict[str, Any]]) -> int:
    """The highest rung the ticket has climbed to, 0 when none."""
    found = [int(m.group(1)) for c in comments
             for m in RUNG_RE.finditer(str(c.get("body") or ""))]
    return max(found, default=0)


def fixes_of(comments: Sequence[Dict[str, Any]]) -> int:
    return sum(1 for c in comments if str(c.get("body") or "").startswith(FIX_MARKER))


def pick_lane(lanes: Sequence[str], load: Dict[str, int]) -> str:
    """The least-loaded lane of a rung, the first listed on a tie."""
    return min(lanes, key=lambda lane: (load.get(lane, 0), lanes.index(lane)))


def pick_rung(start: int, load: Dict[str, int]) -> int:
    """The first rung from `start` (1-based) with a lane under RUNG_BUSY, else
    the last rung. 0 past the last rung."""
    if start > len(LADDER):
        return 0
    for rung in range(start, len(LADDER)):
        lanes = LADDER[rung - 1][0]
        if min(load.get(lane, 0) for lane in lanes) < RUNG_BUSY:
            return rung
    return len(LADDER)


def held_until(issue: Dict[str, Any]) -> float:
    """The ticket's Not before: time, 0 when it has none or it is unreadable."""
    try:
        return tickets.not_before(tickets.parse(issue.get("body"))[0]) or 0.0
    except ValueError:
        return 0.0


def landing_order(prs: List[Dict[str, Any]], tickets: Dict[int, Dict[str, Any]],
                  linked: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull requests of loom:urgent tickets first, then the oldest."""
    urgent = {n for n, issue in tickets.items() if "loom:urgent" in _labels(issue)}
    ticket_of = {int(pr["number"]): n for n, pr in linked.items()}
    return sorted(prs, key=lambda p: (ticket_of.get(int(p["number"])) not in urgent,
                                      str(p.get("created_at") or "")))


def with_model(body: str, model: str) -> str:
    """The ticket body with its Model: header line set to `model`."""
    head, sep, rest = body.partition("\n\n")
    if MODEL_LINE.search(head + "\n"):
        return MODEL_LINE.sub("Model: %s\n" % model, head + "\n", count=1).rstrip("\n") + sep + rest
    return "Model: %s\n%s" % (model, body)


class Factory:
    def __init__(self, gh: GitHub, repos: Sequence[str], now: float,
                 trusted: Sequence[str] = (),
                 review_check: str = DEFAULT_REVIEW_CHECK,
                 merge: Callable[..., Any] = brain.merge,
                 fix: Callable[..., Any] = brain.fix,
                 move: Callable[..., Any] = brain.move) -> None:
        self.gh = gh
        self.repos = list(repos)
        self.now = now
        self.trusted = {t.lower() for t in trusted}
        self.review_check = review_check
        self.merge, self.fix, self.move = merge, fix, move
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

    def _review_state(self, repo: str, number: int, sha: str, pushed_at: float) -> str:
        """"wait" while any check runs; "reviewed" once the review check has
        run on this commit; else "unreviewed", which never merges."""
        runs = self._runs[sha] = self._check_runs(repo, sha)
        if any(r.get("status") != "completed" for r in runs):
            return "wait"
        if any(r.get("name") == self.review_check for r in runs):
            return "reviewed"
        if self.now - pushed_at > REVIEW_GRACE:
            _say("%s#%d has no %s on its head commit; it waits for the brain"
                 % (repo, number, self.review_check))
        return "unreviewed"

    # -- writes ----------------------------------------------------------
    def _climb(self, repo: str, issue: Dict[str, Any], rung: int, why: str) -> Optional[str]:
        """Put the ticket on `rung` (1-based): its Model: line and lane label,
        and a rung marker. Returns the lane, or None past the last rung."""
        number = int(issue["number"])
        rung = pick_rung(rung, self.load)
        if not rung:
            return None
        lanes, model = LADDER[rung - 1]
        lane = pick_lane(lanes, self.load)
        body = with_model(str(issue.get("body") or ""), model)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
        kept = [x for x in _labels(issue) if not x.startswith("lane:")] + ["lane:" + lane]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        issue["labels"] = [{"name": x} for x in kept]
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {
            "body": "%s\nRepeatedly stuck (%s): the factory moved this ticket up the "
                    "model ladder to rung %d, %s on lane %s." % (
                        RUNG_MARKER % rung, why, rung, model, lane)})
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
                   comments: List[Dict[str, Any]], why: str) -> None:
        number = int(issue["number"])
        age = self.now - _ts(pr.get("created_at"))
        if fixes_of(comments) >= 1 or age > STUCK_AGE:
            rung = rung_of(comments) + 1
            if self._climb(repo, issue, rung, why) is None:
                self._give_up(repo, issue, why)
                return
        self.fix(self.gh, repo, int(pr["number"]), (repo, number), FIX_NOTES)
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
        review = self._review_state(repo, number, sha, _ts(full.get("updated_at")))
        if review == "wait":
            return
        details = brain._pr_graphql(self.gh, repo, number)
        faults = [w for bad, w in ((not brain._checks_green(details), "failing checks"),
                                   (not brain._threads_resolved(details), "open review threads"),
                                   (full.get("mergeable") is False, "a conflict")) if bad]
        if faults == ["failing checks"] and self._run_again(repo, number, sha):
            return
        if faults:
            self._send_back(repo, full, issue, self._comments(repo, int(issue["number"])),
                            ", ".join(faults))
        elif review == "reviewed":
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
        rung = rung_of(comments) + 1
        if rung > len(LADDER):
            return  # left for the brain; _give_up said so once
        lane = self._climb(repo, issue, rung, "it failed on lane %s" % _lane(issue))
        if lane is None:
            return
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
        number = int(issue["number"])
        hold = held_until(issue)
        if self.now < hold:
            return  # held by its Not before: line; nobody is late yet
        events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, number))
        since = max((_ts(e.get("created_at")) for e in events if isinstance(e, dict)
                     and e.get("event") == "labeled"
                     and (e.get("label") or {}).get("name") == "loom:briefed"), default=0.0)
        since = max(since, hold) if since else since
        if not since or self.now - since < BRIEFED_STALL:
            return
        comments = self._comments(repo, number)
        if any(CLAIM_RE.match(str(c.get("body") or "")) and _ts(c.get("created_at")) >= since
               for c in comments):
            return
        hours = (self.now - since) / 3600
        rung = rung_of(comments) + 1
        why = "briefed %.0f hours on lane %s with no claim" % (hours, _lane(issue))
        lane = self._climb(repo, issue, rung, why)
        if lane is None:
            self._give_up(repo, issue, why)
            return
        self.actions += 1
        _say("moved stalled %s#%d to %s" % (repo, number, lane))

    # -- the pass --------------------------------------------------------
    def run(self) -> None:
        tickets = {repo: self._tickets(repo) for repo in self.repos}
        for issue in (i for found in tickets.values() for i in found.values()):
            if _state(issue) in ("briefed", "running"):
                self.load[_lane(issue)] = self.load.get(_lane(issue), 0) + 1
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
