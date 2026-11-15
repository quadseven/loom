"""A lease so only one station runs its pass at a time (loom#3050).

The station used to hold a file lock on one host, so a second station could
never run safely and a sleeping host stopped everything. A lease is held by a
named station for a time (the TTL) and renewed while it works. A station that
misses its renewals loses the lease to a peer, and each takeover raises the
epoch. The epoch is the fencing token: a station that was asleep and wakes
holding an old epoch fails its renewal and its state writes (LeaseLost), so it
cannot overwrite what the new holder owns.

The lease sits in one cell that supports a compare-and-set on the epoch. The
file cell here keeps it in a JSON file under a short lock; a database cell
(a row updated `WHERE epoch = <the epoch read>`) can stand in for it without
this module changing. The lease is OFF unless LOOM_STATION_LEASE=1.

Settings: LOOM_STATION_LEASE (1 turns it on), LOOM_STATION_ID (this station's
stable name; default the host name), LOOM_STATION_LEASE_TTL (seconds a lease
lives without a renewal; default 180, three passes at the usual one-minute
interval).

What is not fenced: a GitHub write already in flight when the lease is lost.
Every state write checks the epoch first, and the ledgers make a repeated
write a no-op, so the exposure is one step of a pass, not the pass.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import state_store

DEFAULT_TTL = 180
STATION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class LeaseLost(state_store.StateError):
    """This station no longer holds the lease it took: a peer took it over
    after a missed heartbeat. A StateError, so every
    place the station already stops a pass on one stops it on this too."""


@dataclass(frozen=True)
class Record:
    holder: str
    epoch: int
    expires: float


class FileCell:
    """The lease record in a JSON file. Every read and swap holds an
    exclusive lock on a guard file beside it, so two stations on one host
    cannot both win; a cell on a shared database does the same with one
    conditional UPDATE."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    def now(self) -> float:
        return self.clock()

    def _locked_read(self) -> Optional[Record]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return Record(str(data["holder"]), int(data["epoch"]), float(data["expires"]))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as err:
            raise state_store.StateError("unreadable lease file %s: %s" % (self.path, err)) from None

    def _guard(self) -> int:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path + ".guard", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def read(self) -> Optional[Record]:
        fd = self._guard()
        try:
            return self._locked_read()
        finally:
            os.close(fd)

    def swap(self, expected_epoch: Optional[int], record: Record) -> bool:
        """Store `record` only if the stored epoch is `expected_epoch` (None:
        nothing stored). True when it was stored."""
        fd = self._guard()
        try:
            current = self._locked_read()
            if (current.epoch if current else None) != expected_epoch:
                return False
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"holder": record.holder, "epoch": record.epoch,
                           "expires": record.expires}, fh)
            os.replace(tmp, self.path)
            return True
        finally:
            os.close(fd)


class StationLease:
    def __init__(self, cell: Any, holder: str, ttl: float = DEFAULT_TTL) -> None:
        self.cell = cell
        self.holder = holder
        self.ttl = ttl
        self.epoch: Optional[int] = None

    def acquire(self) -> bool:
        """Take the lease when it is free, expired, or this station's own
        from an earlier pass. True when this station now holds it, at a
        higher epoch than any holder before it."""
        record = self.cell.read()
        now = self.cell.now()
        if record is not None and record.holder != self.holder and record.expires > now:
            return False
        epoch = record.epoch + 1 if record is not None else 1
        taken = self.cell.swap(record.epoch if record is not None else None,
                               Record(self.holder, epoch, now + self.ttl))
        if taken:
            self.epoch = epoch
        return taken

    def _mine(self) -> Record:
        if self.epoch is None:
            raise LeaseLost("this station never took the lease")
        record = self.cell.read()
        if record is None or record.epoch != self.epoch or record.holder != self.holder:
            raise LeaseLost("lease %s at epoch %s was taken by %s at epoch %s" % (
                self.holder, self.epoch, record.holder if record else "nobody",
                record.epoch if record else "none"))
        return record

    def check(self) -> None:
        """Raise LeaseLost unless this station still holds its epoch."""
        self._mine()

    def renew(self) -> None:
        """Move the expiry out by the TTL; LeaseLost when a peer took over."""
        self._mine()
        now = self.cell.now()
        if not self.cell.swap(self.epoch, Record(self.holder, self.epoch, now + self.ttl)):
            raise LeaseLost("lease at epoch %s changed hands during a renewal" % self.epoch)


class FencedStore(state_store.StateStore):
    """A store whose every write first proves the lease is still this
    station's, and renews it once a third of the TTL has passed. Reads are
    not fenced: a stale read harms nothing until a write."""

    def __init__(self, inner: state_store.StateStore, lease: StationLease,
                 clock: Callable[[], float] = time.time) -> None:
        self.inner = inner
        self.lease = lease
        self.clock = clock
        self._renewed = clock()

    def read_json(self, path: str, default: Any, strict: bool = True) -> Any:
        return self.inner.read_json(path, default, strict)

    def write_json(self, path: str, data: Any) -> None:
        now = self.clock()
        if now - self._renewed >= self.lease.ttl / 3:
            self.lease.renew()
            self._renewed = now
        else:
            self.lease.check()
        self.inner.write_json(path, data)


def select(env: Mapping[str, str], home: str, default_id: Optional[str] = None
           ) -> Optional[StationLease]:
    """The lease `env` asks for, or None when it is off (the default)."""
    if (env.get("LOOM_STATION_LEASE") or "0").strip() != "1":
        return None
    holder = (env.get("LOOM_STATION_ID") or default_id or socket.gethostname()).strip()
    if not STATION_ID_RE.fullmatch(holder):
        raise ValueError("LOOM_STATION_ID may only use letters, digits, '.', '_' and '-'")
    raw = (env.get("LOOM_STATION_LEASE_TTL") or str(DEFAULT_TTL)).strip()
    if not raw.isdigit() or int(raw) < 3:
        raise ValueError("LOOM_STATION_LEASE_TTL is a whole number of seconds, at least 3")
    cell = FileCell(os.path.join(home, "state", "station.lease.json"))
    return StationLease(cell, holder, int(raw))
