"""The revert pass: a default branch a factory merge turned red is reverted.

Loom has no merge queue and no global lock: a lane pull request lands as
soon as it is green and reviewed on its own. Two changes that are each green
can still break the default branch together, and without this pass a red
branch stays red until a person or a brain looks (north star 5: no
regression stays shipped more than 2 hours unfixed or unreverted). Each
station pass, for each repo with a lane pull request merged in the last
RECENT seconds, it reads the check runs of the default branch's newest
commits, from the head back to the last green one (the API only, no
checkout, one check-run page per commit per pass):

- A head that is green, or whose newest result is still running, needs
  nothing. A commit with no check runs yet, younger than NO_RUNS_WAIT, is
  waited on; an older one (CI never ran, or every run was cancelled) is
  skipped as untested.
- A red head is bisected: the first red commit after the last green one is
  the change that broke the branch. A commit still running before it is
  waited on, since it may be the one.
- That commit is reverted only when it is a lane pull request's merge whose
  head was green, it has been red for RED_HOLD (a newer commit may turn the
  branch green first), no other revert is in flight in the repo, and no
  revert ticket names it yet. A red commit nobody's lane merged is reported
  once and left for a person or the brain.
- The revert is a ticket (REVERT_LABEL, class logic, rung 0) on the first
  free lane of the easy tier the repo may use, or of the next tier up when
  no easy lane is up (any model can revert): "Revert <sha>: <title>",
  whose What is `git revert --no-edit <sha>` and whose acceptance criterion
  is the failing check passing. The factory lands it first, on green,
  deferring its medium and low findings.
- The reverted ticket gets a comment naming the red check and the revert
  ticket. Once the revert lands it goes back to loom:briefed on its lane,
  to redo the change on the new default branch; before that, its lane would
  find the change still there.

Each decision is one log line, said again only when it changes.
lanes/mac/station.sh does not deploy a commit an in-flight revert names.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import brain
from .factory import GREEN, REVERT_LABEL, Factory, _labels, _lane, _state, _ts
from .github import GitHub, GitHubError
from .tickets import DIFFICULTIES

# A repo is watched while a lane pull request merged into it this recently.
RECENT = 6 * 3600
# A red commit is reverted once it has been red this long with no newer
# commit turning the branch green.
RED_HOLD = 30 * 60
# A commit with no check runs is waited on this long, then skipped.
NO_RUNS_WAIT = 30 * 60
# The default branch's newest commits read per pass (one page).
COMMITS = 30
CLASS_LABEL = "class:logic"
RED = ("failure", "timed_out", "action_required", "startup_failure")
IN_FLIGHT = ("queued", "briefed", "running", "pr", "failed")
LANDED = ("merged", "shipped", "verified")
SHA_RE = re.compile(r"git revert --no-edit ([0-9a-f]{40})\b")
REVERTED_RE = re.compile(r"^Reverted ticket: #(\d+)$", re.M)
REDO_MARKER = "<!-- loom:redo %d -->"
# Commits briefed for a revert, kept locally too: a revert filed this pass
# must not be filed again if the label list lags behind it.
BRIEFED_KEEP = 50

BODY = """Difficulty: %(difficulty)s

## Why
The default branch `%(branch)s` turned red at %(short)s, the merge of #%(pr)d%(for_ticket)s, \
and stayed red for %(minutes)d minutes: %(checks)s on it, while its pull request head was \
green. Two changes that were each green broke the branch together. Reverting it makes the \
default branch green again.%(redo)s

## What
- Start from the default branch and run `git revert --no-edit %(sha)s`.
- Change nothing else: no fix, no new test.

## Acceptance criteria
%(criteria)s

## Size
XS

## Out of scope
- Fixing or redoing the reverted change.

