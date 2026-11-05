"""The brain's tick lock: a branch whose commit message names the holder.

Creating a ref that exists fails with 422, and an expired lock is replaced
only by a fast-forward from the expired commit, so only one holder wins. The
message is `holder=<name> expires=<UTC> acquired=<UTC>`, with ` run=<id>` when
the caller named its run, and ` login=<login>` when it named the GitHub
login it writes as: a fleet brain acts as the brain only while the lock
names its own login (docs/BRAIN.md, Shared brain duty). Every attempt
answers with the holder record, so a
brain can tell its own lock from a peer's (loom#2275): a re-attempt that finds
the caller's own live lock is acquired, and a peer's lock is told with its
holder and how long it has held it. A brain that stands down for a peer's
lock records its yield under the lock line (`record_yield`, loom#2606).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .github import GitHub

LOCK_BRANCH = "loom-lock/brain"
HOLDER_RE = re.compile(r"[A-Za-z0-9._-]+")
# A GitHub login as LOOM_BRIEF_AUTHORS writes it: an App reads "<slug>[bot]".
LOGIN_RE = re.compile(r"[A-Za-z0-9._-]+(?:\[bot\])?")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Seconds between tries while `attempt_waiting` waits out a peer's lock.
WAIT_STEP = 60
# Seconds waited before each fresh read when a lock branch write is refused
# because the branch moved past the head the writer read (loom#2558).
WRITE_RETRY_WAITS = (2.0, 4.0, 8.0)


def _check_holder(holder: str) -> None:
    if not isinstance(holder, str) or not HOLDER_RE.fullmatch(holder):
        raise ValueError("holder may only use letters, digits, '.', '_' and '-'")


def _check_run(run: str) -> None:
    if not isinstance(run, str) or (run and not HOLDER_RE.fullmatch(run)):
        raise ValueError("run may only use letters, digits, '.', '_' and '-'")


def _check_login(login: str) -> None:
    if not isinstance(login, str) or (login and not LOGIN_RE.fullmatch(login)):
        raise ValueError("login is a GitHub login such as name or app-slug[bot]")


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
    # The GitHub login the holder took the lock as, or "" when it named none.
    login: str = ""

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
                  step=step if isinstance(step, str) and HOLDER_RE.fullmatch(step) else "",
                  login=_field(r"(?:^|\s)login=(\S+)", first) or "")


# The yield record (loom#2606): one line under the lock line, a JSON map of
# each brain that stood down for a peer's lock to {"at": <UTC>, "to": <the
# holder it yielded to>}, its newest yield only. It is a commit on the lock
# branch, never a comment, so it emails nobody; the station reads it to tell
# a brain that yielded from one that stopped. A takeover of an expired lock
# carries it into the new lock, and a yield older than YIELD_KEEP is dropped.
YIELD_MARKER = "loom:yield="
YIELD_KEEP = timedelta(days=1)


def yields_from(message: str) -> Dict[str, Dict[str, str]]:
    """The yield record in a lock commit message; malformed entries left out."""
    for line in message.splitlines():
        if not line.startswith(YIELD_MARKER):
            continue
        try:
            record = json.loads(line[len(YIELD_MARKER):])
        except ValueError:
            return {}
        if not isinstance(record, dict):
            return {}
        return {name: {"at": entry["at"], "to": entry["to"]}
                for name, entry in record.items() if _good_yield(name, entry)}
    return {}


def _good_yield(name: Any, entry: Any) -> bool:
    return (isinstance(name, str) and bool(HOLDER_RE.fullmatch(name))
            and isinstance(entry, dict) and isinstance(entry.get("to"), str)
            and bool(HOLDER_RE.fullmatch(entry["to"]))
            and isinstance(entry.get("at"), str) and _parse_time(entry["at"]) is not None)


def _with_yields(message: str, yields: Dict[str, Dict[str, str]], now: datetime) -> str:
    """`message` with its yield line replaced by `yields`, those older than
    YIELD_KEEP at `now` dropped; no line when none is left."""
    keep = {name: entry for name, entry in sorted(yields.items())
            if now - (_parse_time(entry["at"]) or now) <= YIELD_KEEP}
    lines = [line for line in message.splitlines() if not line.startswith(YIELD_MARKER)]
    if keep:
        lines.append(YIELD_MARKER + json.dumps(keep, separators=(",", ":")))
    return "\n".join(lines)


def _read_message(gh: GitHub, repo: str) -> Optional[Tuple[str, str]]:
    """(head sha, commit message) of the current lock, or None when absent."""
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, LOCK_BRANCH),
                  ok=(404,))
    if ref.status == 404:
        return None
    sha = ref.data["object"]["sha"]
    commit = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, sha), ok=(404,))
    if commit.status == 404:
        return None
    return sha, str(commit.data.get("message") or "")


def read_holder(gh: GitHub, repo: str) -> Optional[Holder]:
    """The current lock's holder record, or None when there is no lock."""
    read = _read_message(gh, repo)
    return None if read is None else _holder_from(read[1])


