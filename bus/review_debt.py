"""Review and guard debt, as facts (loom#2429).

Review is meant to be routine and turn mistakes into checks, but nothing made
it happen: the fleet goal's Loom review duty ran once, with no cadence and no
holder, and nobody could tell whether a guard ever shipped. Once an hour the
station reads the goal home and the `loom:factory` tickets and keeps:

- the newest Loom review journal: a goal home comment whose first line starts
  "Loom review", written by a listed author (LOOM_BRIEF_AUTHORS and
  LOOM_REVIEW_AUTHORS, whose default is the fleet's brains). Over OVERDUE old
  raises one rolling invariant, `review-overdue` (bus/invariants.py);
- each guard ticket, one with a line starting "The guard": open, shipped (a
  merged pull request that names it, bus/close_audit.py, whose merge commit is
  on its base repo's default branch and which adds or changes a test file), or
  closed with no such pull request, the rolling `guard-no-check`. A timeline or
  file list cut short reads as no check unless a test is found in what was read.
  A ticket still unreadable after GUARD_TRIES is the rolling `guard-unreadable`.

`line` is the journal scoreboard's one line, read live so any brain can print
it:

    python3 -m bus.review_debt line OWNER/NAME#N

A failed read says unknown, never healthy, and the last known review time
stands, so a finding does not clear on a read that failed. It makes no AI call.
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import change_class, close_audit, tickets
from .github import GitHub, GitHubError, RateLimited, auth_from_env

STATE_FILE = "review-debt.json"
EVERY = 3600
DAY = 24 * 3600
OVERDUE = 7 * DAY
# How far back the first read looks; no entry reads as this old.
WINDOW = 14 * DAY
# What one pass may spend on tickets; the rest wait for the next pass.
READS_PER_PASS = 60
# The most one ticket can read: the timeline, and each pull request, its files
# and where its merge commit is. Reserved before a ticket starts; a pass is
# charged what it read.
TICKET_READS = close_audit.TIMELINE_PAGES + 3 * close_audit.MAX_PULLS + 1
GUARD_TRIES = 5
FILES_READ = 100
LIVE_BUDGET = 2000
FLEET_BRAINS = "agent-anthropic-claude-code[bot] agent-meta-muse[bot] agent-openai-dot[bot]"
REVIEW_RE = re.compile(r"\A\s*(?:#+\s*)?Loom review\b", re.I)
GUARD_RE = re.compile(r"^The guard\b", re.M)
SHIPPED, NO_CHECK, OPEN = "shipped", "no-check", "open"
_TEST_WRITES = ("added", "modified", "renamed", "changed", "copied")


def review_authors(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The logins besides the station's trusted authors that write reviews."""
    values = os.environ if env is None else env
    return str(values.get("LOOM_REVIEW_AUTHORS", FLEET_BRAINS)).replace(",", " ").split()


def is_review(comment: Any, authors: Sequence[str]) -> bool:
    if not isinstance(comment, dict) or not REVIEW_RE.match(str(comment.get("body") or "")):
        return False
    login = str((comment.get("user") or {}).get("login") or "")
    return login.lower() in {a.lower() for a in authors}


def newest_review(comments: Sequence[Any], authors: Sequence[str]) -> Optional[float]:
    found = [close_audit.epoch(c.get("created_at")) for c in comments if is_review(c, authors)]
    return max([at for at in found if at is not None], default=None)


def _read_review(gh: Any, repo: str, goal: int, state: Dict[str, Any], now: float,
                 authors: Sequence[str]) -> None:
    known = state.get("review_at")
    start = float(state.get("read_at") or now - WINDOW) - EVERY
    # Whole hours, so the same URL serves the hour.
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start // 3600 * 3600))
    state["review_read"] = False
    comments = gh.paginate("/repos/%s/issues/%d/comments?since=%s&per_page=100"
                           % (repo, goal, since))
    newest = newest_review(comments, authors)
    state["review_none"] = known is None and newest is None or (
        bool(state.get("review_none")) and newest is None)
    state["review_at"] = max([x for x in (known, newest) if x is not None]
                             or [now - WINDOW])
    state["review_read"] = True
    state["read_at"] = now


def _pull_place(gh: Any, repo: str, pull: Mapping[str, Any]) -> str:
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    held = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    branch, merge = str(held.get("default_branch") or ""), str(pull.get("merge_commit_sha") or "")
    if not branch or not merge:
        return close_audit.UNSEEN
    return close_audit._place(gh, close_audit._pull_repo(repo, pull), branch, merge)


def _changes_a_test(gh: Any, repo: str, pull: Mapping[str, Any]) -> bool:
    files = gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=%d" % (
        close_audit._pull_repo(repo, pull), int(pull["number"]), FILES_READ)).data or []
    return any(change_class.is_test(str(f.get("filename") or ""))
               and f.get("status") in _TEST_WRITES for f in files)


def _ticket_kind(gh: Any, repo: str, issue: Mapping[str, Any], fleet: Sequence[str]) -> str:
    number = int(issue["number"])
    pulls = close_audit._merged_pulls(gh, repo, number, fleet,
                                      tickets.rewritten_source(issue.get("body")))[0]
    unseen = False
    for pull in pulls:
        where = _pull_place(gh, repo, pull)
        unseen = unseen or where == close_audit.UNSEEN
        if where == close_audit.ON and _changes_a_test(gh, repo, pull):
            return SHIPPED
    if unseen:
        raise ValueError("where a merged pull request landed could not be read")
    return OPEN if issue.get("state") == "open" else NO_CHECK


