"""After Loom merges a lane pull request on its own, its deploy is watched,
and a deploy that fails is reverted.

bus/revert.py reverts a merge only when a check turns the default branch
red. Most of the damage an audit of 2026-10-22 found never turned CI red: a
build workflow that failed only on the other build nodes, a dev-world
config roll whose seed step crash-looped, a backup hub left unpinned. Each
was found by a person hours or days later.

For LOOM_POST_MERGE_WATCH minutes (default 60) after a Loom auto-merge (a
lane pull request on a loom/ branch merged by a Loom App, not by a person),
the station reads one deploy outcome for the merge through one interface: each
signal answers healthy, failed or pending, with a reason. The outcome is
failed when any signal failed, healthy when every signal is healthy, and
pending otherwise. The signals are:

- workflow: a repo names its deploy workflow in `.loom/deploy-signal` on its
  default branch (a `.loom/` file, so only the operator changes it), one
  signal a line: `workflow <file> [path glob ...]`. The workflow's run for
  the merge commit is read. A Rollout-managed workload reports one annotation
  per Rollout with a fixed title: `rollout-healthy` is healthy;
  `rollout-aborted` (a canary aborted), `rollout-degraded`,
  `rollout-paused`, `rollout-timeout` and `rollout-unreadable` are failed.
  A Deployment or StatefulSet that is not a Rollout is judged by the run
  itself, which fails when its rollout or readiness wait fails. The titles
  and the conclusion decide; an annotation's prose is only quoted. A merge
  that changes no file under the globs deploys nothing and is healthy.
- live: the ticket's live check, when a live criterion names one Loom can
  run (bus/criteria_gate.py). It is healthy once it passes, and failed when
  it still fails at the end of the window.

Another repo plugs in its own signal by adding a kind to SIGNAL_KINDS.

Every read fails closed: a signal file, run or annotation list GitHub will
not give, a cancelled run, or an unknown signal kind is pending, never
healthy. A merge still pending at the end of the window is not reverted:
the operator is paged that Loom could not tell.

A failed outcome opens a revert pull request of the merge commit (GitHub's
own revert of the pull request) for a revert ticket the station files with
`loom:revert`. The revert is the only change this watch merges, and only
through bus/merge_tier.py's revert path: merge_tier.revert_refusal must find
nothing (the station applied the label, the body names the one reverted
commit, which the base carries, and the diff is that commit's exact
inverse), and then bus.brain merge, whose gates run again. A revert whose
checks fail, that is refused, or that does not merge within REVERT_WAIT is
left for the operator. Either way the operator is paged once, with one exact
message naming the failed signal and both pull requests. A revert is never
watched, so Loom never reverts a revert.

Each merge's watch is kept in the station's `deploy-watch.json`.
"""
from __future__ import annotations

import base64
import fnmatch
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import quote

from . import brain, criteria_gate, merge_tier, naming, notify, operator_hold, revert, text
from .github import GitHubError

HEALTHY = "healthy"
FAILED = "failed"
PENDING = "pending"

DEFAULT_WINDOW = 60 * 60
# A watched merge, and a repo's new merges, are read at most this often.
READ_EVERY = 5 * 60
# A revert pull request whose checks have not passed this long after it was
# opened is left for the operator.
REVERT_WAIT = 60 * 60
SIGNAL_FILE = ".loom/deploy-signal"
LEDGER = "deploy-watch.json"
KEEP = 200
REASON_MAX = 300
FILES_NAMED = 30

ROLLOUT_HEALTHY = "rollout-healthy"
ROLLOUT_FAILED = ("rollout-aborted", "rollout-degraded", "rollout-paused",
                  "rollout-timeout", "rollout-unreadable")
# A run that ended without judging the deploy: it says nothing either way.
UNJUDGED = ("cancelled", "skipped", "stale", "neutral")

# The watch's own states, kept per merge in the ledger.
WATCHING = "watching"
REVERTING = "reverting"
REVERTED = "reverted"
LEFT = "left for the operator"
UNVERIFIED = "unverified"
UNWATCHED = "unwatched"
DONE = (HEALTHY, REVERTED, LEFT, UNVERIFIED, UNWATCHED)


class Outcome(NamedTuple):
    """One deploy-outcome reading: healthy, failed or pending, and why."""
    state: str
    reason: str


class Merge(NamedTuple):
    """A Loom auto-merge under watch."""
    repo: str
    number: int
    sha: str
    merged_at: float
    title: str
    ticket: int
    ticket_body: str


