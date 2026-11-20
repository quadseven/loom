"""Decide whether an open ticket is ready for a worker to take.

A worker told to "take a ready ticket" came back empty from repos with
hundreds of open issues: the marker means different things per repo (here
loom:ready already marks a pull request whose checks passed, so it cannot
mark a ticket), a ready ticket was also blocked or held by a dead claim, or
its problem was not in the code (loom#3362). Readiness is read from the ticket,
not asserted by a label.

classify() is a pure read of one ticket and returns one Verdict:

    READY        no claim, no open pull request, no person wait, not done
    CLEAR_CLAIM  this Loom's own claim has no activity within the idle limit
                 (stale_claims.lapsed); the claim goes, the ticket is then
                 judged again on the next pass
    HAS_PR       an open pull request names the ticket
    NEEDS_PERSON a person's ask, wait label or claim holds the ticket
    DONE         a merged pull request or a state label says the work is done;
                 flagged for a verify, never marked ready
    HELD         anything else that keeps a ticket off the board: another
                 operator's claim, a run in progress, a wait label, a
                 non-ticket

Nothing here reads GitHub or writes a label: the station step that applies a
Verdict and tallies it per repo is a later slice. Tally keeps those counts.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, NamedTuple, Optional

from . import human_claims, stale_claims

# loom:ready is taken: it marks a pull request ready to merge (bus/labels.py).
READY_MARKER = "loom:pickable"

READY = "ready"
CLEAR_CLAIM = "clear-claim"
HAS_PR = "has-pr"
NEEDS_PERSON = "needs-person"
DONE = "done"
HELD = "held"
KINDS = (READY, CLEAR_CLAIM, HAS_PR, NEEDS_PERSON, DONE, HELD)

# State labels that mean the work was built (docs/SDLC.md life cycle).
DONE_STATES = ("loom:merged", "loom:shipped", "loom:verified")
# Labels that mean a person owes the next move.
PERSON_LABELS = ("loom:human", "needs:human")
RUNNING_STATES = ("loom:briefed", "loom:running", "loom:pr", "loom:answered",
                  "loom:answer-pending", "loom:landing")


class Verdict(NamedTuple):
    kind: str
    reason: str


def _names(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") if isinstance(x, dict) else x or "").lower()
            for x in issue.get("labels") or []]


def _early(issue: Dict[str, Any], names: List[str], events: List[Dict[str, Any]],
           merged_pull: bool) -> Optional[Verdict]:
    """The verdicts that need no claim read: not a ticket, done, held for
    outside work, or waiting on a person."""
    if "pull_request" in issue or "loom:ticket" not in names:
        return Verdict(HELD, "not a ticket")
    if merged_pull or any(n in DONE_STATES for n in names):
        return Verdict(DONE, "the work is already merged or shipped")
    if stale_claims.outside_hold(issue):
        return Verdict(HELD, "held for outside work")
    if human_claims.holder(issue, events) or any(
            n in PERSON_LABELS or n.startswith("needs:") for n in names):
        return Verdict(NEEDS_PERSON, "a person holds or must answer it")
    return None


def _claimed(issue: Dict[str, Any], names: List[str], events: List[Dict[str, Any]],
             pulls: List[Any], now: float, idle_seconds: float,
             own_logins: Iterable[str], own_apps: Iterable[str]) -> Optional[Verdict]:
    """The verdicts a claim decides: this Loom's own, stale or active, or
    another operator's."""
    own = stale_claims.own_claims(issue, own_logins)
    if own:
        label = next(iter(own.values()))
        if stale_claims.lapsed(issue, events, pulls, label, now, idle_seconds, own_apps):
            return Verdict(CLEAR_CLAIM, "its claim saw no activity within the idle limit")
        return Verdict(HELD, "claimed and active")
    if stale_claims.claim_labels_not_ours(names, own_logins) or issue.get("assignees"):
        return Verdict(HELD, "another operator holds it")
    return None


def classify(issue: Dict[str, Any], timeline: Iterable[Any], pull_updates: Iterable[Any],
             now: float, idle_seconds: float, own_logins: Iterable[str],
             merged_pull: bool = False, own_apps: Iterable[str] = ()) -> Verdict:
    """The Verdict for one open issue. `timeline` is its events, `pull_updates`
    the updated_at stamps of the open pull requests naming it, `merged_pull`
    whether a merged pull request names it."""
    names = _names(issue)
    events = [e for e in timeline if isinstance(e, dict)]
    pulls = list(pull_updates)
    early = _early(issue, names, events, merged_pull)
    if early:
        return early
    if pulls:
        return Verdict(HAS_PR, "an open pull request names it")
    claimed = _claimed(issue, names, events, pulls, now, idle_seconds, own_logins, own_apps)
    if claimed:
        return claimed
    if any(n in RUNNING_STATES or n.startswith("wait:") for n in names):
        return Verdict(HELD, "a lane or a wait holds it")
    return Verdict(READY, "no claim, pull request, ask or merged work")


class Tally:
    """Per-repo counts of what a pass did: marked ready, cleared, flagged."""

    def __init__(self) -> None:
        self.counts: Dict[str, Dict[str, int]] = {}

    def add(self, repo: str, verdict: Verdict) -> None:
        row = self.counts.setdefault(repo, {k: 0 for k in KINDS})
        row[verdict.kind] += 1

    def line(self, repo: str) -> str:
        row = self.counts.get(repo) or {k: 0 for k in KINDS}
        return "%s: %d ready, %d claims cleared, %d flagged done" % (
            repo, row[READY], row[CLEAR_CLAIM], row[DONE])


def marker_change(verdict: Verdict, issue: Dict[str, Any]) -> Optional[str]:
    """"add" or "remove" for READY_MARKER, None when it already matches."""
    has = READY_MARKER in _names(issue)
    want = verdict.kind == READY
    return None if has == want else ("add" if want else "remove")
