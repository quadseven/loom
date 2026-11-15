"""The station's shared backend: its ledgers and its lease in Postgres (loom#3050).

It reaches the database the way the dashboard does (dashboard/transcripts.py):
the LOOM_DB_HOST, LOOM_DB_NAME, LOOM_DB_USER and LOOM_DB_PASSWORD settings, the
pg8000 driver, a 5 second connect and statement timeout, and tables made on
first use with CREATE TABLE IF NOT EXISTS, so a deploy needs no migration step.
It is OFF unless LOOM_STATE_BACKEND=postgres; the default stays files.

Two tables:

  loom_state     (key text primary key, value jsonb, updated_at double precision)
                 one row per ledger; the key is the ledger's path below the
                 loom home's state directory, with forward slashes.
  station_lease  (name text primary key, holder text, epoch bigint,
                 expires double precision)
                 the one row of the station lease (bus/station_lease.py).

The lease uses the database's own clock, so two stations whose host clocks
differ still agree on when a lease has expired. A ledger write made under a
lease is one statement that writes only while the lease row still holds this
station's epoch, so a station that was asleep cannot write over the one that
took over, even if it checked the lease a moment before.

The database is a single point of failure of its own: with it down, a pass
stops with a state error and writes nothing, as it does for an unreadable file.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import time
from typing import Any, Mapping, Optional, Tuple

from dashboard import transcripts

from . import state_store

SETTINGS = ("LOOM_DB_HOST", "LOOM_DB_NAME", "LOOM_DB_USER", "LOOM_DB_PASSWORD")

CREATE_STATE = ("CREATE TABLE IF NOT EXISTS loom_state (key text primary key, "
                "value jsonb not null, updated_at double precision not null)")
CREATE_LEASE = ("CREATE TABLE IF NOT EXISTS station_lease (name text primary key, "
                "holder text not null, epoch bigint not null, expires double precision not null)")

GET_STATE = "SELECT value FROM loom_state WHERE key = %s"
PUT_STATE = ("INSERT INTO loom_state (key, value, updated_at) VALUES (%s, %s::jsonb, %s) "
             "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at")
# One statement: it writes only while the lease row holds this station's epoch.
PUT_STATE_FENCED = ("INSERT INTO loom_state (key, value, updated_at) "
                    "SELECT %s::text, %s::jsonb, %s::double precision "
                    "WHERE (SELECT epoch FROM station_lease WHERE name = 'station') = %s::bigint "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                    "updated_at = EXCLUDED.updated_at")
GET_LEASE = "SELECT holder, epoch, expires FROM station_lease WHERE name = 'station'"
INSERT_LEASE = ("INSERT INTO station_lease (name, holder, epoch, expires) "
                "VALUES ('station', %s, %s, %s) ON CONFLICT (name) DO NOTHING")
UPDATE_LEASE = ("UPDATE station_lease SET holder = %s, epoch = %s, expires = %s "
                "WHERE name = 'station' AND epoch = %s")
DB_NOW = "SELECT extract(epoch from now())"


def driver() -> Any:
    """The pg8000 DB-API module, or None when it is not installed."""
    if importlib.util.find_spec("pg8000") is None:
        return None
    import pg8000.dbapi
    return pg8000.dbapi


def check_settings(env: Mapping[str, str]) -> Any:
    """The driver, when the database settings are all there and pg8000 is
    installed; otherwise a ValueError saying what is missing, so a station
    asked for the shared backend never quietly falls back to files."""
    missing = [key for key in SETTINGS if not env.get(key)]
    if missing:
        raise ValueError("LOOM_STATE_BACKEND=postgres needs " + ", ".join(missing))
    module = driver()
    if module is None:
        raise ValueError("LOOM_STATE_BACKEND=postgres needs the pg8000 package")
    return module


class _Database:
    """One lazily opened connection, with the tables made on first use. After
    any failure it is dropped, and the next call opens a fresh one."""

    def __init__(self, dbapi: Any) -> None:
        self.dbapi = dbapi
        self._conn: Any = None

    def errors(self) -> Tuple[type, ...]:
        return (self.dbapi.Error, OSError)

    def run(self, work: Any) -> Any:
        try:
            if self._conn is None:
                conn = transcripts.connect(self.dbapi)
                try:
                    cur = conn.cursor()
                    cur.execute(CREATE_STATE)
                    cur.execute(CREATE_LEASE)
                    conn.commit()
                except BaseException:
                    conn.close()
                    raise
                self._conn = conn
            cur = self._conn.cursor()
            result = work(cur)
            self._conn.commit()
            return result
        except BaseException:
            if self._conn is not None:
                # The connection is already bad; a failed close adds nothing.
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None
            raise


class PostgresStore(state_store.StateStore):
    def __init__(self, home: str, dbapi: Any) -> None:
        self.state_dir = os.path.join(home, "state")
        self.db = _Database(dbapi)
        self.epoch: Optional[int] = None

    def fence(self, epoch: int) -> None:
        """From now on every write also needs the lease row to hold `epoch`."""
        self.epoch = epoch

    def _key(self, path: str) -> str:
        key = os.path.relpath(path, self.state_dir).replace(os.sep, "/")
        if key.startswith("../") or key == "..":
            raise state_store.StateError("state path %s is outside %s" % (path, self.state_dir))
        return key

    def read_json(self, path: str, default: Any, strict: bool = True) -> Any:
        key = self._key(path)

        def read(cur: Any) -> Any:
            cur.execute(GET_STATE, (key,))
            row = cur.fetchone()
            return row

        try:
            row = self.db.run(read)
            if row is None:
                return default
            value = row[0]
            return json.loads(value) if isinstance(value, str) else value
        except (ValueError, *self.db.errors()) as err:
            if strict:
                raise state_store.StateError("unreadable state %s: %s" % (key, type(err).__name__)) from None
            return default

    def write_json(self, path: str, data: Any) -> None:
        from .station_lease import LeaseLost
        key, text, at = self._key(path), json.dumps(data), time.time()

        def write(cur: Any) -> int:
            if self.epoch is None:
                cur.execute(PUT_STATE, (key, text, at))
            else:
                cur.execute(PUT_STATE_FENCED, (key, text, at, self.epoch))
            return cur.rowcount

        try:
            written = self.db.run(write)
        except self.db.errors() as err:
            raise state_store.StateError(
                "state %s not stored: %s" % (key, type(err).__name__)) from None
        if self.epoch is not None and written == 0:
            raise LeaseLost("state %s not written: the lease no longer holds epoch %s" % (key, self.epoch))


class PostgresCell:
    """The lease record in the station_lease row. Same interface as
    station_lease.FileCell: read, swap (a compare-and-set on the epoch), now."""

    def __init__(self, dbapi: Any) -> None:
        self.db = _Database(dbapi)

    def _run(self, work: Any) -> Any:
        try:
            return self.db.run(work)
        except self.db.errors() as err:
            raise state_store.StateError("station lease: %s" % type(err).__name__) from None

    def now(self) -> float:
        def read(cur: Any) -> float:
            cur.execute(DB_NOW, ())
            return float(cur.fetchone()[0])
        return self._run(read)

    def read(self):
        from .station_lease import Record

        def read(cur: Any) -> Any:
            cur.execute(GET_LEASE, ())
            return cur.fetchone()

        row = self._run(read)
        return None if row is None else Record(str(row[0]), int(row[1]), float(row[2]))

    def swap(self, expected_epoch: Optional[int], record: Any) -> bool:
        def write(cur: Any) -> int:
            if expected_epoch is None:
                cur.execute(INSERT_LEASE, (record.holder, record.epoch, record.expires))
            else:
                cur.execute(UPDATE_LEASE, (record.holder, record.epoch, record.expires,
                                           expected_epoch))
            return cur.rowcount

        return self._run(write) == 1