def _say(message: str) -> None:
    print("deploy watch: %s" % message, flush=True)


def window(env: Optional[Mapping[str, str]] = None) -> int:
    """The watch window in seconds, from LOOM_POST_MERGE_WATCH (minutes)."""
    raw = str((os.environ if env is None else env).get("LOOM_POST_MERGE_WATCH", "")).strip()
    return int(raw) * 60 if raw.isdigit() and int(raw) > 0 else DEFAULT_WINDOW


def _ts(value: Any) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _clip(reason: str) -> str:
    return " ".join(str(reason).split())[:REASON_MAX]


# -- signals ------------------------------------------------------------------
class WorkflowSignal:
    """A deploy workflow's run for the merge commit, and its Rollout titles."""

    def __init__(self, workflow: str, paths: Sequence[str] = ()) -> None:
        self.workflow = workflow
        self.paths = tuple(paths)
        self.name = "workflow %s" % workflow

    @classmethod
    def parse(cls, args: Sequence[str]) -> "WorkflowSignal":
        if not args or "/" in args[0] or not args[0].endswith((".yml", ".yaml")):
            raise ValueError("a workflow signal names a workflow file, like deploy.yml")
        return cls(args[0], args[1:])

    def _deploys(self, gh: Any, merge: Merge) -> bool:
        """Whether the merge changes a file under the signal's globs (any
        file, with none). A commit whose file list is cut short deploys."""
        if not self.paths:
            return True
        data = gh.rest("GET", "/repos/%s/commits/%s" % (merge.repo, merge.sha)).data or {}
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list) or len(files) >= 300:
            return True
        names = [str(f.get("filename") or "") for f in files if isinstance(f, dict)]
        return any(fnmatch.fnmatchcase(n, g) or n.startswith(g.rstrip("*").rstrip("/") + "/")
                   for n in names for g in self.paths)

    def read(self, gh: Any, merge: Merge, now: float) -> Outcome:
        """The merge commit's newest run of the workflow, judged once it is
        done: pending while it runs or when it judged nothing."""
        run = self._newest_run(gh, merge)
        if run is None:
            if not self._deploys(gh, merge):
                return Outcome(HEALTHY, "it changes nothing %s deploys" % self.workflow)
            return Outcome(PENDING, "%s has no run for %s yet" % (self.workflow, merge.sha[:12]))
        run_id = int(run.get("id") or 0)
        conclusion = str(run.get("conclusion") or "")
        if run.get("status") != "completed":
            return Outcome(PENDING, "%s run %d is %s" % (self.workflow, run_id,
                                                         run.get("status") or "not started"))
        if conclusion in UNJUDGED or not conclusion:
            return Outcome(PENDING, "%s run %d ended %s, which judges nothing"
                           % (self.workflow, run_id, conclusion or "with no conclusion"))
        return self._judge(run_id, conclusion, self._annotations(gh, merge.repo, run_id))

    def _newest_run(self, gh: Any, merge: Merge) -> Optional[Dict[str, Any]]:
        """The workflow's newest run for the merge commit, or None."""
        data = gh.rest("GET", "/repos/%s/actions/workflows/%s/runs?head_sha=%s&per_page=20"
                       % (merge.repo, quote(self.workflow, safe=""), merge.sha)).data
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise ValueError("GitHub gave no run list for %s" % self.workflow)
        runs = [r for r in runs if isinstance(r, dict) and r.get("head_sha") == merge.sha]
        return max(runs, key=lambda r: int(r.get("id") or 0)) if runs else None

    def _judge(self, run_id: int, conclusion: str, notes: List[Dict[str, Any]]) -> Outcome:
        """A finished run: a failing Rollout title fails it whatever the
        run's result; else its result decides."""
        failed = [a for a in notes if a.get("title") in ROLLOUT_FAILED]
        if failed:
            return Outcome(FAILED, "%s: %s" % (failed[0]["title"], failed[0].get("message") or ""))
        if conclusion == "success":
            healthy = sum(1 for a in notes if a.get("title") == ROLLOUT_HEALTHY)
            shown = ", %d %s healthy" % (healthy, "Rollout" if healthy == 1 else "Rollouts")
            return Outcome(HEALTHY, "%s run %d succeeded%s"
                           % (self.workflow, run_id, shown if healthy else ""))
        errors = [str(a.get("message") or "") for a in notes
                  if a.get("annotation_level") == "failure"]
        return Outcome(FAILED, "%s run %d %s%s" % (self.workflow, run_id, conclusion,
                                                   ": " + errors[0] if errors else ""))

    @staticmethod
    def _annotations(gh: Any, repo: str, run_id: int) -> List[Dict[str, Any]]:
        """Every annotation of the run's jobs. Raises when a job or its
        annotations cannot be read: a missed rollout-aborted would read as
        healthy."""
        data = gh.rest("GET", "/repos/%s/actions/runs/%d/jobs?per_page=100" % (repo, run_id)).data
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("GitHub gave no job list for run %d" % run_id)
        notes: List[Dict[str, Any]] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            check = str(job.get("check_run_url") or "").rsplit("/", 1)[-1] or str(job.get("id"))
            if not check.isdigit():
                raise ValueError("a job of run %d names no check run" % run_id)
            # Every page: a rollout-aborted past the first would read healthy.
            found = gh.paginate("/repos/%s/check-runs/%s/annotations?per_page=100"
                                % (repo, check))
            notes += [a for a in found if isinstance(a, dict)]
        return notes