def read_yields(gh: GitHub, repo: str) -> Dict[str, Dict[str, str]]:
    """The yield record on the current lock: {} when there is no lock or none."""
    read = _read_message(gh, repo)
    return {} if read is None else yields_from(read[1])


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


def _lock_commit(gh: GitHub, repo: str, message: str, parents: List[str]) -> str:
    """POST a lock commit on main's tree and answer its sha. Called only once
    the caller is about to write the ref, so a waiting brain leaves no orphan."""
    base = gh.rest("GET", "/repos/%s/git/ref/heads/main" % repo).data["object"]["sha"]
    tree = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, base)).data["tree"]["sha"]
    return gh.rest("POST", "/repos/%s/git/commits" % repo, {
        "message": message, "tree": tree, "parents": parents}).data["sha"]


def _fast_forward(gh: GitHub, repo: str, sha: str) -> bool:
    """Move the lock ref to `sha` without force: False when GitHub refuses
    because `sha` does not descend from the branch head (the branch moved)."""
    resp = gh.rest("PATCH", "/repos/%s/git/refs/heads/%s" % (repo, LOCK_BRANCH),
                   {"sha": sha, "force": False}, ok=(409, 422))
    return resp.status not in (409, 422)


def _no_holder() -> Holder:
    return Holder("", None, None)


def attempt(gh: GitHub, repo: str, holder: str, minutes: int = 15,
            now: Optional[datetime] = None, run: str = "", login: str = "",
            sleep: Callable[[float], None] = time.sleep) -> Attempt:
    """Try once to take the lock. A live lock that is the caller's own (same
    holder, and same run when `run` is given) is acquired, not held: a
    re-attempt after a lost or misread answer must not stand the brain down
    against itself.

    The lock is read first, and a live lock is answered with no write. A
    free lock is created; an expired one is taken over by compare-and-swap
    (loom#2579): the new lock commit's parent is the expired lock and the ref
    moves only by fast-forward, so of two takers that read the same expired
    lock GitHub lets exactly one through."""
    _check_holder(holder)
    _check_run(run)
    _check_login(login)
    now = _utc(now)
    expires = now + timedelta(minutes=minutes)
    message = "holder=%s expires=%s acquired=%s" % (
        holder, expires.strftime(TIME_FORMAT), now.strftime(TIME_FORMAT))
    if run:
        message += " run=%s" % run
    if login:
        message += " login=%s" % login
    mine = Holder(holder, expires, now, run, login=login)
    read = _read_message(gh, repo)
    if read is None:
        if _create_ref(gh, repo, _lock_commit(gh, repo, message, [])):
            return Attempt(True, mine, holder)
        # Another taker created the lock between the read and the create.
        read = _read_message(gh, repo)
        if read is None:
            return Attempt(False, _no_holder(), holder)
    current = _holder_from(read[1])
    if current.live(now):
        if current.is_caller(holder, run):
            return Attempt(True, current, holder, already=True)
        return Attempt(False, current, holder)
    return _take_over(gh, repo, holder, message, mine, read, now, sleep)


def _take_over(gh: GitHub, repo: str, holder: str, message: str, mine: Holder,
               read: Tuple[str, str], now: datetime,
               sleep: Callable[[float], None]) -> Attempt:
    """Replace the expired lock `read` by fast-forward. A refusal whose fresh
    read finds a live lock is a lost race: whoever it names, even the
    caller's own holder name, the caller never held it. A refusal whose
    fresh read still finds the lock expired (a lagging read, loom#2555)
    tries again from that read, after each of WRITE_RETRY_WAITS."""
    waits = list(WRITE_RETRY_WAITS)
    while True:
        stale, stale_message = read
        # The expired lock's yield record goes on into the new lock.
        carried = _with_yields(message, yields_from(stale_message), now)
        if _fast_forward(gh, repo, _lock_commit(gh, repo, carried, [stale])):
            return Attempt(True, mine, holder)
        fresh = _read_message(gh, repo)
        retry = fresh is not None and not _holder_from(fresh[1]).live(now) and bool(waits)
        if retry:
            sleep(waits.pop(0))
            fresh = _read_message(gh, repo)
        current = _no_holder() if fresh is None else _holder_from(fresh[1])
        if not retry or fresh is None or current.live(now):
            return Attempt(False, current, holder)
        read = fresh


