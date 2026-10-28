"""An Actions job that failed on the infrastructure, not on the change.

A job that times out installing packages, loses its runner or cannot reach a
package mirror says nothing about the pull request: a lane cannot fix it by
editing code, and the same job usually passes on its next attempt. The factory
re-ran a failed job only when no review check had failed on the head and only
from the loom:pr judge path (Factory._run_again), and the station's fix-round
path never re-ran one, so such a job was quoted in a fix brief and then read as
"the same failures came back" (bus/converge.py), which climbs the model ladder
or stops the rounds. On 2026-10-26 three pull requests of one public repo went
that way: each had its adapter jobs time out at run attempt 1 in the step
"Install what the core's own Dockerfile installs" beside a failing review.

infra_flake() reads a job's log excerpt (bus/brain.py log_excerpt, which ends
at the last `##[error]` line) or its check output. rerun_once() re-runs the
failed jobs of those workflow runs once: a run already past its first attempt
is left alone, so a job that fails the same way twice goes to the usual paths.
Nothing here decides what may merge.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import brain
from .github import GitHub, GitHubError

ACTIONS_RUN_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")
# A step that only fetches or installs what the job needs. A test or build
# step that times out may be the change's own hang, so it is not a flake.
SETUP_STEP_RE = re.compile(
    r"\b(install(s|ing)?|set ?up|setup|download(s|ing)?|fetch(es|ing)?|apt(-get)?|"
    r"cache|restore|pull(s|ing)?|dependencies|deps|toolchain|checkout)\b", re.I)
# A step name may hold an apostrophe ("Install what the core's own
# Dockerfile installs"), so the name runs to the last quote on the line.
STEP_TIMEOUT_RE = re.compile(r"The action '(.*)' has timed out after \d+ minutes")
STEP_FAILED_RE = re.compile(r"The action '(.*)' failed(?: with exit code \d+)?")
# Runner and mirror failures, whatever step they hit.
INFRA_LINE_RE = re.compile(
    r"The runner has received a shutdown signal"
    r"|lost communication with the server"
    r"|The hosted runner encountered an error"
    r"|^\s*E: (?:Failed to fetch|Unable to fetch some archives)"
    r"|Temporary failure resolving '", re.M)
# A job GitHub's conclusion marks as cancelled: CI infrastructure, not the
# change.
CANCELLED_RE = re.compile(r"\bconclusion\s+cancelled\b|\bthe job was cancelled\b", re.I)
# A pytest error detail line (pytest prefixes the error with "E ").
PYTEST_ERROR_DETAIL_RE = re.compile(r"^\s*E\b", re.M)
# A pytest test failure line (an assertion), which names the change, not the
# infrastructure.
PYTEST_FAILED_RE = re.compile(r"\bFAILED\b", re.I)
# A timeout or connection error named in a pytest error detail line.
PYTEST_INFRA_RE = re.compile(
    r"\b(?:ReadTimeout|ConnectTimeout|ConnectionError|timeout|connection refused|"
    r"unreachable|could not connect|network is unreachable)\b", re.I)


def _pytest_only_infra_errors(text: str) -> bool:
    """True when pytest reports only errors (no test failures) and every error
    detail line names a timeout or a connection error: the change's tests did
    not fail, the infrastructure did."""
    lines = text.splitlines()
    if any(PYTEST_FAILED_RE.search(line) for line in lines):
        return False
    details = [line for line in lines if PYTEST_ERROR_DETAIL_RE.match(line)]
    if not details:
        return False
    return all(PYTEST_INFRA_RE.search(line) for line in details)


def _setup_step_failed(text: str) -> bool:
    """True when a setup or install step (not a test or build step) failed,
    so the job never ran any repo code."""
    for match in STEP_FAILED_RE.finditer(text):
        if SETUP_STEP_RE.search(match.group(1)):
            return True
    return False


def infra_flake(text: Optional[str]) -> bool:
    """Does this job log excerpt or check output show an infrastructure
    failure: a cancelled job, a setup or install step's own timeout or failure,
    a lost or stopped runner, a package mirror that could not be reached, a
    job that failed before any repo code ran, or pytest reporting only errors
    that each name a timeout or a connection error (no test failures)?"""
    if not text:
        return False
    if CANCELLED_RE.search(text):
        return True
    if _setup_step_failed(text):
        return True
    if _pytest_only_infra_errors(text):
        return True
    for match in STEP_TIMEOUT_RE.finditer(text):
        if SETUP_STEP_RE.search(match.group(1)):
            return True
    return bool(INFRA_LINE_RE.search(text))


def _run_id(run: Dict[str, Any]) -> Optional[str]:
    match = ACTIONS_RUN_RE.search(str(run.get("details_url") or ""))
    return match.group(1) if match else None


def _output_text(run: Dict[str, Any]) -> str:
    out = run.get("output") or {}
    return "\n".join(str(x) for x in (out.get("title"), out.get("summary"), out.get("text")) if x)


def flaky_names(gh: GitHub, repo: str, runs: Sequence[Dict[str, Any]],
                skip: Iterable[str] = ()) -> List[str]:
    """The failed Actions checks among `runs` (the newest run of each check
    on a head) that failed on the infrastructure, read from each job's log,
    or its check output when no log is readable. Checks in `skip` (the review
    and plan checks, whose failures are findings) are never flakes."""
    skipped = set(skip)
    found = []
    for run in runs:
        name = str(run.get("name") or "")
        if (run.get("conclusion") not in brain.FAILED_CONCLUSIONS or name in skipped
                or _run_id(run) is None):
            continue
        text = None
        if run.get("id") is not None:
            try:
                text = brain._job_log_tail(gh, repo, int(run["id"]))
            except (GitHubError, TypeError, ValueError):
                text = None
        if infra_flake(text) or infra_flake(_output_text(run)):
            found.append(name)
    return found


def rerun_once(gh: GitHub, repo: str, runs: Sequence[Dict[str, Any]],
               names: Iterable[str]) -> List[str]:
    """Re-run the failed jobs of each workflow run that holds a failed check
    named in `names`, when that run is on its first attempt. The workflow run
    ids re-run; empty when none was."""
    wanted = set(names)
    ids = sorted({rid for rid in (_run_id(r) for r in runs
                                  if r.get("conclusion") in brain.FAILED_CONCLUSIONS
                                  and str(r.get("name") or "") in wanted) if rid})
    done = []
    for run_id in ids:
        attempt = int((gh.rest("GET", "/repos/%s/actions/runs/%s" % (repo, run_id)).data
                       or {}).get("run_attempt") or 1)
        if attempt >= 2:
            continue
        gh.rest("POST", "/repos/%s/actions/runs/%s/rerun-failed-jobs" % (repo, run_id))
        done.append(run_id)
    return done