class LiveSignal:
    """The ticket's live checks (bus/criteria_gate.py): healthy once they
    pass, failed when one still fails at the end of the window."""

    name = "live check"

    def __init__(self, checks: Sequence[criteria_gate.Check], span: int,
                 runner: Optional[Callable[..., Tuple[bool, str]]] = None) -> None:
        self.checks = list(checks)
        self.span = span
        self.runner = runner or criteria_gate.run

    @classmethod
    def of(cls, body: str, span: int, runner: Optional[Callable[..., Tuple[bool, str]]] = None
           ) -> Optional["LiveSignal"]:
        checks = [c for c in (criteria_gate.check_of(t)
                              for k, t in criteria_gate.needing_evidence(body)
                              if k == criteria_gate.LIVE) if c is not None]
        return cls(checks, span, runner) if checks else None

    def read(self, gh: Any, merge: Merge, now: float) -> Outcome:
        for check in self.checks:
            passed, seen = self.runner(check, gh)
            if not passed:
                state = FAILED if now - merge.merged_at >= self.span else PENDING
                return Outcome(state, "live check %s: %s" % (check.target, seen))
        return Outcome(HEALTHY, "the ticket's live check passes")


# A signal kind's parse takes the words after its name on a line of
# SIGNAL_FILE. Add a kind here for a repo whose deploy reports another way.
SIGNAL_KINDS: Dict[str, Callable[[Sequence[str]], Any]] = {"workflow": WorkflowSignal.parse}


def parse_signals(content: str) -> List[Any]:
    """The signals of a SIGNAL_FILE: one a line, `#` comments. Raises on a
    kind it does not know, so a typo is never read as 'nothing to watch'."""
    found = []
    for raw in content.splitlines():
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        kind = SIGNAL_KINDS.get(words[0].lower())
        if kind is None:
            raise ValueError("%s names an unknown signal kind %r" % (SIGNAL_FILE, words[0]))
        found.append(kind(words[1:]))
    return found


def repo_signals(gh: Any, repo: str) -> List[Any]:
    """The repo's configured signals; [] when it has no SIGNAL_FILE. Raises
    when the file cannot be read."""
    try:
        data = gh.rest("GET", "/repos/%s/contents/%s" % (repo, SIGNAL_FILE)).data
    except GitHubError as err:
        if err.status == 404:
            return []
        raise
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        raise ValueError("%s could not be read" % SIGNAL_FILE)
    return parse_signals(base64.b64decode(str(data.get("content") or "")).decode("utf-8"))


def combine(outcomes: Sequence[Tuple[str, Outcome]]) -> Outcome:
    """Failed when any signal failed, healthy when all are, else pending."""
    for name, got in outcomes:
        if got.state == FAILED:
            return Outcome(FAILED, "%s: %s" % (name, got.reason))
    waiting = ["%s: %s" % (n, g.reason) for n, g in outcomes if g.state != HEALTHY]
    if waiting:
        return Outcome(PENDING, "; ".join(waiting))
    return Outcome(HEALTHY, "; ".join("%s: %s" % (n, g.reason) for n, g in outcomes))


