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
repos still queued are read."""

import os
import queue
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

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


def timed_out(seconds: float) -> str:
    """The reason a timed-out repo is named with."""
    return "timed out after %gs (per-repo budget, %s)" % (seconds, BUDGET_ENV)


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
         workers: int, clock: Callable[[], float] = time.monotonic
         ) -> Iterator[Tuple[str, bool, Any]]:
    """Yield (repo, True, read(repo)) for each repo as its read finishes, or
    (repo, False, None) once its read has run `seconds` past its start. An
    exception a read raises is raised here when that repo comes up, so the
    repos already yielded keep their output. The budget counts from the
    moment a worker starts the repo, not from the start of the run."""
    order = list(dict.fromkeys(repos))
    run = _Run(order, read, clock)
    for _ in range(min(workers, len(order))):
        run.spawn()
    pending = set(order)
    while pending:
        now = clock()
        deadlines = run.deadlines(pending, seconds)
        for repo in [r for r in order if deadlines.get(r, now + 1) <= now]:
            pending.discard(repo)
            run.spawn()  # the stuck worker keeps its thread; the queue still needs one
            yield repo, False, None
        left = [d - now for d in deadlines.values() if d > now]
        got = run.answer(min(left, default=seconds)) if pending else None
        if got is None or got[0] not in pending:
            continue  # nothing yet, or a timed-out read that finished late
        pending.discard(got[0])
        if got[2] is not None:
            raise got[2]
        yield got[0], True, got[1]
