"""Station-posted snapshots every dashboard replica reads (loom#2530).

The station POSTs its usage readings, its chain snapshot (also its heartbeat
on /fleet), its goal caps and its brain holds, and each pass keeps a bench
list. Behind one Service with two replicas each POST lands on one pod, so
these snapshots live in the dashboard's database, one row per key, last
write wins by its time: every replica reads the same rows, and a restart
keeps the last snapshot.

The table is created on first use, as the brain memory tables are, so a
deploy needs no migration step. With no database configured, from_env
gives a store in this process's memory: the dashboard keeps each snapshot
to itself, as it did before, which is safe for one replica only.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import transcripts
from . import usage as usage_store

LOG = logging.getLogger(__name__)

KEYS = ("usage", "chain", "chain_seen", "goal_caps", "brain_holds", "bench")
# A page read refreshes from the database at most this often; after a failed
# read it waits RETRY_AFTER instead, so a database outage costs one slow
# request a minute rather than every request.
SYNC_EVERY = 5.0
RETRY_AFTER = 60.0

_SCHEMA = ("CREATE TABLE IF NOT EXISTS dashboard_snapshots (key text primary key, "
           "value jsonb not null, posted_at double precision not null)")
_PUT = ("INSERT INTO dashboard_snapshots (key, value, posted_at) VALUES (%s, %s::jsonb, %s) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, posted_at=EXCLUDED.posted_at "
        "WHERE dashboard_snapshots.posted_at <= EXCLUDED.posted_at")
_ALL = "SELECT key, value, posted_at FROM dashboard_snapshots"


class MemoryBackend:
    """One process's rows, when no database is configured."""

    def __init__(self):
        self.rows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value: Any, at: float) -> None:
        with self._lock:
            old = self.rows.get(key)
            if old is None or old["at"] <= at:
                self.rows[key] = {"value": json.loads(json.dumps(value)), "at": float(at)}

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {key: {"value": json.loads(json.dumps(row["value"])), "at": row["at"]}
                    for key, row in self.rows.items()}


class PostgresBackend:
    def __init__(self, module):
        self.dbapi = module
        self.ready = False

    def _run(self, work):
        conn = transcripts.connect(self.dbapi)
        try:
            cur = conn.cursor()
            if not self.ready:
                cur.execute(_SCHEMA)
                conn.commit()
                self.ready = True
            result = work(cur)
            conn.commit()
            return result
        finally:
            conn.close()

    def put(self, key: str, value: Any, at: float) -> None:
        self._run(lambda cur: cur.execute(_PUT, (key, json.dumps(value), float(at))))

    def all(self) -> Dict[str, Dict[str, Any]]:
        def read(cur):
            cur.execute(_ALL)
            out = {}
            for key, value, at in cur.fetchall():
                out[key] = {"value": json.loads(value) if isinstance(value, str) else value,
                            "at": float(at)}
            return out
        return self._run(read)


class Store:
    def __init__(self, backend):
        self.backend = backend

    def put(self, key: str, value: Any, at: Optional[float] = None) -> None:
        if key not in KEYS:
            raise ValueError("unknown snapshot key " + key)
        self.backend.put(key, value, time.time() if at is None else at)

    def all(self) -> Dict[str, Dict[str, Any]]:
        return {key: row for key, row in self.backend.all().items() if key in KEYS}


def from_env() -> Store:
    """A Store over the dashboard's database when it is configured
    (LOOM_DB_PASSWORD and the pg8000 package), else over this process's
    memory, as before. LOOM_TRANSCRIPTS_MEMORY=1 also means memory."""
    if os.environ.get("LOOM_TRANSCRIPTS_MEMORY") == "1" or not os.environ.get("LOOM_DB_PASSWORD") \
            or importlib.util.find_spec("pg8000") is None:
        return Store(MemoryBackend())
    import pg8000.dbapi
    return Store(PostgresBackend(pg8000.dbapi))


def errors():
    """What a failed database read or write raises."""
    return transcripts.storage_errors()


# Each key's form in the database and on the dashboard state.

def _usage_value(store: Any) -> Dict[str, Any]:
    received = getattr(store, "received_at", None)
    return {"providers": dict(getattr(store, "providers", None) or {}),
            "received_at": received.isoformat() if received else None}


def _usage_store(value: Any) -> Any:
    store = usage_store.Store()
    if isinstance(value, dict):
        providers = value.get("providers")
        store.providers = dict(providers) if isinstance(providers, dict) else {}
        received = value.get("received_at")
        if isinstance(received, str):
            try:
                at = datetime.fromisoformat(received)
                store.received_at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            except ValueError:
                store.received_at = None
    return store


def _apply(state: Any, key: str, value: Any) -> None:
    if key == "usage":
        state.usage = _usage_store(value)
    elif key == "chain_seen":
        from . import fleet
        beats = [float(at) for at in value if isinstance(at, (int, float))] if isinstance(value, list) else []
        state.chain_seen = collections.deque(beats, maxlen=fleet.CHAIN_SEEN_MAX)
    elif key == "bench":
        bench = getattr(state, "bench", None)
        if bench is not None and isinstance(value, dict):
            bench.adopt(value)
    else:
        setattr(state, key, value)


def _seen(state: Any) -> Dict[str, float]:
    seen = getattr(state, "_station_state_at", None)
    if seen is None:
        seen = {}
        state._station_state_at = seen
    return seen


def sync(state: Any, force: bool = False, now: Optional[float] = None) -> None:
    """Take every row newer than what this state holds. Throttled to one read
    each SYNC_EVERY seconds unless force; a database error is logged and the
    state keeps what it has. No-op for a state that has no store."""
    store = getattr(state, "station_state", None)
    if store is None:
        return
    now = time.monotonic() if now is None else now
    if not force and now < getattr(state, "_station_state_next", 0.0):
        return
    try:
        rows = store.all()
    except errors() as exc:
        state._station_state_next = now + RETRY_AFTER
        LOG.warning("station snapshots not read: %s", type(exc).__name__)
        return
    state._station_state_next = now + SYNC_EVERY
    seen = _seen(state)
    for key, row in rows.items():
        if row["at"] > seen.get(key, float("-inf")):
            _apply(state, key, row["value"])
            seen[key] = row["at"]


def publish(state: Any, key: str, value: Any, at: Optional[float] = None) -> None:
    """Write one snapshot this state just took. Raises errors() when the
    database refuses it, so the station's POST fails and is sent again; the
    state keeps the value either way. No-op for a state that has no store."""
    store = getattr(state, "station_state", None)
    if store is None:
        return
    at = time.time() if at is None else at
    try:
        store.put(key, value, at)
    except errors() as exc:
        LOG.warning("station snapshot %s not stored: %s", key, type(exc).__name__)
        raise
    _seen(state)[key] = at


def usage_value(state: Any) -> Dict[str, Any]:
    return _usage_value(getattr(state, "usage", None))


def bench_pass(state: Any, sources: Any) -> list:
    """One bench pass on the refresh thread: start from the stored bench
    list, evaluate, and store the result when it changed, so every replica
    publishes one list. A database error is logged; the pass still runs on
    this replica's own state."""
    sync(state, force=True)
    bench = state.bench
    before = bench.state
    events = bench.update(sources)
    if getattr(state, "station_state", None) is not None and bench.state != before:
        try:
            publish(state, "bench", bench.state)
        except errors():
            pass  # publish logged it; the next pass stores the list again
    return events
