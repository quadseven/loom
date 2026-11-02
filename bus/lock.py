"""The brain's tick lock: a branch whose commit message names the holder.

Creating a ref that exists fails with 422, so only one holder wins. The
message is `holder=<name> expires=<UTC> acquired=<UTC>`, with ` run=<id>` when
the caller named its run. Every attempt answers with the holder record, so a
brain can tell its own lock from a peer's (loom#2275): a re-attempt that finds
the caller's own live lock is acquired, and a peer's lock is told with its
holder and how long it has held it.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from .github import GitHub

LOCK_BRANCH = "loom-lock/brain"
HOLDER_RE = re.compile(r"[A-Za-z0-9._-]+")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Seconds between tries while `attempt_waiting` waits out a peer's lock.
WAIT_STEP = 60


def _check_holder(holder: str) -> None:
    if not isinstance(holder, str) or not HOLDER_RE.fullmatch(holder):
        raise ValueError("holder may only use letters, digits, '.', '_' and '-'")


def _check_run(run: str) -> None:
    if not isinstance(run, str) or (run and not HOLDER_RE.fullmatch(run)):
        raise ValueError("run may only use letters, digits, '.', '_' and '-'")


def _utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _field(pattern: str, message: str) -> Optional[str]:
    found = re.search(pattern, message)
    return found.group(1) if found else None


@dataclass(frozen=True)
class Holder:
    """The lock's holder record, read from the lock commit message."""
    name: str
    expires: Optional[datetime]
    # When the holder took the lock. A lock taken before this field existed
    # falls back to its tick record's start; None when neither is there.
    acquired: Optional[datetime]
    run: str = ""
    # The tick step the holder last recorded, or "" when none.
    step: str = ""

    def is_caller(self, holder: str, run: str = "") -> bool:
        """Whether this is the caller's own lock: the same brain, and the same
        run when the caller names one. A caller that names no run owns every
        lock under its name (one brain ticks once at a time)."""
        return self.name == holder and (not run or self.run == run)

    def live(self, now: datetime) -> bool:
        # An unreadable expiry counts as held: never steal a lock we cannot read.
        return self.expires is None or self.expires > now


TICK_MARKER = "loom:tick="


def _tick_record(message: str) -> dict:
    """The tick record bus.brain step writes under the lock line, or {}."""
    for line in message.splitlines():
        if line.startswith(TICK_MARKER):
            try:
                record = json.loads(line[len(TICK_MARKER):])
            except ValueError:
                return {}
            return record if isinstance(record, dict) else {}
    return {}


def _holder_from(message: str) -> Holder:
    # The holder fields live on the first line; the tick record's notes
    # below it are free text and are never read as fields.
    first = message.splitlines()[0] if message else ""
    record = _tick_record(message)
    started = record.get("started_at")
    step = record.get("step")
    return Holder(name=_field(r"(?:^|\s)holder=(\S+)", first) or "",
                  expires=_parse_time(_field(r"(?:^|\s)expires=(\S+)", first)),
                  acquired=(_parse_time(_field(r"(?:^|\s)acquired=(\S+)", first))
                            or _parse_time(started if isinstance(started, str) else None)),
                  run=_field(r"(?:^|\s)run=(\S+)", first) or "",
                  step=step if isinstance(step, str) and HOLDER_RE.fullmatch(step) else "")


def read_holder(gh: GitHub, repo: str) -> Optional[Holder]:
    """The current lock's holder record, or None when there is no lock."""
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, LOCK_BRANCH),
                  ok=(404,))
    if ref.status == 404:
        return None
    sha = ref.data["object"]["sha"]
    commit = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, sha), ok=(404,))
    if commit.status == 404:
        return None
    return _holder_from(str(commit.data.get("message") or ""))


def _read_lock(gh: GitHub, repo: str) -> Optional[Tuple[str, Optional[datetime]]]:
    """Return (holder, expiry) of the current lock, or None when absent."""
    current = read_holder(gh, repo)
    return None if current is None else (current.name, current.expires)


