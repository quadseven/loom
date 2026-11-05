"""Per-repo reads for bus.brain status and resume, each under a time budget.

Status and resume read every repo they are given, and used to print nothing
until the last one finished: on 2026-10-27 status for two repos took 274 s,
past the brain tick's timeout, so the tick saw no output at all (loom#2049).
each() runs one read per repo side by side and hands back each repo as soon
as its read finishes, so the caller can print that repo's section at once,
and a repo whose read runs past the budget is handed back as timed out
instead of holding the whole run.

A timed-out read cannot be stopped: its thread is a daemon, left to finish
on its own, and its answer is dropped. A fresh worker takes its place so the
repos still queued are read.

A slow or dropped read gets one more try before its repo is given up. On a
remote brain on 2026-10-31 the two largest repos took the
whole budget on 3 of 17 ticks, and a connection dropped 3 times running ended
resume on 5, while the same reads took 17 s from the lane machine. A read
that hangs costs 98 s of a 120 s budget in the client's own retries. The
second try shares the pass's kept answers (bus/issue_reads.py), so it sends
only what the first did not finish. A connection that stays down names the
repo as skipped: one repo's reads no longer end the run for the others."""

import os
import queue
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .github import GitHubError

# Seconds one repo's read may take before it is reported as timed out. A
# brain tick reads status in a few minutes at most; one repo's status took
# about 106 s (2026-10-27) before its reads ran side by side.
DEFAULT_BUDGET = 120.0
BUDGET_ENV = "LOOM_REPO_BUDGET"


def budget(env: Optional[Mapping[str, str]] = None, given: Optional[float] = None) -> float:
    """The per-repo budget in seconds: `given` when set, else LOOM_REPO_BUDGET
    when it is a positive number, else DEFAULT_BUDGET."""
    if given is not None:
        if given <= 0:
            raise ValueError("the per-repo budget is a positive number of seconds")
        return float(given)
    raw = (os.environ if env is None else env).get(BUDGET_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BUDGET
    return value if value > 0 else DEFAULT_BUDGET


class PastDeadline(Exception):
    """each() stopped at its `until` before this repo's read finished."""

    def __str__(self) -> str:
        return "deadline"


def not_read(seconds: float, err: Optional[BaseException]) -> str:
    """The reason a repo each() gave up on is named with: `err` when its
    connection failed or the run reached its deadline, else the timeout."""
    if isinstance(err, PastDeadline):
        return str(err)
    return "timed out after %gs (per-repo budget, %s)" % (seconds, BUDGET_ENV) if err is None \
        else "connection failed twice: %s" % err


def _dropped(err: Optional[BaseException]) -> bool:
    """A read that failed for want of a connection (status 0, after the
    client's own retries). An answer from GitHub, a 5xx included, is a fault
    and still ends the run."""
    return isinstance(err, GitHubError) and err.status == 0


class _Run:
    """The workers and answers of one each() call."""

    def __init__(self, order: List[str], read: Callable[[str], Any],
                 clock: Callable[[], float]) -> None:
        self.read = read
        self.clock = clock
        self.todo: "queue.Queue[str]" = queue.Queue()
        for repo in order:
            self.todo.put(repo)
        self.answers: "queue.Queue[Tuple[str, Any, Optional[BaseException]]]" = queue.Queue()
        self.started: Dict[str, float] = {}
        self.lock = threading.Lock()

    def work(self) -> None:
        while True:
            try:
                repo = self.todo.get_nowait()
            except queue.Empty:
                return
            with self.lock:
                self.started[repo] = self.clock()
            try:
                self.answers.put((repo, self.read(repo), None))
            except BaseException as exc:  # handed to the caller's thread
                self.answers.put((repo, None, exc))

    def again(self, repo: str) -> None:
        """Queue `repo` once more, on a worker of its own."""
        with self.lock:
            self.started.pop(repo, None)
        self.todo.put(repo)
        self.spawn()

    def spawn(self) -> None:
        threading.Thread(target=self.work, name="per-repo-read", daemon=True).start()

    def deadlines(self, pending: Set[str], seconds: float) -> Dict[str, float]:
        with self.lock:
            return {r: self.started[r] + seconds for r in pending if r in self.started}

    def answer(self, wait: float) -> Optional[Tuple[str, Any, Optional[BaseException]]]:
        try:
            return self.answers.get(timeout=max(0.01, wait))
        except queue.Empty:
            return None


def each(repos: Sequence[str], read: Callable[[str], Any], seconds: float,
         workers: int, clock: Callable[[], float] = time.monotonic,
         until: Optional[float] = None) -> Iterator[Tuple[str, bool, Any]]:
    """Yield (repo, True, read(repo)) for each repo as its read finishes, or
    (repo, False, err) for a repo given up on: its read ran `seconds` past
    its start (err None), or its connection failed (err the failure). Either
    happens once before it counts, and the repo is read again with a fresh
    budget; a first read that answers late still counts. Any other exception
    a read raises is raised here when that repo comes up, so the repos
    already yielded keep their output. The budget counts from the moment a
    worker starts the repo, not from the start of the run."""
    order = list(dict.fromkeys(repos))
    if until is not None and clock() >= until:
        for repo in order:
            yield repo, False, PastDeadline()
        return
    run = _Run(order, read, clock)
    for _ in range(min(workers, len(order))):
        run.spawn()
    pending = set(order)
    retried: Set[str] = set()
    while pending:
        now = clock()
        if until is not None and now >= until:
            for repo in [r for r in order if r in pending]:
                yield repo, False, PastDeadline()
            return
        deadlines = run.deadlines(pending, seconds)
        for repo in [r for r in order if deadlines.get(r, now + 1) <= now]:
            # The stuck worker keeps its thread; the queue still needs one.
            if repo not in retried:
                retried.add(repo)
                run.again(repo)
                continue
            pending.discard(repo)
            run.spawn()
            yield repo, False, None
        left = [d - now for d in deadlines.values() if d > now]
        wait = min(left, default=seconds)
        if until is not None:
            wait = min(wait, until - now)
        got = run.answer(wait) if pending else None
        if got is None or got[0] not in pending:
            continue  # nothing yet, or a timed-out read that finished late
        if _dropped(got[2]) and got[0] not in retried:
            retried.add(got[0])
            run.again(got[0])
            continue
        pending.discard(got[0])
        if _dropped(got[2]):
            yield got[0], False, got[2]
            continue
        if got[2] is not None:
            raise got[2]
        yield got[0], True, got[1]
