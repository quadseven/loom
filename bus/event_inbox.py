"""The station as the consumer of the fleet event inbox (LOOM_INBOX=1).

The fleet keeps one durable list of events per consumer in a Postgres table
(`inbox_event`), with a lease row per inbox (`inbox_lease`) whose epoch fences
every status write. A webhook receiver and a scheduler append to it and send
`NOTIFY fleet_inbox, '<inbox>'`. This module is the station's side of that
table: it reads and updates the station's own inbox and nothing else.

Events are hints. The pass stays the truth: the station never acts on an
event's payload, only on what its own GitHub reads say. An event only moves
its repo to the front of the pass, and makes an idle repo due now.

What it does when LOOM_INBOX=1:

  wait(env)      between passes (`python3 -m bus station-wait`): LISTEN on
                 fleet_inbox for up to LOOM_STATION_INTERVAL seconds. A
                 NOTIFY for this inbox ends the wait early, but no earlier
                 than LOOM_INBOX_DEBOUNCE seconds (default 15) after the
                 first pending event, so a burst of events becomes one pass.
  Consumer.begin at the start of a pass, after the station lease is taken:
                 take the inbox lease under the station lease's holder, and
                 claim every due event. Their subjects are the pass's
                 priority list.
  Consumer.finish at the end of a pass that ran: mark the claimed events
                 `done` with the pass id as `run_ref`.
  Consumer.close always: give the inbox lease up. Events a pass claimed and
                 did not finish become claimable by the next pass at once.

The two leases. The inbox lease is never held without the station lease:
begin() takes it only after checking the station lease, under the same
holder name and TTL, and finish() checks the station lease again before it
writes. A station that loses its lease mid-pass writes no `done`, and close()
gives the inbox lease up. A station that dies mid-pass leaves the inbox lease
to expire with its TTL; the next holder's claim then takes the events back.

With LOOM_INBOX unset or anything but 1, nothing here opens a connection,
imports a driver or changes a pass, and `station-wait` sleeps exactly
LOOM_STATION_INTERVAL seconds. An inbox failure never stops a pass: it is
logged and the pass runs as it would with the inbox off.

Settings: LOOM_INBOX (1 turns it on), LOOM_INBOX_DB_HOST, LOOM_INBOX_DB_NAME,
LOOM_INBOX_DB_USER, LOOM_INBOX_DB_PASSWORD (the fleet database, its own role,
not LOOM_DB_*), LOOM_INBOX_INSTANCE (the instance the database checks every
write against), LOOM_INBOX_NAME (default loom-station), LOOM_INBOX_DEBOUNCE
(seconds, default 15). The inbox needs LOOM_STATION_LEASE=1.
"""
from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

DEFAULT_INBOX = "loom-station"
CHANNEL = "fleet_inbox"
DEFAULT_DEBOUNCE = 15
DEFAULT_INTERVAL = 60
CLAIM_LIMIT = 100
MAX_ATTEMPTS = 5
POLL_SECONDS = 1.0
CONNECT_TIMEOUT = 5
SETTINGS = ("LOOM_INBOX_DB_HOST", "LOOM_INBOX_DB_NAME", "LOOM_INBOX_DB_USER",
            "LOOM_INBOX_DB_PASSWORD", "LOOM_INBOX_INSTANCE")
