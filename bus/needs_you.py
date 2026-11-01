"""What needs a person now, read from the tickets (loom#2387).

    python3 -m bus.needs_you [--repo OWNER/NAME ...]

A label says an ask was made, not that it is still open. A brain that counted
needs:human, carried a list between ticks, or read one page of comments told
the operator something needed them when the answer was already on the ticket:
GitHub's comments API returns the oldest 30 by default, so `.[-1]` of a read
with no pagination is not the newest comment.

This command writes nothing. For each open issue or pull request that holds
an ask label or a needs:<login> label it pages every comment and event, runs
bus/ask_state.py, and puts the item in one section:

- answered: the answer is in the ticket; the label pass has not caught up.
- verification: the station's own tag (criteria_gate.ask), not a person's
  question. A later person ask replaces it.
- waiting: a wait:* label, or a Blocked on section naming an open issue.
- no written ask: a label with no ask in words. Nobody can act on it.
- check first: open, but a pull request naming the ticket merged after the
  ask. That may be the answer: read it. A merged pull request is not proof,
  so the ask stays open and stays printed.
- needs you: a written ask to a person, open, with no later answer.

An item that could not be read is listed as unread and the command exits 1:
an unread ticket is never a ticket with nothing to do. Repos, their pages and
their items are read on a few threads at once, and the report is printed by
--deadline (inside a Muse tick, loom#2394): a repo or item not read by then is
listed as unread too. The repo with the most open issues has its items read
first, so a slow network leaves the light repos, not the heavy one, for last.

Answers are kept between runs under $LOOM_HOME/state/github-cache, the folder
bus.metrics keeps its answers in, and an answer unused for a day is removed.
Every read sends the ETag it holds, so a list that has not changed is a 304,
which GitHub does not count. A ticket's comments and events are kept whole
under the listing's updated_at: while that has not moved, the next run reads
neither. That holds across tokens too; GitHub's ETags on a private repo
differ per token, and each Muse tick has a token of its own.
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from dashboard import operators

from . import answer, ask_state, brain, criteria_gate, decision_claim, issue_reads, status
from .github import GitHub, GitHubError, auth_from_env, urllib_transport
from .labels import needs_human

NEEDS_YOU, CHECK_FIRST, NO_ASK = "needs you", "check first", "no written ask"
WAITING, VERIFICATION, ANSWERED = "waiting", "verification", "answered"
HEADINGS = (
    (NEEDS_YOU, "Needs you"),
    (CHECK_FIRST, "Check first: a pull request naming it merged after the ask"),
    (NO_ASK, "No written ask: a label, not a question"),
    (WAITING, "Waiting: nobody can act on these today"),
    (VERIFICATION, "Station verification tags: not asks to a person"),
    (ANSWERED, "Answered: the label has not caught up"),
)
THE_OPERATOR = "the operator"
_ASK_SIGNS = ("needs-operator", "needs:human", "loom:human")
# Muse gives the command 45 seconds (loom#2394): the report is printed by
# DEADLINE, with what was not read by then named. WORKERS reads run at once
# (GitHub allows far more). A read that hangs is given up after
# READ_TIMEOUT, so its retries still fit before the deadline.
DEADLINE = 40.0
WORKERS = 12
READ_TIMEOUT = 10.0


# Where the answers are kept between runs: the folder bus.metrics keeps its
# GitHub answers in. An answer no run has used for CACHE_KEEP seconds is
# removed, as the station prunes its own (bus/station.py).
CACHE_KEEP = 24 * 3600


def cache_dir(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    home = env.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")
    return os.path.join(home, "state", "github-cache")


def _short_transport(method: str, url: str, headers: Dict[str, str],
                     body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    return urllib_transport(method, url, headers, body, READ_TIMEOUT)


class Item(NamedTuple):
    ref: str            # OWNER/NAME#N
    section: str        # one of HEADINGS
    state: str          # ask_state's answered, waiting or open
    who: Tuple[str, ...]
    ask: str            # verbatim, "" when none is written
    asked_at: str       # "" when it cannot be read
    url: str
    latest_by: str      # "" with no comment
    latest_at: str
    claims: Tuple[str, ...] = ()
    answer: Optional[str] = None
    merged: Tuple[str, ...] = ()


def _names(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") if isinstance(x, dict) else x) for x in issue.get("labels") or []]


def waits_on_person(names: Sequence[str]) -> bool:
    """The labels ask a person: an ask label or a needs:<login> label."""
    return needs_human(names) or bool(operators.needs_of(names))


def addressed_to(names: Sequence[str]) -> Tuple[str, ...]:
    """Who the labels name: each needs:<login>, and the operator for a label
    that names nobody."""
    people = list(operators.needs_of(names))
    return tuple(people + ([THE_OPERATOR] if set(names) & set(_ASK_SIGNS) else []))


def _stamp(when: Optional[datetime]) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if when else ""


def written_ask(issue: Dict[str, Any], comments: Sequence[Any],
                names: Sequence[str]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(the ask as written, the comment holding it): the newest needs:human
    comment, else the body's "## Ask" section, else the newest comment that
    names the ask. ("", None) when none is written."""
    chosen = operators.ask_comment(comments, operators.ask_names(names), strict=True)
    marked = chosen is not None and answer.HUMAN_MARKER in str(chosen.get("body") or "")
    section = operators.body_ask(issue.get("body"))
    if section and not marked:
        return section, None
    return (operators.ask_full(chosen.get("body")), chosen) if chosen is not None else ("", None)