## Revert
Class: logic
%(reverted)sTests: none needed (a revert; the failing check is its test)
"""


def _say(message: str) -> None:
    print("revert: %s" % message, flush=True)


def latest_runs(runs: Sequence[Dict[str, Any]], skip: str = "") -> List[Dict[str, Any]]:
    """The newest run of each check name (a re-run replaces the run before
    it), without the review check `skip`, which reviews and does not test."""
    newest: Dict[str, Dict[str, Any]] = {}
    for run in runs:
        name = str(run.get("name") or "")
        if not name or name == skip:
            continue
        old = newest.get(name)
        if old is None or int(run.get("id") or 0) >= int(old.get("id") or 0):
            newest[name] = run
    return list(newest.values())


def commit_state(runs: Sequence[Dict[str, Any]], age: float, skip: str = "") -> str:
    """"red" when a check failed, "pending" while one runs or none has
    started within NO_RUNS_WAIT, "green" when every check passed, and
    "none" for a commit CI did not test (no runs since, or only cancelled)."""
    latest = latest_runs(runs, skip)
    done = [r for r in latest if r.get("status") == "completed"]
    if any(r.get("conclusion") in RED for r in done):
        return "red"
    if len(done) < len(latest):
        return "pending"
    if any(r.get("conclusion") in GREEN for r in done):
        return "green"
    return "pending" if not latest and age < NO_RUNS_WAIT else "none"


def red_checks(runs: Sequence[Dict[str, Any]], skip: str = "") -> List[Dict[str, Any]]:
    return [r for r in latest_runs(runs, skip)
            if r.get("status") == "completed" and r.get("conclusion") in RED]


def bisect(states: Sequence[Tuple[str, str]]) -> Tuple[str, Optional[str]]:
    """Over (sha, state) pairs, newest first: ("green", None) when the
    newest tested commit is green or none was tested, ("running", sha) while
    the newest one runs, ("red", sha) with the first red commit after the
    last green one, ("wait", sha) when a commit before that red one, which
    could be the culprit, still runs, or ("unknown", None) when no green
    commit was read."""
    tested = [(sha, s) for sha, s in states if s != "none"]
    if not tested or tested[0][1] == "green":
        return "green", None
    if tested[0][1] == "pending":
        return "running", tested[0][0]
    since = next((i for i, (_, s) in enumerate(tested) if s == "green"), None)
    if since is None:
        return "unknown", None
    after = list(reversed(tested[:since]))
    first = after[0]
    return ("red", first[0]) if first[1] == "red" else ("wait", first[0])


def revert_shas(issues: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """sha -> the revert ticket naming it."""
    found: Dict[str, Dict[str, Any]] = {}
    for issue in issues:
        m = SHA_RE.search(str(issue.get("body") or ""))
        if m:
            found.setdefault(m.group(1), issue)
    return found


def in_flight(issue: Dict[str, Any]) -> bool:
    return issue.get("state") == "open" and _state(issue) in IN_FLIGHT


class Reverts:
    def __init__(self, gh: GitHub, repos: Sequence[str], now: float,
                 trusted: Sequence[str] = (),
                 review_check: str = "",
                 lanes: Optional[Callable[[], Factory]] = None,
                 state_dir: str = "",
                 move: Callable[..., Any] = brain.move) -> None:
        self.gh = gh
        self.repos = list(repos)
        self.now = now
        self.trusted = {t.lower() for t in trusted}
        self.review_check = review_check
        # Builds the factory whose lane load picks the revert's lane; only
        # called when a revert is briefed.
        self.lanes = lanes or (lambda: Factory(gh, self.repos, now, trusted=list(trusted)))
        self._factory: Optional[Factory] = None
        self.state_dir = state_dir
        self.move = move
        self._runs: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._said: Dict[str, str] = {}
        self._briefed: List[str] = []
        self._load()

    # -- the decision ledger ---------------------------------------------
    def _path(self) -> str:
        return os.path.join(self.state_dir, "reverts.json")

    def _load(self) -> None:
        if not self.state_dir:
            return
        try:
            with open(self._path()) as fh:
                kept = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(kept, dict):
            return
        said, briefed = kept.get("said"), kept.get("briefed")
        if isinstance(said, dict):
            self._said = {str(k): str(v) for k, v in said.items()}
        if isinstance(briefed, list):
            self._briefed = [str(x) for x in briefed]

    def _save(self) -> None:
        if not self.state_dir:
            return
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self._path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"said": self._said, "briefed": self._briefed[-BRIEFED_KEEP:]}, fh)
        os.replace(tmp, self._path())

    def _decide(self, repo: str, line: str) -> None:
        """Log the repo's decision once; the same decision next pass is
        not said again."""
        if self._said.get(repo) != line:
            _say("%s: %s" % (repo, line))
        self._said[repo] = line

    # -- reads -----------------------------------------------------------
    def _trusted(self, item: Dict[str, Any]) -> bool:
        login = str((item.get("user") or {}).get("login") or "").lower()
        return bool(login) and login in self.trusted

    def _merged(self, repo: str) -> List[Dict[str, Any]]:
        """Trusted lane pull requests merged, from the newest page of closed
        ones."""
        data = self.gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated&direction=desc"
                                   "&per_page=100" % repo).data
        return [p for p in (data if isinstance(data, list) else [])
                if isinstance(p, dict) and p.get("merged_at") and p.get("merge_commit_sha")
                and self._trusted(p)
                and str((p.get("head") or {}).get("ref") or "").startswith("loom/")]

    def _commits(self, repo: str, branch: str) -> List[Dict[str, Any]]:
        data = self.gh.rest("GET", "/repos/%s/commits?sha=%s&per_page=%d"
                            % (repo, quote(branch, safe=""), COMMITS)).data
        return [c for c in (data if isinstance(data, list) else [])
                if isinstance(c, dict) and c.get("sha")]

    def _check_runs(self, repo: str, sha: str) -> List[Dict[str, Any]]:
        """The commit's check runs: one page, read at most once a pass."""
        key = (repo, sha)
        if key not in self._runs:
            data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100"
                                % (repo, sha)).data or {}
            self._runs[key] = [r for r in data.get("check_runs") or [] if isinstance(r, dict)]
        return self._runs[key]

    def _reverts(self, repo: str) -> List[Dict[str, Any]]:
        data = self.gh.paginate("/repos/%s/issues?labels=%s&state=all" % (repo, REVERT_LABEL))
        return [i for i in data if isinstance(i, dict) and "pull_request" not in i
                and self._trusted(i)]

    # -- the pass --------------------------------------------------------
    def run(self) -> None:
        for repo in self.repos:
            try:
                self._repo(repo)
            except (GitHubError, ValueError, KeyError, TypeError) as err:
                _say("%s failed: %s" % (repo, err))
        try:
            self._save()
        except OSError as err:
            _say("could not keep the revert decisions: %s" % err)

    def _repo(self, repo: str) -> None:
        merged = self._merged(repo)
        if not any(self.now - _ts(p.get("merged_at")) <= RECENT for p in merged):
            self._said.pop(repo, None)
            return
        reverts = self._reverts(repo)
        for issue in reverts:
            try:
                self._redo(repo, issue)
            except (GitHubError, ValueError, KeyError, TypeError) as err:
                _say("%s: re-briefing the ticket revert #%s reverted failed: %s"
                     % (repo, issue.get("number"), err))
        branch = str((self.gh.rest("GET", "/repos/%s" % repo).data or {}).get("default_branch") or "")
        if not branch:
            return
        commits = self._commits(repo, branch)
        states: List[Tuple[str, str]] = []
        for c in commits:
            age = self.now - _ts(((c.get("commit") or {}).get("committer") or {}).get("date"))
            state = commit_state(self._check_runs(repo, c["sha"]), age, self.review_check)
            states.append((c["sha"], state))
            # The last green one bounds the bisect; a head still running
            # is not judged yet.
            if state == "green" or (state == "pending"
                                    and all(s == "none" for _, s in states[:-1])):
                break
        verdict, sha = bisect(states)
        if verdict in ("green", "running"):
            self._said.pop(repo, None)
        elif verdict == "wait":
            self._decide(repo, "%s is red; waiting on the checks of %s, which may "
                               "have turned it" % (branch, (sha or "")[:12]))
        elif verdict == "unknown":
            self._decide(repo, "%s is red and none of its last %d commits is green; "
                               "nothing to bisect" % (branch, len(commits)))
        else:
            assert sha is not None
            by_sha = {str(p["merge_commit_sha"]): p for p in merged
                      if str((p.get("base") or {}).get("ref") or "") == branch}
            self._red(repo, branch, sha, by_sha.get(sha), reverts)

    def _red(self, repo: str, branch: str, sha: str, pr: Optional[Dict[str, Any]],
             reverts: List[Dict[str, Any]]) -> None:
        short = sha[:12]
        if pr is None:
            self._decide(repo, "%s is red from %s, which no lane merged; nothing "
                               "reverted, left for a person or the brain" % (branch, short))
            return
        failing = red_checks(self._check_runs(repo, sha), self.review_check)
        names = sorted({str(r.get("name")) for r in failing})
        open_ = [i for i in reverts if in_flight(i)]
        named = revert_shas(reverts).get(sha)
        red_since = min((_ts(r.get("completed_at")) for r in failing), default=self.now)
        if named is not None or sha in self._briefed:
            self._decide(repo, "%s is red from %s; revert #%s already names it"
                         % (branch, short, (named or {}).get("number", "?")))
        elif open_:
            self._decide(repo, "%s is red from %s; it waits for revert #%s, still in flight"
                         % (branch, short, open_[0].get("number")))
        elif self.now - red_since < RED_HOLD:
            self._decide(repo, "%s is red from %s (#%d) for %d minutes; a newer commit may "
                               "turn it green first" % (branch, short, int(pr["number"]),
                                                       (self.now - red_since) // 60))
        elif self._pr_head_state(repo, pr) != "green":
            self._decide(repo, "%s is red from %s, but #%d's head was not green; nothing "
                               "reverted, left for the brain" % (branch, short, int(pr["number"])))
        else:
            self._brief(repo, branch, sha, pr, names, red_since)

    def _pr_head_state(self, repo: str, pr: Dict[str, Any]) -> str:
        head = str((pr.get("head") or {}).get("sha") or "")
        if not head:
            return "none"
        return commit_state(self._check_runs(repo, head), self.now, self.review_check)

    # -- writes ----------------------------------------------------------
    def _lane(self, repo: str) -> Tuple[Optional[str], str]:
        """(lane, difficulty): the easiest tier with a lane up gets it."""
        if self._factory is None:
            self._factory = self.lanes()
            self._factory.count_load()
        for difficulty in DIFFICULTIES:
            lane = self._factory.tier_lane(repo, difficulty)
            if lane is not None:
                return lane, difficulty
        return None, ""

    def _brief(self, repo: str, branch: str, sha: str, pr: Dict[str, Any],
               names: List[str], red_since: float) -> None:
        short = sha[:12]
        try:
            ticket = brain._ticket_association(repo, pr)
        except ValueError:
            ticket = None
        lane, difficulty = self._lane(repo)
        if lane is None:
            self._decide(repo, "%s is red from %s; no lane is up to take its revert"
                         % (branch, short))
            return
        checks = ", ".join("`%s`" % n for n in names) or "a check"
        number = ticket[1] if ticket else 0
        title = " ".join(str(pr.get("title") or "").split()) or "#%d" % int(pr["number"])
        body = BODY % {
            "difficulty": difficulty, "branch": branch, "short": short, "sha": sha, "pr": int(pr["number"]),
            "for_ticket": " for ticket #%d" % number if number else "",
            "minutes": (self.now - red_since) // 60,
            "checks": ("check %s fails" if len(names) == 1 else "checks %s fail") % checks,
            "redo": (" Ticket #%d goes back to its lane to redo the change once this "
                     "lands." % number) if number else "",
            "criteria": "\n".join("- [ ] The check `%s` passes on this pull request." % n
                                  for n in names) or "- [ ] Every check passes on this pull request.",
            "reverted": "Reverted ticket: #%d\n" % number if number else "",
        }
        full_title = "Revert %s: %s" % (short, title)
        brain._check_written_text(full_title + "\n" + body)
        made = self.gh.rest("POST", "/repos/%s/issues" % repo, {
            "title": full_title, "body": body,
            "labels": ["loom:ticket", "loom:briefed", "lane:" + lane, REVERT_LABEL,
                       CLASS_LABEL]}).data or {}
        revert = int(made.get("number") or 0)
        self._briefed.append(sha)
        if self._factory is not None:
            self._factory.load[lane] = self._factory.load.get(lane, 0) + 1
        if number:
            note = ("The default branch `%s` turned red at %s, the merge of this ticket's "
                    "pull request #%d: %s. Revert ticket #%d reverts it. This ticket "
                    "goes back to `loom:briefed` on its lane once the revert lands, to "
                    "redo the change on the new default branch."
                    % (branch, short, int(pr["number"]),
                       ("check %s fails" if len(names) == 1 else "checks %s fail") % checks,
                       revert))
            brain._check_written_text(note)
            self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body": note})
        self._decide(repo, "%s is red from %s (#%d, %s); briefed revert #%d on lane %s"
                     % (branch, short, int(pr["number"]), ", ".join(names) or "checks",
                        revert, lane))

    def _redo(self, repo: str, revert: Dict[str, Any]) -> None:
        """Once a revert lands, its reverted ticket goes back to loom:briefed
        on its own lane, once (a REDO_MARKER comment on that ticket)."""
        if _state(revert) not in LANDED or self.now - _ts(revert.get("updated_at")) > RECENT:
            return
        m = REVERTED_RE.search(str(revert.get("body") or ""))
        if not m:
            return
        number, rnum = int(m.group(1)), int(revert["number"])
        marker = REDO_MARKER % rnum
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        if any(marker in str(c.get("body") or "") for c in comments
               if isinstance(c, dict) and self._trusted(c)):
            return
        issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
        lane = _lane(issue)
        if issue.get("state") != "open" or not lane or "loom:ticket" not in _labels(issue):
            self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {
                "body": "%s\nRevert #%d landed; this ticket is not an open ticket with a "
                        "lane, so it is left for the brain to redo." % (marker, rnum)})
            _say("%s: revert #%d landed; #%d left for the brain" % (repo, rnum, number))
            return
        self.move(self.gh, repo, number, "briefed", lane=lane,
                  note="%s\nRevert #%d landed: this ticket goes back to lane %s to redo "
                       "the change on the new default branch." % (marker, rnum, lane))
        _say("%s: revert #%d landed; re-briefed #%d on lane %s" % (repo, rnum, number, lane))
