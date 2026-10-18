"""Validated transcript storage shared by dashboard readers."""
from __future__ import annotations

import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_ISSUE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$")
_BRAIN = re.compile(r"^brain/\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_FIELDS = ("ticket", "slot", "model", "outcome", "started_at", "finished_at")
EVENT_TEXT_MAX = 5000


def zoned_time(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return bool(re.search(r"(?:Z|[+-]\d\d:\d\d)$", value))


def validate(raw: Any) -> Dict[str, Any]:
    """Validate and normalize an upload body, preserving its field contract."""
    if not isinstance(raw, dict):
        raise ValueError("body must be an object")
    body = dict(raw)
    for key in _FIELDS:
        value = body.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 500:
            raise ValueError("invalid " + key)
    if not (_ISSUE.fullmatch(body["ticket"]) or _BRAIN.fullmatch(body["ticket"])):
        raise ValueError("invalid ticket")
    for key in ("started_at", "finished_at"):
        if not zoned_time(body[key]):
            raise ValueError("invalid " + key)
    events = body.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    normalized = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        for key in ("ts", "role", "kind", "text"):
            if not isinstance(event.get(key), str):
                raise ValueError("invalid event " + key)
        item = dict(event)
        if len(item["text"]) > EVENT_TEXT_MAX:
            item["text"] = item["text"][:EVENT_TEXT_MAX - len("\n[cut]")] + "\n[cut]"
            item["truncated"] = True
        normalized.append(item)
    body["events"] = normalized
    return body


def storage_errors():
    """Exception types callers should catch when database storage fails."""
    try:
        import pg8000.dbapi as dbapi
    except ImportError:
        return (OSError,)
    return (dbapi.Error, OSError)


_CREATE = ("CREATE TABLE IF NOT EXISTS transcripts ("
           "id bigserial primary key, ticket text not null, slot text not null, "
           "model text not null, outcome text not null, started_at timestamptz not null, "
           "finished_at timestamptz not null, events jsonb not null, bytes int not null, "
           "uploaded_at timestamptz default now(), unique (ticket, slot, started_at))")
_UPSERT = ("INSERT INTO transcripts (ticket, slot, model, outcome, started_at, finished_at, events, bytes) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s) "
           "ON CONFLICT (ticket, slot, started_at) DO UPDATE SET model=EXCLUDED.model, "
           "outcome=EXCLUDED.outcome, finished_at=EXCLUDED.finished_at, events=EXCLUDED.events, "
           "bytes=EXCLUDED.bytes, uploaded_at=now() RETURNING id")
_LIST_ALL = "SELECT id, ticket, slot, model, outcome, started_at, finished_at, bytes, uploaded_at FROM transcripts ORDER BY started_at DESC LIMIT 100"
_LIST_TICKET = "SELECT id, ticket, slot, model, outcome, started_at, finished_at, bytes, uploaded_at FROM transcripts WHERE ticket=%s ORDER BY started_at DESC LIMIT 100"
_ATTEMPTS = "SELECT id, ticket, slot, model, outcome, started_at, finished_at, bytes, uploaded_at FROM transcripts WHERE ticket=%s ORDER BY started_at ASC"
# The newest run of every ticket: the Tickets page and the Floor show its model.
_LATEST = ("SELECT DISTINCT ON (ticket) id, ticket, slot, model, outcome, started_at, finished_at, bytes, uploaded_at "
           "FROM transcripts ORDER BY ticket, started_at DESC")
_GET = "SELECT id, ticket, slot, model, outcome, started_at, finished_at, events, bytes FROM transcripts WHERE id=%s"
RETENTION_DAYS = 180
_PURGE = "DELETE FROM transcripts WHERE started_at < now() - INTERVAL '%d days'" % RETENTION_DAYS  # noqa: S608 - RETENTION_DAYS is a fixed integer constant, not user input.


class MemoryBackend:
    def __init__(self):
        self.rows: List[Dict[str, Any]] = []
        self.next_id = 1

    def put(self, body: Dict[str, Any]) -> Dict[str, Any]:
        key = (body["ticket"], body["slot"], body["started_at"])
        row = next((r for r in self.rows if (r["ticket"], r["slot"], r["started_at"]) == key), None)
        if row is None:
            row = {"id": self.next_id}
            self.next_id += 1
            self.rows.append(row)
        row.update(body)
        row["bytes"] = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        return {key: value for key, value in row.items() if key != "events"}

    def read(self, query: str, ticket: Optional[str] = None):
        rows = list(self.rows)
        if ticket is not None:
            rows = [r for r in rows if r["ticket"] == ticket]
        rows.sort(key=lambda r: (r["started_at"], r["id"]), reverse=query != "attempts")
        if query == "list":
            rows = rows[:100]
        if query == "latest":
            newest = {}
            for r in rows:
                newest.setdefault(r["ticket"], r)
            rows = list(newest.values())
        return [{key: value for key, value in r.items() if key != "events"} for r in rows]

    def get(self, id_: int):
        return next((dict(r) for r in self.rows if r["id"] == id_), None)

    def initialize(self):
        return None


def connect(dbapi):
    """A connection to the dashboard's database, configured by LOOM_DB_*;
    the brain memory tables (dashboard/brain_memory.py) share it."""
    config = {}
    for key in ("LOOM_DB_HOST", "LOOM_DB_NAME", "LOOM_DB_USER", "LOOM_DB_PASSWORD"):
        value = os.environ.get(key)
        if not value:
            raise dbapi.Error("Missing required database configuration: " + key)
        config[key] = value
    return dbapi.connect(host=config["LOOM_DB_HOST"], database=config["LOOM_DB_NAME"],
                         user=config["LOOM_DB_USER"], password=config["LOOM_DB_PASSWORD"])


class PostgresBackend:
    def __init__(self, module):
        self.dbapi = module

    def _connect(self):
        return connect(self.dbapi)

    def initialize(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(_CREATE)
            cur.execute(_PURGE)
            conn.commit()
        finally:
            conn.close()

    def put(self, body):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(_PURGE)
            cur.execute(_UPSERT, (body["ticket"], body["slot"], body["model"], body["outcome"],
                                  body["started_at"], body["finished_at"], json.dumps(body["events"]),
                                  len(json.dumps(body, ensure_ascii=False).encode("utf-8"))))
            conn.commit()
            body["id"] = cur.fetchone()[0]
            return body
        finally:
            conn.close()

    def read(self, query, ticket=None):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(_PURGE)
            if query == "latest":
                cur.execute(_LATEST, ())
            else:
                cur.execute(_LIST_TICKET if query == "list" and ticket is not None else
                            _LIST_ALL if query == "list" else _ATTEMPTS,
                            (ticket,) if query == "attempts" or ticket is not None else ())
            names = [column[0] for column in cur.description]
            rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
            for row in rows:
                if isinstance(row.get("events"), str):
                    row["events"] = json.loads(row["events"])
            return rows
        finally:
            conn.close()

    def get(self, id_):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(_PURGE)
            cur.execute(_GET, (id_,))
            row = cur.fetchone()
            if row is None:
                return None
            result = dict(zip([c[0] for c in cur.description], row, strict=True))
            if isinstance(result.get("events"), str):
                result["events"] = json.loads(result["events"])
            return result
        finally:
            conn.close()


def _backend():
    return choose_backend(MemoryBackend, PostgresBackend)


def choose_backend(memory, postgres):
    """memory() when LOOM_TRANSCRIPTS_MEMORY=1, else postgres(the pg8000 DB-API
    module) when the database is configured; RuntimeError otherwise."""
    if os.environ.get("LOOM_TRANSCRIPTS_MEMORY") == "1":
        return memory()
    if os.environ.get("LOOM_DB_PASSWORD") and importlib.util.find_spec("pg8000") is not None:
        import pg8000.dbapi
        return postgres(pg8000.dbapi)
    missing = [key for key in ("LOOM_DB_HOST", "LOOM_DB_NAME", "LOOM_DB_USER", "LOOM_DB_PASSWORD")
               if not os.environ.get(key)]
    if missing:
        raise RuntimeError("Transcript database is not configured; missing " + ", ".join(missing) +
                           ". Set LOOM_TRANSCRIPTS_MEMORY=1 to explicitly use memory storage.")
    raise RuntimeError("Transcript database requires the pg8000 package")


def plain(row):
    """A stored row with every datetime as an ISO time ending in Z: Postgres
    returns datetime objects for started_at and finished_at, which JSON cannot
    carry (the runs API answered 502 on 2026-10-15), while the memory store
    returns the strings it was given."""
    if row is None:
        return None
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
            value = value.isoformat(timespec="seconds") + "Z"
        out[key] = value
    return out


class Store:
    def __init__(self, backend=None):
        self.backend = backend if backend is not None else _backend()

    def upload(self, raw):
        return self.backend.put(validate(raw))

    def initialize(self):
        self.backend.initialize()

    def list(self, ticket=None):
        return [plain(row) for row in self.backend.read("list", ticket)]

    def attempts(self, ticket):
        return [dict(plain(row), attempt=i)
                for i, row in enumerate(self.backend.read("attempts", ticket), 1)]

    def get(self, id_):
        return plain(self.backend.get(id_))

    def latest(self):
        """{ticket: its newest run} over every ticket with a stored run."""
        return {row["ticket"]: row for row in (plain(r) for r in self.backend.read("latest"))}