def _read_guards(gh: Any, repo: str, state: Dict[str, Any], fleet: Sequence[str],
                 say: Callable[[str], None], budget: int, now: float) -> None:
    state["guards_read"] = False
    listed = gh.paginate("/repos/%s/issues?labels=loom:factory&state=all&per_page=100" % repo)
    guards = {str(i["number"]): i for i in listed if isinstance(i, dict)
              and GUARD_RE.search(str(i.get("body") or "")) and "pull_request" not in i
              and i.get("state_reason") != "not_planned"}
    old = state.get("guards") if isinstance(state.get("guards"), dict) else {}
    # A row keeps its kind until a read replaces it, so a finding cannot clear
    # on a read that failed or waited; a stale row reads as unknown.
    kept: Dict[str, Any] = {}
    for key, issue in guards.items():
        seen = old.get(key) if isinstance(old.get(key), dict) else {}
        kept[key] = dict(seen, created_at=issue.get("created_at"),
                         fresh=bool(seen.get("kind"))
                         and seen.get("updated_at") == issue.get("updated_at"))
    state["guards"] = kept
    counted = close_audit._Counted(gh)
    due = [key for key in guards if not kept[key]["fresh"]]
    # Never read first, then the fewest failures: a ticket that cannot be read
    # never holds the budget from the others.
    due.sort(key=lambda key: (int(kept[key].get("fails") or 0), int(key)))
    for key in due:
        if budget - counted.spent < TICKET_READS:
            break
        issue = guards[key]
        try:
            kept[key].update(kind=_ticket_kind(counted, repo, issue, fleet),
                             updated_at=issue.get("updated_at"), fresh=True, fails=0)
        except RateLimited:
            raise
        except (GitHubError, OSError, ValueError) as err:
            kept[key].update(fails=int(kept[key].get("fails") or 0) + 1, tried=now)
            say("review debt: guard #%s could not be read: %s" % (key, err))
    state["guards_read"] = True


def run_pass(gh: Any, repo: str, goal: int, state: Dict[str, Any], now: float,
             authors: Sequence[str], say: Callable[[str], None],
             fleet: Sequence[str] = (), budget: int = READS_PER_PASS) -> None:
    """Refresh `state`, at most every EVERY seconds, and log the scoreboard.
    A pass stopped at the station's request target runs again on the next."""
    last = state.get("last")
    if isinstance(last, (int, float)) and 0 <= now - last < EVERY:
        return
    for read in (lambda: _read_review(gh, repo, goal, state, now, authors),
                 lambda: _read_guards(gh, repo, state, fleet, say, budget, now)):
        try:
            read()
        except RateLimited:
            raise
        except (GitHubError, OSError, ValueError) as err:
            say("review debt: a read failed: %s" % err)
    state["last"] = now
    say("review debt: %s" % line(state, now))


def review_at(state: Mapping[str, Any]) -> Optional[float]:
    """The newest review time known; None before any read succeeded."""
    at = state.get("review_at")
    return float(at) if isinstance(at, (int, float)) else None


def _guards(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    found = state.get("guards")
    return found if isinstance(found, dict) else {}


def no_check(state: Mapping[str, Any]) -> List[int]:
    """The guard tickets closed with no merged pull request that changed a test."""
    return sorted(int(k) for k, v in _guards(state).items() if v.get("kind") == NO_CHECK)


def unreadable(state: Mapping[str, Any]) -> List[int]:
    """The guard tickets still unreadable after GUARD_TRIES reads."""
    return sorted(int(k) for k, v in _guards(state).items()
                  if not v.get("fresh") and int(v.get("fails") or 0) >= GUARD_TRIES)


def _days(seconds: float) -> str:
    days = int(max(0, seconds) // DAY)
    return "1 day" if days == 1 else "%d days" % days


def _guard_words(state: Mapping[str, Any], now: float) -> str:
    rows = list(_guards(state).values())
    if not state.get("guards_read") or any(not r.get("fresh") for r in rows):
        return "guards unknown"
    count = {kind: len([r for r in rows if r.get("kind") == kind])
             for kind in (OPEN, SHIPPED, NO_CHECK)}
    ages = [close_audit.epoch(r.get("created_at")) for r in rows if r.get("kind") == OPEN]
    ages = [now - at for at in ages if at is not None]
    return "guards %d open, %d shipped, %d closed with no check%s" % (
        count[OPEN], count[SHIPPED], count[NO_CHECK],
        ", oldest open %s" % _days(max(ages)) if ages else "")


def line(state: Mapping[str, Any], now: float) -> str:
    """The journal scoreboard's line. Unknown is said, never left out."""
    at = review_at(state)
    if at is None or not state.get("review_read"):
        review = "last review unknown"
    elif state.get("review_none"):
        review = "no review in %s or more" % _days(WINDOW)
    else:
        review = "last review %s ago" % _days(now - at)
    return "%s; %s" % (review, _guard_words(state, now))


def live_line(gh: Any, goal_ref: str, authors: Sequence[str], fleet: Sequence[str],
              now: float) -> str:
    """The scoreboard line read now, for a brain that holds no station state."""
    repo, _, number = goal_ref.partition("#")
    state: Dict[str, Any] = {}
    run_pass(gh, repo, int(number), state, now, authors, lambda text: None, fleet,
             budget=LIVE_BUDGET)
    return line(state, now)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "line" or not re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", args[1]):
        print("usage: python3 -m bus.review_debt line OWNER/NAME#N", file=sys.stderr)
        return 2
    gh = GitHub(auth_from_env())
    print(live_line(gh, args[1], os.environ.get("LOOM_BRIEF_AUTHORS", "").split() + review_authors(),
                    close_audit.fleet_repos(), time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