def merged_after(timeline: Iterable[Any], since: Optional[datetime]) -> Tuple[str, ...]:
    """Each pull request that names the ticket and merged after the ask."""
    found: List[str] = []
    for event in timeline:
        if since is None or not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        merged = decision_claim.when((source.get("pull_request") or {}).get("merged_at"))
        if merged is not None and merged > since:
            found.append("%s#%s" % ((source.get("repository") or {}).get("full_name") or "?",
                                    source.get("number")))
    return tuple(dict.fromkeys(found))


def _station_tag(chosen: Optional[Dict[str, Any]], trusted: Sequence[str]) -> bool:
    """The ask is the station's verification tag. Only a trusted author's
    counts: anyone can type the words on a public repo."""
    if chosen is None or not criteria_gate.is_verification_ask(chosen.get("body")):
        return False
    return brain.comment_login(chosen).lower() in {str(t).lower() for t in trusted}


def _section(said: str, tag: bool, ask: str) -> str:
    """Where an item goes before its merged pull requests are read."""
    if said == ask_state.ANSWERED:
        return ANSWERED
    if tag:
        return VERIFICATION
    if said == ask_state.WAITING:
        return WAITING
    return NEEDS_YOU if ask else NO_ASK


def judge(repo: str, issue: Dict[str, Any], comments: Sequence[Any], events: Sequence[Any],
          trusted: Sequence[str], ops: Sequence[str], blockers: Sequence[int] = (),
          timeline: Iterable[Any] = (), answerers: Sequence[str] = ()) -> Item:
    """One item's place, from its whole comment and event history (oldest
    first) and, for an item that would need you, its timeline. `answerers`
    are the LOOM_ASK_ANSWERERS logins; the station tag still reads `trusted`."""
    names = _names(issue)
    said = ask_state.state(issue, comments, events, trusted, ops, blockers, answerers)
    ask, chosen = written_ask(issue, comments, names)
    section = _section(said, _station_tag(chosen, trusted), ask)
    since = ask_state.asked_at(comments, events)
    merged = merged_after(timeline, since) if section == NEEDS_YOU else ()
    real = [c for c in comments if isinstance(c, dict) and not c.get(status.EXPANDED)]
    latest = real[-1] if real else {}
    return Item("%s#%s" % (repo, issue.get("number")), CHECK_FIRST if merged else section, said,
                addressed_to(names), ask,
                str((chosen or {}).get("created_at") or "") or _stamp(since),
                str((chosen or {}).get("html_url") or issue.get("html_url") or ""),
                brain.comment_login(latest), str(latest.get("created_at") or ""),
                tuple(n for n in names if n.startswith("claim:")),
                ask_state.answer_of(issue, comments, events, trusted, ops, answerers), merged)


def unchanged_since(gh: Any, issue: Dict[str, Any]) -> str:
    """What says a ticket's comments and events are as last read: the
    listing's updated_at, which a new comment, a comment edit, a label and a
    body edit all move (loom#2394), with the logins whose status comments
    are expanded, since the kept lists are the expanded ones. "" when the
    listing carries no updated_at: that ticket is read whole."""
    stamp = issue.get("updated_at")
    if not isinstance(stamp, str) or not stamp:
        return ""
    return "%s %s" % (stamp, ",".join(sorted(getattr(gh, "status_authors", None) or ())))


def _whole(gh: Any, path: str, since: str) -> List[Any]:
    """Every item of the list at `path`: the copy kept on the last run when
    the ticket has not changed since, else read and kept. The timeline is
    never read this way: a pull request that names the ticket and merges
    does not move the ticket's updated_at."""
    recall = getattr(gh, "recall", None) if since else None
    kept = recall(path, since) if callable(recall) else None
    if isinstance(kept, list):
        return kept
    found = gh.paginate(path)
    if since and callable(getattr(gh, "remember", None)):
        gh.remember(path, since, found)
    return found


