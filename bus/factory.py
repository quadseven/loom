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
- One with a failing check, an unresolved thread or a conflict goes back to
  a lane as a fix brief (bus.brain.fix). The first send-back of a young pull
  request stays on the ticket's lane. Every later one, and any pull request
  older than STUCK_AGE, climbs the model ladder (docs/MUSE.md).
- A failed ticket climbs the ladder, and so does a ticket left briefed for
  BRIEFED_STALL with no lane claiming it.
- Past the last rung the ticket is left failed for the brain, with a note.

Each climb is recorded on the ticket as a RUNG_MARKER comment, so the ladder
survives restarts and any brain can read where a ticket stands.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import brain
from .github import GitHub, GitHubError
from .tickets import FIX_MARKER

FACTORY_CHECK = 300
STUCK_AGE = 6 * 3600
BRIEFED_STALL = 2 * 3600
# A head commit with no review check this long after its push is treated as
# reviewed, so a repo without the review bot is not held forever.
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
RUNG_MARKER = "<!-- loom:rung %d -->"
RUNG_RE = re.compile(r"<!-- loom:rung (\d+) -->")
CLAIM_RE = re.compile(r"^Claimed for lane ")
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


def with_model(body: str, model: str) -> str:
    """The ticket body with its Model: header line set to `model`."""
    head, sep, rest = body.partition("\n\n")
    if MODEL_LINE.search(head + "\n"):
        return MODEL_LINE.sub("Model: %s\n" % model, head + "\n", count=1).rstrip("\n") + sep + rest
    return "Model: %s\n%s" % (model, body)


class Factory:
    def __init__(self, gh: GitHub, repos: Sequence[str], now: float,
                 review_check: str = DEFAULT_REVIEW_CHECK,
                 merge: Callable[..., Any] = brain.merge,
                 fix: Callable[..., Any] = brain.fix,
                 move: Callable[..., Any] = brain.move) -> None:
        self.gh = gh
        self.repos = list(repos)
        self.now = now
        self.review_check = review_check
        self.merge, self.fix, self.move = merge, fix, move
        self.actions = 0
        self.load: Dict[str, int] = {}

    # -- reads -----------------------------------------------------------
    def _tickets(self, repo: str) -> Dict[int, Dict[str, Any]]:
        data = self.gh.paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo)
        return {int(i["number"]): i for i in data
                if isinstance(i, dict) and "pull_request" not in i and "number" in i}

    def _comments(self, repo: str, number: int) -> List[Dict[str, Any]]:
        data = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        return [c for c in data if isinstance(c, dict)]

    def _review_state(self, repo: str, sha: str, pushed_at: float) -> str:
        """"wait" while any check runs or the review has not run on this
        commit; else "done"."""
        data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100"
                            % (repo, sha)).data or {}
        runs = [r for r in data.get("check_runs") or [] if isinstance(r, dict)]
        if any(r.get("status") != "completed" for r in runs):
            return "wait"
        if any(r.get("name") == self.review_check for r in runs):
            return "done"
        return "done" if self.now - pushed_at > REVIEW_GRACE else "wait"

    # -- writes ----------------------------------------------------------
    def _climb(self, repo: str, issue: Dict[str, Any], rung: int, why: str) -> Optional[str]:
        """Put the ticket on `rung` (1-based): its Model: line and lane label,
        and a rung marker. Returns the lane, or None past the last rung."""
        number = int(issue["number"])
        if rung > len(LADDER):
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
    def pull_request(self, repo: str, pr: Dict[str, Any],
                     tickets: Dict[int, Dict[str, Any]]) -> None:
        if pr.get("draft") or not str((pr.get("head") or {}).get("ref") or "").startswith("loom/"):
            return
        try:
            association = brain._ticket_association(repo, pr)
        except ValueError:
            return
        issue = tickets.get(association[1]) if association else None
        if issue is None or _state(issue) != "pr":
            return
        number = int(pr["number"])
        full = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data or {}
        sha = str((full.get("head") or {}).get("sha") or "")
        if not sha or full.get("mergeable") is None:
            return
        if self._review_state(repo, sha, _ts(full.get("updated_at"))) == "wait":
            return
        details = brain._pr_graphql(self.gh, repo, number)
        green = brain._checks_green(details)
        resolved = brain._threads_resolved(details)
        if full.get("mergeable") is True and green and resolved:
            try:
                self.merge(self.gh, repo, number, (repo, int(issue["number"])))
            except (ValueError, GitHubError) as err:
                _say("did not merge %s#%d: %s" % (repo, number, err))
                return
            self.actions += 1
            _say("merged %s#%d for #%d" % (repo, number, issue["number"]))
            return
        why = ", ".join(w for ok, w in ((not green, "failing checks"),
                                        (not resolved, "open review threads"),
                                        (full.get("mergeable") is False, "a conflict"))
                        if ok) or "not mergeable"
        self._send_back(repo, full, issue, self._comments(repo, int(issue["number"])), why)

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
        events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, number))
        since = max((_ts(e.get("created_at")) for e in events if isinstance(e, dict)
                     and e.get("event") == "labeled"
                     and (e.get("label") or {}).get("name") == "loom:briefed"), default=0.0)
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
        tickets = {}
        for repo in self.repos:
            tickets[repo] = self._tickets(repo)
            for issue in tickets[repo].values():
                if _state(issue) in ("briefed", "running"):
                    self.load[_lane(issue)] = self.load.get(_lane(issue), 0) + 1
        for repo in self.repos:
            prs = [p for p in self.gh.paginate("/repos/%s/pulls?state=open" % repo)
                   if isinstance(p, dict)]
            linked: Dict[int, Dict[str, Any]] = {}
            for pr in prs:
                try:
                    association = brain._ticket_association(repo, pr)
                except ValueError:
                    continue
                if association:
                    linked[association[1]] = pr
            for pr in prs:
                if self.actions >= MAX_ACTIONS:
                    return
                self._guarded(self.pull_request, repo, pr, tickets[repo])
            for issue in list(tickets[repo].values()):
                if self.actions >= MAX_ACTIONS:
                    return
                state = _state(issue)
                if state == "failed":
                    self._guarded(self.failed, repo, issue, linked)
                elif state == "briefed":
                    self._guarded(self.briefed, repo, issue)

    @staticmethod
    def _guarded(step: Callable[..., None], repo: str, item: Dict[str, Any], *rest: Any) -> None:
        """One item's failure is logged and never stops the others."""
        try:
            step(repo, item, *rest)
        except (GitHubError, ValueError, KeyError, TypeError) as err:
            _say("%s on %s#%s failed: %s" % (step.__name__, repo, item.get("number"), err))


def due(state_dir: str, now: float) -> bool:
    """True at most every FACTORY_CHECK seconds; records the run."""
    path = os.path.join(state_dir, "factory.json")
    try:
        with open(path) as fh:
            last = float(json.load(fh).get("ran", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        last = 0.0
    if 0 <= now - last < FACTORY_CHECK:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"ran": now}, fh)
    os.replace(tmp, path)
    return True