def outcome(gh: Any, merge: Merge, signals: Sequence[Any], now: float) -> Outcome:
    """The merge's deploy outcome over its signals. A signal that cannot be
    read is pending."""
    read: List[Tuple[str, Outcome]] = []
    for signal in signals:
        try:
            got = signal.read(gh, merge, now)
        except (GitHubError, ValueError, KeyError, TypeError) as err:
            got = Outcome(PENDING, "could not be read (%s)" % _clip(str(err)))
        read.append((signal.name, Outcome(got.state, _clip(got.reason))))
    return combine(read)


# -- messages -----------------------------------------------------------------
def _safe(reason: str) -> str:
    """The reason as written to GitHub: a quoted annotation that names a
    private host or path is cut to its signal word."""
    try:
        text.check(reason)
        return reason
    except ValueError:
        return reason.split(":", 1)[0] + " (details withheld: they name a private host or path)"


def reverted_page(merge: Merge, reason: str, revert_pr: int) -> str:
    return ("Loom reverted %s#%d (%s): its deploy failed after Loom merged it: %s. "
            "Revert %s#%d merged. Check the deploy is healthy again, then re-brief ticket "
            "#%d or close it." % (merge.repo, merge.number, merge.title, reason, merge.repo,
                                  revert_pr, merge.ticket))


def left_page(merge: Merge, reason: str, revert_pr: int, why: str) -> str:
    made = ("revert %s#%d was not merged: %s" % (merge.repo, revert_pr, why) if revert_pr
            else "no revert could be opened: %s" % why)
    return ("Loom could not revert %s#%d (%s) on its own: its deploy failed after Loom "
            "merged it: %s; %s. Merge or close the revert yourself, or revert by hand."
            % (merge.repo, merge.number, merge.title, reason, made))