def read_item(gh: Any, repo: str, issue: Dict[str, Any], trusted: Sequence[str],
              ops: Sequence[str], answerers: Sequence[str] = ()) -> Item:
    """One item, read whole. Every list goes through paginate: one page of
    comments is the oldest ones."""
    base = "/repos/%s/issues/%d" % (repo, int(issue["number"]))
    since = unchanged_since(gh, issue)
    comments = _whole(gh, base + "/comments?per_page=100", since)
    events = _whole(gh, base + "/events?per_page=100", since)
    blockers = ask_state.open_blockers(gh.rest, repo, issue)
    item = judge(repo, issue, comments, events, trusted, ops, blockers, answerers=answerers)
    if item.section != NEEDS_YOU:
        return item
    return judge(repo, issue, comments, events, trusted, ops, blockers,
                 gh.paginate(base + "/timeline?per_page=100"), answerers)


PER_PAGE = 100
LISTING = "/repos/%s/issues?state=open&per_page=" + str(PER_PAGE)


def waiting(issues: Iterable[Any]) -> List[Dict[str, Any]]:
    """The open issues and pull requests that wait on a person. One list of
    every open issue costs fewer reads than one list per asking label, since
    each needs:<login> is a label of its own."""
    return [i for i in issues if isinstance(i, dict) and waits_on_person(_names(i))]


# A name, a callable that returns more tasks, and optionally a weight: the
# heavier task starts first, and tasks of one weight start in the order given.
Task = Tuple[Any, ...]
FIRST = float("inf")  # a listing or a page: it finds the work, so it goes first


def drain(first: Sequence[Task], workers: int, seconds: float) -> List[str]:
    """Run each task on at most `workers` threads, heaviest first, until
    every task is done or `seconds` pass. The names of the tasks not done by
    then, sorted.

    The threads are daemons: a read stuck past the deadline is left behind,
    and the process still exits once the report is written."""
    cond = threading.Condition()
    todo: List[Tuple[float, int, str, Callable[[], List[Any]]]] = []
    pending: set = set()
    order = itertools.count()

    def add(task: Task) -> None:
        weight = float(task[2]) if len(task) > 2 else 0.0
        heapq.heappush(todo, (-weight, next(order), task[0], task[1]))
        pending.add(task[0])

    for task in first:
        add(task)

    def work() -> None:
        while True:
            with cond:
                while not todo and pending:
                    cond.wait()
                if not todo:
                    return
                _, _, name, task = heapq.heappop(todo)
            more = task()
            with cond:
                for added in more:
                    add(added)
                pending.discard(name)
                cond.notify_all()

    for _ in range(max(1, workers)):
        threading.Thread(target=work, daemon=True).start()
    end = time.monotonic() + seconds
    with cond:
        while pending:
            left = end - time.monotonic()
            if left <= 0:
                break
            cond.wait(left)
        return sorted(pending)


def _quoted(text: str) -> List[str]:
    return ["    > " + line for line in re.split(r"\r?\n", text.strip())]


def _lines(item: Item) -> List[str]:
    """One item: repo#N, state, the ask, who, its time, the latest comment."""
    out = ["- %s: %s" % (item.ref, item.state)]
    out.append("  to %s, asked %s" % (" and ".join(item.who) or "nobody named",
                                      item.asked_at or "at an unreadable time"))
    out.append("  latest comment: %s" % ("%s at %s" % (item.latest_by, item.latest_at)
                                         if item.latest_at else "none"))
    if item.claims:
        out.append("  held by %s" % ", ".join(item.claims))
    if item.merged:
        out.append("  merged after the ask: %s" % ", ".join(item.merged))
    if item.answer is not None:
        out.append("  answer:")
        out += _quoted(item.answer) if item.answer else ["    > a Decision heading in the body"]
    if item.ask:
        out.append("  ask (%s):" % item.url if item.url else "  ask:")
        out += _quoted(item.ask)
    return out


