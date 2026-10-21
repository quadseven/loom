"""Settle operator-claimed issues once their pull requests merge (loom#1204).

Pull requests never carry a closing keyword, so an issue an operator session
files or claims (a `claim:<login>` label and no `loom:` label) stayed open
after its pull request merged: Loom settles only its own tickets (the verify
path, the done close, settle_source). About 60 such issues stayed open in one
day while their work had landed.

The station's settle_claimed_tickets step reads each such issue at most once
an hour. When every pull request whose `Refs` line names it is merged into
the default branch, with the review check concluding success on its head and
every other check success, skipped or neutral, and no open pull request
names it:

- every acceptance box asks only for merged code or tests: one evidence
  comment of checked facts, the boxes ticked, the issue closed as completed;
- a box asks for live, deployed or production behaviour, a time window or an
  observed event: the `awaiting-live-check` label, once, and the issue stays
  open for a person.

An issue with a `loom:`, `needs:`, `needs-`, `ready-for-human` or `wontfix`
label, an epic, or one with no acceptance boxes is never touched. LOOM_SETTLE_CLAIMED picks the mode:
unset or `dry` logs what it would do and writes nothing, `on` writes, `off`
skips the step. LOOM_SETTLE_CLAIMED_MAX caps the issues settled per run, and
writes are at least WRITE_GAP seconds apart.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import evidence
from . import proof
from . import verify_data

CLAIM_PREFIX = "claim:"
LIVE_LABEL = "awaiting-live-check"
MARKER = "<!-- loom:claimed-settle v1 -->"
STATE_FILE = "claimed.json"
EVERY = 3600
PER_RUN = 10
WRITE_GAP = 3.0
# Pull requests per issue, and test files per pull request, read in full.
MAX_PULLS = verify_data.MAX_PULLS
MAX_TEST_FILES = 10
# What a check other than the review check may conclude. The review bot's
# other checks conclude neutral when they have nothing to say, on nearly every
# head, and the merge gate takes that; the review check itself must succeed.
PASSING = verify_data.GREEN_CONCLUSIONS + ("neutral",)
HOLD_PREFIXES = ("loom:", "needs:", "needs-")
HOLD_LABELS = ("ready-for-human", "epic", "wontfix", LIVE_LABEL)

_sleep = time.sleep
_clock = time.monotonic

# A word that names a running system, a time window or an observed event. A
# word followed by a path or file character ("production/", "deploy.yml") is
# a name in the code, not a claim about a running system. Wide on purpose: a
# box read as live only labels the issue, a box missed closes it.
_NOT_A_NAME = r"(?![./_-]\w)"
_COUNT = r"(?:a|an|one|two|three|four|five|six|seven|\d+)"
_SPAN = r"(?:second|minute|hour|day|night|week|month|tick|run|pass|cycle)s?"
_LIVE_RE = re.compile(
    r"\b(?:live|deploy(?:s|ed|ing|ment)?|production|prod|staging|in the wild"
    r"|roll(?:s|ed)? out"
    r"|on the (?:host|server|cluster|node|runner|device)s?|cluster|kubectl|ssh"
    r"|observed?|seen|shows? up|appears? in|in the logs?|logs? shows?"
    r"|alerts?|alerted|fires?|fired|uptime|reachable|outage"
    r"|overnight|nightly|daily|weekly|hourly|tomorrow"
    r"|next (?:run|tick|pass|day|week|deploy|release)|scheduled run|cron"
    r"|after (?:the |a |each |every )?(?:merge|deploy|ship|release|restart|reboot)"
    r"|(?:for|within|over|across|after) " + _COUNT + r" " + _SPAN +
    r"|\d+ ?(?:h|hrs?|hours?|d|days?|mins?|minutes?|weeks?))\b" + _NOT_A_NAME,
    re.I)


def mode(raw: str) -> str:
    """LOOM_SETTLE_CLAIMED as "on", "off" or "dry" (the default)."""
    value = raw.strip().lower()
    if value in ("1", "on", "true", "yes"):
        return "on"
    return "off" if value in ("0", "off", "false", "no") else "dry"


def per_run(raw: str) -> int:
    """LOOM_SETTLE_CLAIMED_MAX: a whole number, else PER_RUN."""
    raw = raw.strip()
    return int(raw) if raw.isdigit() else PER_RUN


def _names(issue: Dict[str, Any]) -> List[str]:
    return [str((label or {}).get("name") or "") for label in issue.get("labels") or []
            if isinstance(label, dict)]


def _holds(name: str) -> bool:
    return name.startswith(HOLD_PREFIXES) or name in HOLD_LABELS


def _epic(issue: Dict[str, Any]) -> bool:
    """An `Epic:` title or sub-issues (the `epic` label is a hold)."""
    summary = issue.get("sub_issues_summary")
    return str(issue.get("title") or "").lower().startswith("epic:") \
        or bool(isinstance(summary, dict) and summary.get("total"))


def skip_reason(issue: Dict[str, Any]) -> str:
    """Why the claimed settle leaves the issue alone, or "" for a candidate."""
    names = [name.lower() for name in _names(issue)]
    if issue.get("state") != "open" or "pull_request" in issue:
        return "not an open issue"
    if not any(name.startswith(CLAIM_PREFIX) for name in names):
        return "no claim label"
    held = next((name for name in names if _holds(name)), "")
    if held:
        return "labelled %s" % held
    if _epic(issue):
        return "an epic"
    if not proof.criteria(str(issue.get("body") or "")):
        return "no acceptance boxes"
    return ""


def live_box(text: str) -> bool:
    """True when the box asks for live, deployed or production behaviour, a
    time window or an observed event: merged code cannot show it."""
    return bool(_LIVE_RE.search(text))


def live_boxes(body: str) -> List[str]:
    """The unticked acceptance boxes merged code cannot prove."""
    return [text for checked, text in proof.criteria(body) if not checked and live_box(text)]


def pull_gate(pr: Dict[str, Any], runs: Sequence[Dict[str, Any]], review_check: str,
              branch: str) -> str:
    """Why a referencing pull request does not prove its issue, or "" when it
    merged into `branch` with the review check concluding success on its
    head and every other check success, skipped or neutral. `runs` are its head's
    newest runs, one per check name."""
    return _merge_gap(pr, branch) or _checks_gap(runs, review_check)


def _merge_gap(pr: Dict[str, Any], branch: str) -> str:
    if not pr.get("merged_at") or not verify_data.COMMIT_RE.fullmatch(
            str(pr.get("merge_commit_sha") or "")):
        return "not merged"
    if not branch or str((pr.get("base") or {}).get("ref") or "") != branch:
        return "merged into another branch than %s" % (branch or "the default branch")
    return ""


def _checks_gap(runs: Sequence[Dict[str, Any]], review_check: str) -> str:
    if not runs:
        return "no check runs on its head"
    results = {verify_data._plain(run.get("name")): verify_data._run_result(run) for run in runs}
    review = results.get(verify_data._plain(review_check))
    if review != "success":
        return "%s %s on its head" % (review_check, review or "missing")
    red = [(name, result) for name, result in results.items()
           if result not in PASSING]
    if red:
        return "%s %s on its head" % red[0]
    return ""


def added_tests(files: Iterable[Any]) -> List[Tuple[str, str]]:
    """(file, test function) for each test the pull request's test files add."""
    found: List[Tuple[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or "")
        if verify_data.test_file(name):
            found.extend((name, test) for test in
                         dict.fromkeys(verify_data.TEST_DEF_RE.findall(str(item.get("patch") or ""))))
    return found


def decoded(content: Any) -> str:
    """A contents API reply's text, "" when it carries none."""
    if not isinstance(content, dict) or content.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(str(content.get("content") or "")).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def defines(source: str, test: str) -> bool:
    return bool(re.search(r"^\s*def %s\s*\(" % re.escape(test), source, re.M))


def _checks_line(runs: Sequence[Dict[str, Any]]) -> str:
    shown = ["%s %s" % (verify_data._plain(run.get("name")) or "unnamed",
                        verify_data._run_result(run))
             for run in runs[:verify_data.MAX_CHECKS]]
    more = len(runs) - len(shown)
    return ", ".join(shown) + (", and %d more" % more if more > 0 else "")


def evidence_comment(pulls: Sequence[Dict[str, Any]], branch: str, review_check: str) -> str:
    """The one comment a settled issue gets, from checked facts only. Each
    pull dict carries `runs` (its head's newest runs) and `tests` (the test
    functions it added that the default branch still defines). A line that
    fails the text checks is dropped and counted."""
    lines: List[Tuple[str, Sequence[str]]] = []
    for pr in pulls:
        sha = str(pr.get("merge_commit_sha") or "")
        head = str((pr.get("head") or {}).get("sha") or "")
        lines.append(("- PR #%d merged into %s as %s." % (int(pr.get("number") or 0), branch, sha),
                      [sha]))
        lines.append(("  - Checks on its head %s: %s." % (head[:12], _checks_line(pr["runs"])),
                      []))
        tests = list(pr.get("tests") or [])[:verify_data.MAX_TESTS]
        lines.append(("  - Tests it added that %s defines: %s." % (
            branch, ", ".join(tests) if tests else "none found"),
            [t for t in tests if verify_data.name_shaped(t)]))
    kept = [line for line, known in lines if evidence.safe_line(line, known)]
    dropped = len(lines) - len(kept)
    return "\n".join([
        MARKER,
        "Settled by the station: every pull request whose Refs line names this issue is "
        "merged into %s, with %s concluding success on its head and every other check "
        "success, skipped or neutral, and none is still open." % (branch, review_check),
        ""] + kept + ([""] + ["%d lines failed the text checks and were left out." % dropped]
                      if dropped else []) + [
        "",
        "No acceptance box asks for live, deployed or production behaviour, a time window "
        "or an observed event, so each is shown by the merged code and its tests: the boxes "
        "are ticked and this issue is closed as completed. A box that does not hold is a "
        "reason to reopen it."])


class Pacer:
    """At least WRITE_GAP seconds between writes."""

    def __init__(self) -> None:
        self._last: Optional[float] = None

    def wait(self) -> None:
        if self._last is not None:
            gap = WRITE_GAP - (_clock() - self._last)
            if gap > 0:
                _sleep(gap)
        self._last = _clock()