def _age(since: Optional[datetime], now: datetime) -> str:
    if since is None:
        return "for an unknown time (the lock records no start)"
    minutes = max(0, int((now - since).total_seconds() // 60))
    return "%dm since %s" % (minutes, since.strftime(TIME_FORMAT))


@dataclass(frozen=True)
class Attempt:
    """One lock attempt's answer: whether the caller holds the lock now, and
    the holder record it found or wrote."""
    acquired: bool
    holder: Holder
    caller: str
    # True when the lock was already the caller's own (a re-attempt).
    already: bool = False
    # Minutes spent waiting for a peer's lock before this answer.
    waited: int = 0

    def line(self, now: Optional[datetime] = None) -> str:
        """The one line the CLI prints. It always names the holder, says
        whether the lock is the caller's own, and how long it has been held."""
        now = _utc(now)
        h = self.holder
        expires = h.expires.strftime(TIME_FORMAT) if h.expires else "unreadable"
        run = ", run %s" % h.run if h.run else ""
        waited = "; waited %dm" % self.waited if self.waited else ""
        if self.acquired and self.already:
            return ("lock acquired: already yours (holder %s%s), held %s, expires %s; "
                    "this is not a peer's lock, so go on with the tick%s"
                    % (h.name, run, _age(h.acquired, now), expires, waited))
        if self.acquired:
            return "lock acquired: yours (holder %s%s), expires %s%s" % (h.name, run, expires, waited)
        if not h.name:
            return "lock held by an unnamed holder (not yours: you are %s), expires %s%s" % (
                self.caller, expires, waited)
        same = " (same holder name, another run)" if h.name == self.caller else ""
        step = ", at step %s" % h.step if h.step else ""
        return ("lock held by %s%s%s (not yours: you are %s), held %s%s, expires %s%s"
                % (h.name, run, same, self.caller, _age(h.acquired, now), step, expires, waited))


def _create_ref(gh: GitHub, repo: str, sha: str) -> bool:
    resp = gh.rest("POST", "/repos/%s/git/refs" % repo,
                   {"ref": "refs/heads/" + LOCK_BRANCH, "sha": sha}, ok=(422,))
    return resp.status != 422


def attempt(gh: GitHub, repo: str, holder: str, minutes: int = 15,
            now: Optional[datetime] = None, run: str = "") -> Attempt:
    """Try once to take the lock. A live lock that is the caller's own (same
    holder, and same run when `run` is given) is acquired, not held: a
    re-attempt after a lost or misread answer must not stand the brain down
    against itself."""
    _check_holder(holder)
    _check_run(run)
    now = _utc(now)
    main = gh.rest("GET", "/repos/%s/git/ref/heads/main" % repo)
    base = main.data["object"]["sha"]
    tree = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, base)).data["tree"]["sha"]
    expires = now + timedelta(minutes=minutes)
    message = "holder=%s expires=%s acquired=%s" % (
        holder, expires.strftime(TIME_FORMAT), now.strftime(TIME_FORMAT))
    if run:
        message += " run=%s" % run
    mine = Holder(holder, expires, now, run)
    commit = gh.rest("POST", "/repos/%s/git/commits" % repo, {
        "message": message,
        "tree": tree,
        "parents": [],
    })
    sha = commit.data["sha"]
    if _create_ref(gh, repo, sha):
        return Attempt(True, mine, holder)
    current = read_holder(gh, repo)
    if current is not None:
        if current.live(now):
            if current.is_caller(holder, run):
                return Attempt(True, current, holder, already=True)
            return Attempt(False, current, holder)
        gh.rest("DELETE", "/repos/%s/git/refs/heads/%s" % (repo, LOCK_BRANCH),
                ok=(404, 422))
    if _create_ref(gh, repo, sha):
        return Attempt(True, mine, holder)
    current = read_holder(gh, repo)
    if current is not None and current.live(now) and current.is_caller(holder, run):
        return Attempt(True, current, holder, already=True)
    return Attempt(False, current or Holder("", None, None), holder)


def attempt_waiting(gh: GitHub, repo: str, holder: str, minutes: int = 15,
                    wait: int = 0, run: str = "",
                    clock: Optional[Callable[[], datetime]] = None,
                    sleep: Callable[[float], None] = time.sleep,
                    step: int = WAIT_STEP) -> Attempt:
    """`attempt`, tried again every `step` seconds for up to `wait` minutes
    while a peer holds the lock. Bounded: it gives up at the deadline and
    answers with the last holder it found."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    start = _utc(clock())
    deadline = start + timedelta(minutes=max(0, wait))
    while True:
        now = _utc(clock())
        result = attempt(gh, repo, holder, minutes, now, run)
        if result.acquired or now + timedelta(seconds=step) > deadline:
            waited = int((now - start).total_seconds() // 60)
            return Attempt(result.acquired, result.holder, holder, result.already, waited)
        sleep(step)


def acquire(gh: GitHub, repo: str, holder: str, minutes: int = 15,
            now: Optional[datetime] = None, run: str = "") -> Tuple[bool, str]:
    """(whether the caller holds the lock now, the holder's name)."""
    result = attempt(gh, repo, holder, minutes, now, run)
    return result.acquired, result.holder.name


def release(gh: GitHub, repo: str, holder: str) -> bool:
    _check_holder(holder)
    current = _read_lock(gh, repo)
    if current is None or current[0] != holder or current[1] is None or current[1] <= datetime.now(timezone.utc):
        return False
    # Keep the lock ref as the durable last-tick record. Expire it now so the
    # next holder can replace it immediately through acquire's stale-lock path.
    ref_path = "/repos/%s/git/ref/heads/%s" % (repo, LOCK_BRANCH)
    ref = gh.rest("GET", ref_path)
    sha = ref.data["object"]["sha"]
    previous = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, sha)).data
    message = re.sub(r"expires=\S+", "expires=" + datetime.now(timezone.utc).strftime(TIME_FORMAT),
                     previous.get("message", ""))
    commit = gh.rest("POST", "/repos/%s/git/commits" % repo, {
        "message": message, "tree": previous["tree"]["sha"], "parents": [sha]})
    # Updated at git/refs/<ref>: the git/ref/<ref> read path answers 404 to a PATCH.
    gh.rest("PATCH", "/repos/%s/git/refs/heads/%s" % (repo, LOCK_BRANCH),
            {"sha": commit.data["sha"], "force": True})
    return True