def render(items: Sequence[Item], unread: Sequence[str]) -> str:
    """The report. A section with nothing in it is left out, except Needs you."""
    out: List[str] = []
    for section, heading in HEADINGS:
        found = sorted((i for i in items if i.section == section), key=lambda i: i.ref)
        if not found and section != NEEDS_YOU:
            continue
        out.append("%s: %s" % (heading, len(found) if found or unread else "nothing"))
        for item in found:
            out += _lines(item)
        out.append("")
    if unread:
        out.append("Unread, so this report is incomplete: %d" % len(unread))
        out += ["- " + line for line in unread]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def run(gh: Any, repos: Sequence[str], trusted: Sequence[str], ops: Sequence[str],
        answerers: Sequence[str] = (), deadline: Optional[float] = None,
        workers: Optional[int] = None) -> Tuple[str, int]:
    """(the report, the exit code). Repos and items are read on WORKERS
    threads; what is not read by the deadline (DEADLINE seconds when None)
    is named as unread, so a slow repo is a short report that says so, never
    a report that does not come (loom#2394)."""
    lock = threading.Lock()
    items: List[Item] = []
    unread: List[str] = []
    # Each issue is read once a run: a listed ticket's own body read (its
    # comments' status section) and a Blocked on issue many asks name.
    reads = issue_reads.once(gh)

    def one(repo: str, issue: Dict[str, Any]) -> Callable[[], List[Any]]:
        def task() -> List[Any]:
            try:
                found = read_item(reads, repo, issue, trusted, ops, answerers)
            except Exception as err:  # noqa: BLE001 - an unread ticket is named, never dropped
                with lock:
                    unread.append("%s#%s: %s" % (repo, issue.get("number"), err or repr(err)))
            else:
                with lock:
                    items.append(found)
            return []
        return task

    def items_of(repo: str, issues: List[Any], size: int) -> List[Any]:
        """The asking items of one page, weighed by the repo's listing size:
        the repo with the most open issues has the most asks to read, so
        its reads start first and do not trail past the deadline."""
        reads.keep_listed(repo, issues)
        return [("%s#%s" % (repo, i.get("number")), one(repo, i), size) for i in waiting(issues)]

    def page(repo: str, url: str, number: int, final: bool,
             size: int) -> Callable[[], List[Any]]:
        def task() -> List[Any]:
            try:
                return items_of(repo, gh.read_page(url, final), size)
            except Exception as err:  # noqa: BLE001 - an unread page is named, never dropped
                with lock:
                    unread.append("%s: open issues page %d: %s" % (repo, number, err or repr(err)))
                return []
        return task

    def listing(repo: str) -> Callable[[], List[Any]]:
        """The first page of the repo's open issues; the later pages and
        the items are tasks of their own."""
        def task() -> List[Any]:
            try:
                first, later = gh.page_plan(LISTING % repo)
            except Exception as err:  # noqa: BLE001 - an unread repo is named, never dropped
                with lock:
                    unread.append("%s: %s" % (repo, err or repr(err)))
                return []
            size = PER_PAGE * (len(later) + 1) if later else len(first)
            pages = [("%s open issues page %d" % (repo, n),
                      page(repo, url, n, n == len(later) + 1, size), FIRST)
                     for n, url in enumerate(later, 2)]
            return pages + items_of(repo, first, size)
        return task

    seconds = DEADLINE if deadline is None else deadline
    late = drain([(repo, listing(repo), FIRST) for repo in dict.fromkeys(repos)],
                 WORKERS if workers is None else workers, seconds)
    with lock:
        got, missed = list(items), list(unread)
    missed += ["%s: not read within the %gs deadline" % (name, seconds) for name in late]
    return render(got, missed), 1 if missed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.needs_you")
    parser.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                        help="a repo to read, repeatable (default: LOOM_REPOS)")
    parser.add_argument("--deadline", type=float, default=DEADLINE, metavar="SECONDS",
                        help="print what was read by then and name the rest as unread "
                             "(default %(default)gs, inside a Muse tick)")
    parser.add_argument("--workers", type=int, default=WORKERS, metavar="N",
                        help="reads sent at once (default %(default)d); on a slow network "
                             "more workers finish a first run sooner")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    repos = args.repo or os.environ.get("LOOM_REPOS", "").split()
    trusted = brain.trusted_authors()
    if not repos or not trusted:
        # With no trusted login no brain's record counts, and every answered
        # ask would read as open.
        print("error: set LOOM_BRIEF_AUTHORS, and LOOM_REPOS or --repo", file=sys.stderr)
        return 2
    gh = GitHub(auth_from_env(), transport=_short_transport, cache_dir=cache_dir())
    gh.status_in_body = True  # read as the brain reads (bus.brain.main)
    gh.status_authors |= {login.lower() for login in trusted}
    text, code = run(gh, repos, trusted, answer.operators(trusted=trusted), ask_state.answerers(),
                     deadline=args.deadline, workers=args.workers)
    gh.prune_cache(CACHE_KEEP)
    sys.stdout.write(text)
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