INBOX_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# A subject names a repo, an issue or pull request (`owner/name#N`) or a ref
# (`owner/name@ref`). Only the repo part is used, and only for a watched repo.
SUBJECT_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:[#@].*)?")

# Every statement keeps to the station's own inbox and instance. The lease
# statements follow the fleet library's: a takeover raises the epoch. Unlike a
# peer's live lease, this holder's own lease is retaken at once, as the
# station lease is: the station lease and the pass lock already keep two
# passes of one holder apart, and a pass that died must not keep the next
# one from its events for a whole TTL.
STATEMENT_TIMEOUT = "SET statement_timeout = '5s'"
ACQUIRE = ("INSERT INTO inbox_lease (inbox, instance, holder, epoch, expires_at) "
           "VALUES (%s, %s, %s, 1, now() + make_interval(secs => %s)) "
           "ON CONFLICT (inbox) DO UPDATE SET holder = EXCLUDED.holder, "
           "epoch = inbox_lease.epoch + 1, expires_at = EXCLUDED.expires_at "
           "WHERE inbox_lease.expires_at < now() OR inbox_lease.holder = EXCLUDED.holder "
           "RETURNING epoch")
RENEW = ("UPDATE inbox_lease SET expires_at = now() + make_interval(secs => %s) "
         "WHERE inbox = %s AND holder = %s AND epoch = %s")
RELEASE = ("UPDATE inbox_lease SET expires_at = now() - interval '1 millisecond' "
           "WHERE inbox = %s AND holder = %s AND epoch = %s")
_HELD = ("EXISTS (SELECT 1 FROM inbox_lease WHERE inbox = %s AND holder = %s "
         "AND epoch = %s AND expires_at > now())")
# An event a lost consumer held that already used every attempt is dead, so
# an event that kills its pass each time does not loop forever.
BURY = ("UPDATE inbox_event SET status = 'dead', done_at = now(), "
        "result = 'dead: abandoned by a consumer that lost its lease, attempt ' "
        "|| attempts || ' of ' || %s "
        "WHERE instance = %s AND inbox = %s AND status = 'claimed' "
        "AND claim_epoch < %s AND attempts >= %s AND " + _HELD)
CLAIM = ("WITH picked AS (SELECT id FROM inbox_event "
         "WHERE instance = %s AND inbox = %s AND not_before <= now() "
         "AND (status = 'pending' OR (status = 'claimed' AND claim_epoch < %s)) "
         "AND " + _HELD + " ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED) "
         "UPDATE inbox_event e SET status = 'claimed', claimed_by = %s, "
         "claim_epoch = %s, attempts = e.attempts + 1 "
         "FROM picked WHERE e.id = picked.id RETURNING e.id, e.kind, e.subject")
COMPLETE = ("UPDATE inbox_event SET status = 'done', result = %s, run_ref = %s, "
            "done_at = now() WHERE id = ANY(CAST(%s AS bigint[])) "
            "AND instance = %s AND inbox = %s AND status = 'claimed' AND claim_epoch = %s "
            "AND (SELECT epoch FROM inbox_lease WHERE inbox = %s) = %s")
# Seconds since the oldest due pending event arrived, by the database's clock;
# NULL when there is none.
PENDING_AGE = ("SELECT extract(epoch from now() - min(received_at)) FROM inbox_event "
               "WHERE instance = %s AND inbox = %s AND status = 'pending' "
               "AND not_before <= now()")
LISTEN = "LISTEN " + CHANNEL
PUMP = "SELECT 1"

Say = Callable[[str], None]


class InboxError(Exception):
    """The inbox could not be used for this pass; the pass runs without it."""


@dataclass(frozen=True)
class Config:
    instance: str
    inbox: str
    debounce: float
    connect: Callable[[], Any]


@dataclass(frozen=True)
class Claimed:
    id: int
    kind: str
    subject: Optional[str]


def enabled(env: Mapping[str, str]) -> bool:
    return (env.get("LOOM_INBOX") or "0").strip() == "1"


def interval(env: Mapping[str, str]) -> int:
    """The pass interval, as the installer reads it: whole seconds, at least
    60; anything else is the default."""
    raw = (env.get("LOOM_STATION_INTERVAL") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) >= DEFAULT_INTERVAL else DEFAULT_INTERVAL


def config(env: Mapping[str, str], dbapi: Any = None) -> Config:
    """The inbox settings, or a ValueError naming what is wrong."""
    missing = [key for key in SETTINGS if not (env.get(key) or "").strip()]
    if missing:
        raise ValueError("LOOM_INBOX=1 needs " + ", ".join(missing))
    inbox = (env.get("LOOM_INBOX_NAME") or DEFAULT_INBOX).strip()
    if not INBOX_RE.fullmatch(inbox):
        raise ValueError("LOOM_INBOX_NAME may only use lowercase letters, digits, '.', '_' and '-'")
    raw = (env.get("LOOM_INBOX_DEBOUNCE") or str(DEFAULT_DEBOUNCE)).strip()
    if not raw.isdigit():
        raise ValueError("LOOM_INBOX_DEBOUNCE is a whole number of seconds")
    if dbapi is None:
        from . import state_postgres
        dbapi = state_postgres.driver()
        if dbapi is None:
            raise ValueError("LOOM_INBOX=1 needs the pg8000 package")
    settings = {key: env[key].strip() for key in SETTINGS[:4]}

    def connect() -> Any:
        conn = dbapi.connect(host=settings["LOOM_INBOX_DB_HOST"],
                             database=settings["LOOM_INBOX_DB_NAME"],
                             user=settings["LOOM_INBOX_DB_USER"],
                             password=settings["LOOM_INBOX_DB_PASSWORD"],
                             timeout=CONNECT_TIMEOUT)
        try:
            conn.cursor().execute(STATEMENT_TIMEOUT)
            conn.commit()
        except BaseException:
            conn.close()
            raise
        return conn

    return Config(env["LOOM_INBOX_INSTANCE"].strip(), inbox, float(raw), connect)


def priority_repos(subjects: Sequence[Optional[str]], watched: Sequence[str]) -> List[str]:
    """The watched repos the subjects name, in the order first named. A
    subject that names no watched repo is dropped: the payload is untrusted,
    so an event can only reorder repos the station already reads."""
    known = set(watched)
    out: List[str] = []
    for subject in subjects:
        m = SUBJECT_RE.fullmatch((subject or "").strip())
        if m and m.group(1) in known and m.group(1) not in out:
            out.append(m.group(1))
    return out


class Consumer:
    """One pass's hold on the inbox. Every method logs a failure and returns;
    none raises into the pass."""

    def __init__(self, cfg: Config, lease: Any, say: Say) -> None:
        self.cfg = cfg
        self.lease = lease
        self.say = say
        self.conn: Any = None
        self.epoch: Optional[int] = None
        self.claimed: List[Claimed] = []

    def _run(self, sql: str, params: Tuple[Any, ...]) -> Tuple[List[Any], int]:
        cur = self.conn.cursor()
        cur.execute(sql, params)
        rows = list(cur.fetchall()) if cur.description else []
        return rows, cur.rowcount

    def _station_lease_held(self) -> bool:
        from . import station_lease
        try:
            self.lease.check()
        except (station_lease.LeaseLost, station_lease.state_store.StateError) as err:
            self.say("inbox: the station lease is not held (%s); no events this pass" % err)
            return False
        return True

    def begin(self) -> List[Claimed]:
        """Take the inbox lease and claim the due events. [] when the station
        lease is not held, the inbox lease is a peer's, or the database fails."""
        if not self._station_lease_held():
            return []
        try:
            self.conn = self.cfg.connect()
            rows, _ = self._run(ACQUIRE, (self.cfg.inbox, self.cfg.instance, self.lease.holder,
                                          float(self.lease.ttl)))
            if not rows:
                self.conn.commit()
                self.say("inbox: another consumer holds the %s lease; no events this pass"
                         % self.cfg.inbox)
                return []
            self.epoch = int(rows[0][0])
            self.conn.commit()
            held = (self.cfg.inbox, self.lease.holder, self.epoch)
            self._run(BURY, (MAX_ATTEMPTS, self.cfg.instance, self.cfg.inbox, self.epoch,
                             MAX_ATTEMPTS) + held)
            rows, _ = self._run(CLAIM, (self.cfg.instance, self.cfg.inbox, self.epoch) + held
                                + (CLAIM_LIMIT, self.lease.holder, self.epoch))
            self.conn.commit()
        except Exception as err:  # noqa: BLE001 - the inbox is a hint; the pass goes on without it
            self._rollback()
            self.say("inbox: could not claim events: %s: %s" % (type(err).__name__, err))
            return []
        self.claimed = sorted((Claimed(int(r[0]), str(r[1]), r[2]) for r in rows),
                              key=lambda c: c.id)
        if self.claimed:
            self.say("inbox: claimed %d event(s) at epoch %d" % (len(self.claimed), self.epoch))
        return list(self.claimed)

    def finish(self, run_ref: str) -> int:
        """Mark the claimed events done, while both leases are still this
        station's. How many rows changed: 0 when fenced out."""
        if self.epoch is None or not self.claimed:
            return 0
        if not self._station_lease_held():
            return 0
        ids = [c.id for c in self.claimed]
        try:
            self._run(RENEW, (float(self.lease.ttl), self.cfg.inbox, self.lease.holder, self.epoch))
            _, count = self._run(COMPLETE, ("done: station pass", run_ref, ids, self.cfg.instance,
                                            self.cfg.inbox, self.epoch, self.cfg.inbox, self.epoch))
            self.conn.commit()
        except Exception as err:  # noqa: BLE001 - unfinished events are claimed again next pass
            self._rollback()
            self.say("inbox: could not mark events done: %s: %s" % (type(err).__name__, err))
            return 0
        if count != len(ids):
            self.say("inbox: marked %d of %d event(s) done; the rest were fenced out"
                     % (count, len(ids)))
        return count

    def close(self) -> None:
        """Give the inbox lease up and close the connection."""
        if self.conn is None:
            return
        try:
            if self.epoch is not None:
                self._run(RELEASE, (self.cfg.inbox, self.lease.holder, self.epoch))
                self.conn.commit()
        except Exception as err:  # noqa: BLE001 - the lease then expires with its TTL
            self.say("inbox: could not give the lease up: %s: %s" % (type(err).__name__, err))
        finally:
            with contextlib.suppress(Exception):
                self.conn.close()
            self.conn = None

    def _rollback(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self.conn.rollback()


def select(env: Mapping[str, str], lease: Any, say: Say, dbapi: Any = None) -> Optional[Consumer]:
    """The consumer LOOM_INBOX asks for, or None when it is off (the default),
    when there is no station lease to tie it to, or when a setting is wrong."""
    if not enabled(env):
        return None
    if lease is None:
        say("inbox: LOOM_INBOX=1 needs LOOM_STATION_LEASE=1; the inbox stays off")
        return None
    try:
        cfg = config(env, dbapi)
    except ValueError as err:
        say("inbox: %s; the inbox stays off" % err)
        return None
    return Consumer(cfg, lease, say)


def run_ref(lease: Any, at: float) -> str:
    """The pass id recorded on the events a pass finished."""
    return "loom-pass:%s:%d:%d" % (lease.holder, lease.epoch or 0, int(at))


# -- between passes --------------------------------------------------------

class Debounce:
    """When the wait ends: at `start + interval` at the latest, or `debounce`
    seconds after the first event seen, whichever is first. Times are one
    monotonic clock."""

    def __init__(self, start: float, interval_s: float, debounce_s: float) -> None:
        self.deadline = start + interval_s
        self.debounce = debounce_s
        self.first: Optional[float] = None

    def seen(self, at: float) -> None:
        self.first = at if self.first is None else min(self.first, at)

    def wake_at(self) -> float:
        if self.first is None:
            return self.deadline
        return min(self.deadline, self.first + self.debounce)


def _notified(conn: Any, inbox: str) -> bool:
    """Drain the connection's notifications; True when one was for `inbox`."""
    found = False
    queue = getattr(conn, "notifications", None)
    while queue:
        note = queue.popleft()
        channel, payload = (note[1], note[2]) if len(note) >= 3 else (CHANNEL, "")
        if channel == CHANNEL and payload == inbox:
            found = True
    return found


def _listen(cfg: Config, clock: Callable[[], float], sleep: Callable[[float], None],
            timer: Debounce) -> str:
    conn = cfg.connect()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(LISTEN)
        # An event that arrived during the last pass sent its NOTIFY before
        # this LISTEN: its age, by the database's clock, starts the debounce.
        cur.execute(PENDING_AGE, (cfg.instance, cfg.inbox))
        row = cur.fetchone()
        if row and row[0] is not None:
            timer.seen(clock() - max(float(row[0]), 0.0))
        while True:
            now = clock()
            if now >= timer.wake_at():
                return "event" if timer.first is not None else "interval"
            sleep(min(POLL_SECONDS, timer.wake_at() - now))
            # Any statement makes the driver read what the server sent.
            cur.execute(PUMP)
            cur.fetchall()
            if _notified(conn, cfg.inbox):
                timer.seen(clock())
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def wait(env: Mapping[str, str], say: Say, clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep, dbapi: Any = None) -> str:
    """Wait for the next pass. Returns why it ended: "interval", "event", or
    "fallback" when the inbox could not be read and the wait slept out the
    interval instead. With the inbox off it is a plain sleep."""
    start = clock()
    total = float(interval(env))
    if not enabled(env):
        sleep(total)
        return "interval"
    timer = Debounce(start, total, DEFAULT_DEBOUNCE)
    try:
        cfg = config(env, dbapi)
        timer.debounce = cfg.debounce
        return _listen(cfg, clock, sleep, timer)
    except Exception as err:  # noqa: BLE001 - a broken inbox must never shorten the wait
        say("inbox: could not listen (%s: %s); waiting out the interval" % (type(err).__name__, err))
        left = timer.deadline - clock()
        if left > 0:
            sleep(left)
        return "fallback"