def attempt_waiting(gh: GitHub, repo: str, holder: str, minutes: int = 15,
                    wait: int = 0, run: str = "", login: str = "",
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
        result = attempt(gh, repo, holder, minutes, now, run, login)
        if result.acquired or now + timedelta(seconds=step) > deadline:
            waited = int((now - start).total_seconds() // 60)
            return Attempt(result.acquired, result.holder, holder, result.already, waited)
        sleep(step)


def record_yield(gh: GitHub, repo: str, holder: str, now: Optional[datetime] = None,
                 sleep: Callable[[float], None] = time.sleep) -> Optional[Dict[str, str]]:
    """Record on the lock that `holder` stood down for the brain the lock
    names, at `now`: its yield entry, or None when no other named brain's
    lock is there to yield to. Written by compare-and-swap (write_branch), so
    the holder's own step commits are never dropped."""
    _check_holder(holder)
    now = _utc(now)

    def build() -> Tuple[Optional[str], Optional[str], Optional[Dict[str, str]]]:
        read = _read_message(gh, repo)
        if read is None:
            return None, None, None
        sha, message = read
        to = _holder_from(message).name
        if not to or to == holder:
            return sha, None, None
        entry = {"at": now.strftime(TIME_FORMAT), "to": to}
        yields = dict(yields_from(message), **{holder: entry})
        tree = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, sha)).data["tree"]["sha"]
        commit = gh.rest("POST", "/repos/%s/git/commits" % repo, {
            "message": _with_yields(message, yields, now), "tree": tree, "parents": [sha]})
        return sha, commit.data["sha"], entry

    return write_branch(gh, repo, build, sleep)


class LockHeadMoved(RuntimeError):
    """A lock branch write still refused after every fresh read: the branch
    is at a head the writer could not read. It names both heads, so a step
    never vanishes in silence (loom#2558)."""

    def __init__(self, repo: str, read: str, current: Optional[str]):
        self.repo = repo
        self.read = read
        self.current = current
        if current is None:
            where = "the branch is gone"
        elif current == read:
            where = "GitHub still serves %s as the head, but refuses a commit on it" % read
        else:
            where = "the branch is at %s" % current
        super().__init__(
            "the %s lock branch in %s moved past the head this write read (%s); %s. "
            "Nothing was written: a lock branch write only fast-forwards from the head "
            "it read (docs/BRAIN.md, The tick)" % (LOCK_BRANCH, repo, read, where))


def _head(gh: GitHub, repo: str) -> Optional[str]:
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, LOCK_BRANCH), ok=(404,))
    return None if ref.status == 404 else ref.data["object"]["sha"]


def write_branch(gh: GitHub, repo: str, build: Callable[[], Tuple[Optional[str], Optional[str], Any]],
                 sleep: Callable[[float], None] = time.sleep) -> Any:
    """Move the lock branch by compare-and-swap. `build` reads the branch and
    answers (the head it read, the new commit whose parent is that head, a
    result), or a None commit when there is nothing to write. The ref update
    is never forced: GitHub refuses with 422 a commit that does not descend
    from the branch head, so a write built on an old read (GitHub can serve a
    head two step commits old, loom#2555) never drops the newer commits. A
    refusal waits, then builds again from a fresh read, up to
    len(WRITE_RETRY_WAITS) times; then LockHeadMoved names both heads."""
    waits = list(WRITE_RETRY_WAITS)
    while True:
        read, new, result = build()
        if new is None:
            return result
        # Updated at git/refs/<ref>: the git/ref/<ref> read path answers 404 to a PATCH.
        resp = gh.rest("PATCH", "/repos/%s/git/refs/heads/%s" % (repo, LOCK_BRANCH),
                       {"sha": new, "force": False}, ok=(409, 422))
        if resp.status not in (409, 422):
            return result
        if not waits:
            raise LockHeadMoved(repo, str(read), _head(gh, repo))
        sleep(waits.pop(0))


def release(gh: GitHub, repo: str, holder: str,
            sleep: Callable[[float], None] = time.sleep) -> bool:
    _check_holder(holder)

    def build() -> Tuple[Optional[str], Optional[str], bool]:
        current = _read_lock(gh, repo)
        if (current is None or current[0] != holder or current[1] is None
                or current[1] <= datetime.now(timezone.utc)):
            return None, None, False
        # Keep the lock ref as the durable last-tick record. Expire it now so the
        # next holder can replace it immediately through attempt's stale-lock path.
        sha = _head(gh, repo)
        if sha is None:
            return None, None, False
        previous = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, sha)).data
        message = re.sub(r"expires=\S+", "expires=" + datetime.now(timezone.utc).strftime(TIME_FORMAT),
                         previous.get("message", ""))
        commit = gh.rest("POST", "/repos/%s/git/commits" % repo, {
            "message": message, "tree": previous["tree"]["sha"], "parents": [sha]})
        return sha, commit.data["sha"], True

    return write_branch(gh, repo, build, sleep)