def unverified_page(merge: Merge, reason: str, span: int) -> str:
    return ("Loom could not tell whether the deploy of %s#%d (%s) worked within %d minutes "
            "of its merge: %s. Check the deploy yourself."
            % (merge.repo, merge.number, merge.title, span // 60, reason))


REVERT_BODY = """Difficulty: easy

## Why
Loom merged #%(pr)d on its own, and its deploy failed within the post-merge \
watch: %(reason)s. The watch reverts it.

## What
- `git revert --no-edit %(sha)s`, opened as a revert pull request by the station.
- Change nothing else. The files it puts back: %(files)s.

## Acceptance criteria
- [ ] The revert pull request merges with its checks green.

## Size
XS

## Out of scope
- Fixing or redoing the reverted change.
"""


# -- the pass -------------------------------------------------------------------
REVERT_MUTATION = (
    "mutation($id:ID!,$title:String!,$body:String!) {"
    " revertPullRequest(input:{pullRequestId:$id,title:$title,body:$body}) {"
    " revertPullRequest { number } } }")


class Watch:
    """One station pass over Loom's recent auto-merges.

    `page` sends one message to the operator and returns True when it went
    out, False to try again next pass, or None when no page is set up (the
    message then goes on the merged pull request's ticket instead)."""

    def __init__(self, gh: Any, repos: Sequence[str], now: float, trusted: Sequence[str],
                 state_dir: str, page: Callable[[str], Optional[bool]],
                 env: Optional[Mapping[str, str]] = None,
                 runner: Optional[Callable[..., Tuple[bool, str]]] = None) -> None:
        self.gh = gh
        self.repos = list(repos)
        self.now = now
        self.trusted = {t.lower() for t in trusted}
        self.state_dir = state_dir
        self.page = page
        self.env = dict(os.environ if env is None else env)
        self.span = window(self.env)
        self.runner = runner
        self.ledger: Dict[str, Any] = {"merges": {}, "scanned": {}}
        self._signals: Dict[str, Any] = {}
        self._load()

    # -- ledger ----------------------------------------------------------
    def _path(self) -> str:
        return os.path.join(self.state_dir, LEDGER)

    def _load(self) -> None:
        try:
            with open(self._path()) as fh:
                kept = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(kept, dict) and isinstance(kept.get("merges"), dict):
            self.ledger["merges"] = kept["merges"]
            if isinstance(kept.get("scanned"), dict):
                self.ledger["scanned"] = kept["scanned"]

    def save(self) -> None:
        merges = self.ledger["merges"]
        if len(merges) > KEEP:
            old = sorted(merges, key=lambda k: float(merges[k].get("merged_at") or 0))
            for key in old[:len(merges) - KEEP]:
                if merges[key].get("state") in DONE:
                    del merges[key]
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self._path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.ledger, fh, indent=1, sort_keys=True)
        os.replace(tmp, self._path())

    # -- discovery -------------------------------------------------------
    def _loom_merge(self, pr: Any) -> bool:
        """A lane pull request a trusted login opened, merged recently. Who
        merged it is read later (the list does not say)."""
        return (isinstance(pr, dict) and bool(pr.get("merged_at"))
                and bool(pr.get("merge_commit_sha"))
                and str((pr.get("head") or {}).get("ref") or "").startswith("loom/")
                and str((pr.get("user") or {}).get("login") or "").lower() in self.trusted
                and self.now - _ts(pr.get("merged_at")) <= self.span
                and not revert.looks_like_revert(pr))

    def discover(self, repo: str) -> None:
        if self.now - float(self.ledger["scanned"].get(repo) or 0) < READ_EVERY:
            return
        data = self.gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated&direction=desc"
                                   "&per_page=100" % repo).data
        merges = self.ledger["merges"]
        for pr in (data if isinstance(data, list) else []):
            if not self._loom_merge(pr):
                continue
            key = "%s#%d" % (repo, int(pr["number"]))
            if key in merges:
                continue
            entry = self._entry(repo, pr)
            if entry is not None:
                merges[key] = entry
        self.ledger["scanned"][repo] = self.now

    def _entry(self, repo: str, pr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The ledger entry for a new merge, or None to look again next pass.
        A merge by a person is the operator's, and a revert's is never
        watched."""
        number = int(pr["number"])
        full = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data or {}
        merger = full.get("merged_by") if isinstance(full, dict) else None
        if not isinstance(merger, dict) or not merger.get("login"):
            return None
        base = {"pr": number, "repo": repo, "sha": str(pr["merge_commit_sha"]),
                "merged_at": _ts(pr.get("merged_at")),
                "title": " ".join(str(pr.get("title") or "").split()),
                "merged_by": str(merger.get("login")), "lane": "", "ticket": 0}
        if merger.get("type") != "Bot" or str(merger["login"]).lower() not in self.trusted:
            return dict(base, state=UNWATCHED, reason="merged by a person")
        return self._with_ticket(repo, pr, base)

    def _with_ticket(self, repo: str, pr: Dict[str, Any],
                     base: Dict[str, Any]) -> Dict[str, Any]:
        """The entry with its ticket's number, lane and body; unwatched when
        the ticket is a revert's."""
        try:
            ticket = brain._ticket_association(repo, pr)
        except ValueError:
            ticket = None
        if ticket is None:
            return dict(base, state=WATCHING, reason="", read_at=0.0, ticket_body="")
        issue = self.gh.rest("GET", "/repos/%s/issues/%d" % ticket).data or {}
        names = [str(label.get("name") if isinstance(label, dict) else label)
                 for label in issue.get("labels") or []]
        if merge_tier.REVERT_LABEL in names:
            return dict(base, state=UNWATCHED, reason="it is a revert")
        return dict(base, state=WATCHING, reason="", read_at=0.0, ticket=ticket[1],
                    lane=next((n[5:] for n in names if n.startswith("lane:")), ""),
                    ticket_body=str(issue.get("body") or ""))

    # -- reading -----------------------------------------------------------
    def _repo_signals(self, repo: str) -> Any:
        """The repo's signals, read once a pass; an error string when they
        cannot be read."""
        if repo not in self._signals:
            try:
                self._signals[repo] = repo_signals(self.gh, repo)
            except (GitHubError, ValueError, UnicodeDecodeError) as err:
                self._signals[repo] = "%s could not be read (%s)" % (SIGNAL_FILE, _clip(str(err)))
        return self._signals[repo]

    @staticmethod
    def merge_of(entry: Dict[str, Any]) -> Merge:
        return Merge(str(entry["repo"]), int(entry["pr"]), str(entry["sha"]),
                     float(entry.get("merged_at") or 0), str(entry.get("title") or ""),
                     int(entry.get("ticket") or 0), str(entry.get("ticket_body") or ""))

    def judge(self, key: str, entry: Dict[str, Any]) -> None:
        if self.now - float(entry.get("read_at") or 0) < READ_EVERY:
            return
        merge = self.merge_of(entry)
        configured = self._repo_signals(merge.repo)
        signals: List[Any] = []
        if isinstance(configured, str):
            got: Optional[Outcome] = Outcome(PENDING, configured)
        else:
            signals = list(configured)
            live = LiveSignal.of(merge.ticket_body, self.span, self.runner)
            if live is not None:
                signals.append(live)
            got = outcome(self.gh, merge, signals, self.now) if signals else None
        entry["read_at"] = self.now
        if got is None:
            entry.update(state=UNWATCHED, reason="no deploy signal and no live check")
            _say("%s: nothing to watch (no %s, no live check)" % (key, SIGNAL_FILE))
            return
        entry["reason"] = got.reason
        if got.state == HEALTHY:
            entry["state"] = HEALTHY
            _say("%s deployed healthy: %s" % (key, got.reason))
        elif got.state == FAILED:
            entry.update(state=REVERTING, failed_at=self.now, failure=got.reason)
            _say("%s failed after merge: %s; reverting it" % (key, got.reason))
        elif self.now - merge.merged_at >= self.span:
            entry.update(state=UNVERIFIED, page=unverified_page(merge, got.reason, self.span))
            _say("%s still pending after %d minutes: %s" % (key, self.span // 60, got.reason))

    # -- reverting -----------------------------------------------------------
    def _file_ticket(self, merge: Merge, entry: Dict[str, Any]) -> int:
        reason = _safe(str(entry.get("failure") or ""))
        data = self.gh.rest("GET", "/repos/%s/commits/%s" % (merge.repo, merge.sha)).data or {}
        names = [str(f.get("filename") or "") for f in (data.get("files") or [])
                 if isinstance(f, dict)][:FILES_NAMED]
        # The files are named so bus/operator_hold.py sees the revert's ticket
        # name a doc the revert takes away again.
        body = REVERT_BODY % {"pr": merge.number, "reason": reason, "sha": merge.sha,
                              "files": ", ".join("`%s`" % n for n in names) or "none listed"}
        title = naming.generated("Revert %s" % merge.sha[:12], merge.title)
        text.check(title + "\n" + body)
        made = self.gh.rest("POST", "/repos/%s/issues" % merge.repo, {
            "title": title, "body": body,
            "labels": ["loom:ticket", "loom:pr", merge_tier.REVERT_LABEL,
                       revert.CLASS_LABEL]}).data or {}
        return int(made.get("number") or 0) if isinstance(made, dict) else 0

    def _open_revert(self, merge: Merge, entry: Dict[str, Any]) -> int:
        node = (self.gh.rest("GET", "/repos/%s/pulls/%d" % (merge.repo, merge.number)).data
                or {}).get("node_id")
        if not node:
            raise ValueError("#%d has no node id to revert" % merge.number)
        body = ("Refs %s#%d\n\nThe post-merge watch saw the deploy of #%d fail: %s\n\n"
                "git revert --no-edit %s\n" % (merge.repo, int(entry["revert_ticket"]),
                                              merge.number, _safe(str(entry.get("failure"))),
                                              merge.sha))
        title = 'Revert "%s"' % merge.title
        text.check(title + "\n" + body)
        data = self.gh.graphql(REVERT_MUTATION, {"id": node, "title": title, "body": body}) or {}
        made = ((data.get("revertPullRequest") or {}).get("revertPullRequest") or {})
        number = int(made.get("number") or 0)
        if not number:
            raise ValueError("GitHub opened no revert pull request")
        return number

    def _try_merge(self, merge: Merge, entry: Dict[str, Any]) -> Optional[str]:
        """Merge the revert pull request through the revert path: None while
        it waits for its checks, "" once merged, else why it was not."""
        number, ticket = int(entry["revert_pr"]), int(entry["revert_ticket"])
        pr, waiting = self._revert_ready(merge, entry, number)
        if waiting is not None or pr is None:
            return waiting
        issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (merge.repo, ticket)).data or {}
        entries = operator_hold.pull_files(self.gh, merge.repo, number)
        refused = merge_tier.revert_refusal(self.gh, merge.repo, dict(pr, number=number),
                                            issue, entries, self.env)
        if refused:
            return refused
        try:
            brain.merge(self.gh, merge.repo, number, (merge.repo, ticket), env=self.env)
        except ValueError as err:
            # bus.brain merge refused it: its gates are the operator's to judge.
            return _clip(str(err)) or "bus.brain merge refused it"
        return ""

    def _revert_ready(self, merge: Merge, entry: Dict[str, Any], number: int
                      ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """(the revert pull request, None) once its checks passed and GitHub
        says it can merge; else (None, None) while it waits, or (None, why)
        when it failed or waited past REVERT_WAIT."""
        details = brain._pr_graphql(self.gh, merge.repo, number)
        nodes = (details.get("commits") or {}).get("nodes") or []
        rollup = ((nodes[-1].get("commit") or {}).get("statusCheckRollup") or {}) if nodes else {}
        state = str(rollup.get("state") or "")
        if state in ("FAILURE", "ERROR"):
            return None, "its checks failed"
        pr = self.gh.rest("GET", "/repos/%s/pulls/%d" % (merge.repo, number)).data or {}
        if state == "SUCCESS" and pr.get("mergeable") is True:
            return pr, None
        if self.now - float(entry.get("revert_at") or self.now) >= REVERT_WAIT:
            return None, "its checks did not pass within %d minutes" % (REVERT_WAIT // 60)
        return None, None

    def _named_elsewhere(self, merge: Merge) -> int:
        """A revert ticket bus/revert.py (or a person) already filed for the
        merge commit, or 0."""
        found = self.gh.paginate("/repos/%s/issues?labels=%s&state=all"
                                 % (merge.repo, merge_tier.REVERT_LABEL))
        named = revert.revert_shas([i for i in found if isinstance(i, dict)
                                    and "pull_request" not in i]).get(merge.sha)
        return int((named or {}).get("number") or 0)

    def revert(self, key: str, entry: Dict[str, Any]) -> None:
        merge = self.merge_of(entry)
        reason = _safe(str(entry.get("failure") or ""))
        try:
            if not entry.get("revert_ticket"):
                other = self._named_elsewhere(merge)
                if other:
                    why = "revert ticket #%d already names it" % other
                    entry.update(state=LEFT, left=why, page=left_page(merge, reason, 0, why))
                    _say("%s left for the operator: %s" % (key, why))
                    return
                entry["revert_ticket"] = self._file_ticket(merge, entry)
                self.save()
            if not entry.get("revert_pr"):
                entry["revert_pr"] = self._open_revert(merge, entry)
                entry["revert_at"] = self.now
                self.save()
                return
            why = self._try_merge(merge, entry)
        except (GitHubError, ValueError) as err:
            # A read or write that failed is tried again next pass, until the
            # revert has had REVERT_WAIT; then the operator takes it.
            why = _clip(str(err))
            if self.now - float(entry.get("failed_at") or self.now) < REVERT_WAIT:
                _say("%s: the revert waits: %s" % (key, why))
                return
        if why is None:
            return
        pr = int(entry.get("revert_pr") or 0)
        if why == "":
            entry.update(state=REVERTED, page=reverted_page(merge, reason, pr))
            _say("%s reverted by #%d" % (key, pr))
        else:
            entry.update(state=LEFT, left=why, page=left_page(merge, reason, pr, why))
            _say("%s left for the operator: %s" % (key, why))

    # -- paging -----------------------------------------------------------------
    def tell(self, key: str, entry: Dict[str, Any]) -> None:
        """Page the operator once with the entry's message."""
        message = str(entry.get("page") or "")
        if not message or entry.get("paged"):
            return
        sent = self.page(message)
        if sent is None and entry.get("ticket"):
            try:
                text.check(message)
                notify.post(self.gh, entry["repo"], int(entry["ticket"]), message,
                            notify.FAILURE)
                sent = True
            except (GitHubError, ValueError) as err:
                _say("%s: could not note the outcome on its ticket: %s" % (key, err))
                sent = False
        if sent is None:
            _say("%s: no page is set up; %s" % (key, message))
            sent = True
        entry["paged"] = bool(sent)

    def run(self) -> None:
        for repo in self.repos:
            try:
                self.discover(repo)
            except (GitHubError, ValueError, KeyError, TypeError) as err:
                _say("%s: could not read its merges: %s" % (repo, err))
        for key, entry in sorted(self.ledger["merges"].items()):
            if entry.get("repo") not in self.repos:
                continue
            try:
                if entry.get("state") == WATCHING:
                    self.judge(key, entry)
                if entry.get("state") == REVERTING:
                    self.revert(key, entry)
                self.tell(key, entry)
            except (GitHubError, ValueError, KeyError, TypeError) as err:
                _say("%s: %s" % (key, err))
        try:
            self.save()
        except OSError as err:
            _say("could not keep the watch ledger: %s" % err)
