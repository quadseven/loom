"""Brain memory in the dashboard's database (docs/decisions/0002, item 3).

Three tables: brain_ticks (one row per brain tick: holder, lane, model,
start, end, outcome, and what it decided, left open and handed off),
brain_commitments (work a tick deferred, keyed by item, stage and brief key,
open until a done marker for the same work closes it) and brain_rules (the
reviewed rules, one row per source and the commit or comment it came from;
an empty text retires a source). The station writes ticks, closings and
rules (bus/brain_memory.py); every brain reads the handoff packet: the last
PACKET_TICKS ticks, the open commitments and the newest rules of each
source still in force.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from . import transcripts

_HOLDER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ITEM = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$")
_STAGE = re.compile(r"^[a-z]{1,32}$")
_KEY = re.compile(r"^(?:[a-f0-9]{8})?$")
OUTCOMES = ("ok", "failed")
STATES = ("open", "done")
PACKET_TICKS = 5
# Caps on one body; the station clips to the same.
NAME_MAX = 200
LINE_MAX = 500
LIST_MAX = 50
COMMITMENTS_MAX = 200
RULES_MAX = 100
RULE_TEXT_MAX = 20000
TICK_FIELDS = ("id", "holder", "lane", "model", "started", "ended", "outcome",
               "decided", "left_open", "handed_off")
COMMITMENT_FIELDS = ("id", "tick_id", "holder", "item", "stage", "key", "what", "due", "state")
RULE_FIELDS = ("source", "source_ref", "text", "loaded_at")


def _name(body: Dict[str, Any], key: str, limit: int = NAME_MAX) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= limit or "\n" in value:
        raise ValueError("invalid " + key)
    return value


def _time(body: Dict[str, Any], key: str, optional: bool = False) -> Optional[str]:
    value = body.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not transcripts.zoned_time(value):
        raise ValueError("invalid " + key)
    return value


def _lines(body: Dict[str, Any], key: str) -> List[str]:
    value = body.get(key)
    if not isinstance(value, list) or len(value) > LIST_MAX or not all(
            isinstance(line, str) and 1 <= len(line) <= LINE_MAX for line in value):
        raise ValueError("invalid " + key)
    return list(value)


def _matching(pattern: "re.Pattern[str]", body: Dict[str, Any], key: str, default: Any = None) -> str:
    value = body.get(key, default)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("invalid " + key)
    return value


def _commitment(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("commitment must be an object")
    if raw.get("state") not in STATES:
        raise ValueError("invalid commitment state")
    return {"item": _matching(_ITEM, raw, "item"), "stage": _matching(_STAGE, raw, "stage"),
            "key": _matching(_KEY, raw, "key", ""), "what": _name(raw, "what", LINE_MAX),
            "due": _time(raw, "due", optional=True), "state": raw["state"]}


def validate_tick(raw: Any) -> Dict[str, Any]:
    """A tick body as the station posts it, normalized; ValueError names the
    first field that is wrong."""
    if not isinstance(raw, dict):
        raise ValueError("body must be an object")
    if raw.get("outcome") not in OUTCOMES:
        raise ValueError("invalid outcome")
    tick = {"holder": _matching(_HOLDER, raw, "holder"), "lane": _name(raw, "lane"),
            "model": _name(raw, "model"), "started": _time(raw, "started"),
            "ended": _time(raw, "ended"), "outcome": raw["outcome"]}
    for key in ("decided", "left_open", "handed_off"):
        tick[key] = _lines(raw, key)
    commitments = raw.get("commitments", [])
    if not isinstance(commitments, list) or len(commitments) > COMMITMENTS_MAX:
        raise ValueError("invalid commitments")
    tick["commitments"] = [_commitment(c) for c in commitments]
    return tick


def validate_closings(raw: Any) -> List[Dict[str, Any]]:
    """{"commitments": [...]}, each a done commitment: the done markers the
    station sends between ticks."""
    found = raw.get("commitments") if isinstance(raw, dict) else None
    if not isinstance(found, list) or len(found) > COMMITMENTS_MAX:
        raise ValueError("commitments must be a list")
    out = [_commitment(c) for c in found]
    if any(c["state"] != "done" for c in out):
        raise ValueError("only done commitments close without a tick")
    return out


def validate_rules(raw: Any) -> List[Dict[str, str]]:
    """{"rules": [{source, source_ref, text}]}, as a list of rules; an empty
    text retires its source."""
    rules = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(rules, list) or len(rules) > RULES_MAX:
        raise ValueError("rules must be a list")
    out = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("rule must be an object")
        text = rule.get("text")
        if not isinstance(text, str) or len(text) > RULE_TEXT_MAX:
            raise ValueError("invalid rule text")
        out.append({"source": _name(rule, "source"), "source_ref": _name(rule, "source_ref"),
                    "text": text})
    return out


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MemoryBackend:
    def __init__(self):
        self.ticks: List[Dict[str, Any]] = []
        self.commitments: List[Dict[str, Any]] = []
        self.rules: List[Dict[str, Any]] = []

    def put_tick(self, tick: Dict[str, Any]) -> Dict[str, Any]:
        key = (tick["holder"], tick["started"])
        row = next((r for r in self.ticks if (r["holder"], r["started"]) == key), None)
        if row is None:
            row = {"id": len(self.ticks) + 1}
            self.ticks.append(row)
        row.update({k: v for k, v in tick.items() if k != "commitments"})
        opened = closed = 0
        for c in tick["commitments"]:
            if c["state"] == "open":
                self._open(row["id"], c)
                opened += 1
            else:
                closed += self._close(row["id"], tick["ended"], c)
        return {"id": row["id"], "opened": opened, "closed": closed}

    @staticmethod
    def _same(row: Dict[str, Any], c: Dict[str, Any]) -> bool:
        return row["state"] == "open" and (row["item"], row["stage"], row["key"]) == (
            c["item"], c["stage"], c["key"])

    def _open(self, tick_id: int, c: Dict[str, Any]) -> None:
        row = next((r for r in self.commitments if self._same(r, c)), None)
        if row is None:
            row = {"id": len(self.commitments) + 1, "closed_tick": None, "closed_at": None}
            self.commitments.append(row)
        row.update(c, tick_id=tick_id)

    def _close(self, tick_id: Optional[int], at: str, c: Dict[str, Any]) -> int:
        rows = [r for r in self.commitments if self._same(r, c)]
        for row in rows:
            row.update(state="done", closed_tick=tick_id, closed_at=at)
        return len(rows)

    def close(self, closings: List[Dict[str, Any]]) -> int:
        at = _now()
        return sum(self._close(None, at, c) for c in closings)

    def put_rules(self, rules: List[Dict[str, str]]) -> int:
        for rule in rules:
            key = (rule["source"], rule["source_ref"])
            row: Optional[Dict[str, Any]] = next(
                (r for r in self.rules if (r["source"], r["source_ref"]) == key), None)
            if row is None:
                row = {"id": len(self.rules) + 1}
                self.rules.append(row)
            row.update(rule, loaded_at=_now())
        return len(rules)

    def handoff(self) -> Dict[str, List[Dict[str, Any]]]:
        ticks = sorted(self.ticks, key=lambda r: (r["started"], r["id"]), reverse=True)
        holders = {r["id"]: r["holder"] for r in self.ticks}
        open_ = sorted((dict(r, holder=holders[r["tick_id"]]) for r in self.commitments
                        if r["state"] == "open"), key=lambda r: (r["due"] or "~", r["id"]))
        newest: Dict[str, Dict[str, Any]] = {}
        for rule in sorted(self.rules, key=lambda r: (r["loaded_at"], r["id"])):
            newest[rule["source"]] = rule
        return {"ticks": [_only(r, TICK_FIELDS) for r in ticks[:PACKET_TICKS]],
                "commitments": [_only(r, COMMITMENT_FIELDS) for r in open_[:COMMITMENTS_MAX]],
                "rules": [_only(newest[s], RULE_FIELDS) for s in sorted(newest)
                          if newest[s]["text"]]}


def _only(row: Dict[str, Any], fields) -> Dict[str, Any]:
    return {key: row.get(key) for key in fields}


# The tables are created on first use, so a deploy needs no migration step.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS brain_ticks (id bigserial primary key, holder text not null, "
    "lane text not null, model text not null, started timestamptz not null, "
    "ended timestamptz not null, outcome text not null, decided jsonb not null, "
    "left_open jsonb not null, handed_off jsonb not null, posted_at timestamptz default now(), "
    "unique (holder, started))",
    "CREATE TABLE IF NOT EXISTS brain_commitments (id bigserial primary key, "
    "tick_id bigint not null references brain_ticks (id), item text not null, "
    "stage text not null, key text not null, what text not null, due timestamptz, "
    "state text not null, closed_tick bigint references brain_ticks (id), "
    "closed_at timestamptz)",
    "CREATE UNIQUE INDEX IF NOT EXISTS brain_commitments_open "
    "ON brain_commitments (item, stage, key) WHERE state = 'open'",
    "CREATE TABLE IF NOT EXISTS brain_rules (id bigserial primary key, source text not null, "
    "source_ref text not null, text text not null, "
    "loaded_at timestamptz not null default now(), unique (source, source_ref))",
)
_PUT_TICK = ("INSERT INTO brain_ticks (holder, lane, model, started, ended, outcome, decided, "
             "left_open, handed_off) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, "
             "%s::jsonb) ON CONFLICT (holder, started) DO UPDATE SET lane=EXCLUDED.lane, "
             "model=EXCLUDED.model, ended=EXCLUDED.ended, outcome=EXCLUDED.outcome, "
             "decided=EXCLUDED.decided, left_open=EXCLUDED.left_open, "
             "handed_off=EXCLUDED.handed_off, posted_at=now() RETURNING id")
_OPEN = ("INSERT INTO brain_commitments (tick_id, item, stage, key, what, due, state) "
         "VALUES (%s, %s, %s, %s, %s, %s, 'open') "
         "ON CONFLICT (item, stage, key) WHERE state = 'open' "
         "DO UPDATE SET tick_id=EXCLUDED.tick_id, what=EXCLUDED.what, due=EXCLUDED.due")
_CLOSE = ("UPDATE brain_commitments SET state='done', closed_tick=%s, closed_at=%s "
          "WHERE item=%s AND stage=%s AND key=%s AND state='open'")
_PUT_RULE = ("INSERT INTO brain_rules (source, source_ref, text) VALUES (%s, %s, %s) "
             "ON CONFLICT (source, source_ref) DO UPDATE SET text=EXCLUDED.text, loaded_at=now()")
_TICKS = ("SELECT id, holder, lane, model, started, ended, outcome, decided, left_open, "
          "handed_off FROM brain_ticks ORDER BY started DESC, id DESC LIMIT %s")
_OPEN_COMMITMENTS = ("SELECT c.id, c.tick_id, t.holder, c.item, c.stage, c.key, c.what, c.due, "
                     "c.state FROM brain_commitments c JOIN brain_ticks t ON t.id = c.tick_id "
                     "WHERE c.state = 'open' ORDER BY c.due ASC NULLS LAST, c.id "
                     "LIMIT %s")
_RULES = ("SELECT source, source_ref, text, loaded_at FROM (SELECT DISTINCT ON (source) "
          "source, source_ref, text, loaded_at FROM brain_rules "
          "ORDER BY source, loaded_at DESC, id DESC) newest WHERE text <> '' ORDER BY source")


def _rows(cur, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    names = [column[0] for column in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _decoded(row: Dict[str, Any]) -> Dict[str, Any]:
    """A tick row with its jsonb lists as lists, whichever way the driver
    returned them."""
    for key in ("decided", "left_open", "handed_off"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


def _close_all(cur, rows) -> int:
    """Close the open commitments rows match in one batch; the count actually closed."""
    if not rows:
        return 0
    cur.executemany(_CLOSE, rows)
    return max(cur.rowcount, 0)


class PostgresBackend:
    def __init__(self, module):
        self.dbapi = module
        self.ready = False

    def _run(self, work):
        conn = transcripts.connect(self.dbapi)
        try:
            cur = conn.cursor()
            if not self.ready:
                for statement in _SCHEMA:
                    cur.execute(statement)
                conn.commit()
                self.ready = True
            result = work(cur)
            conn.commit()
            return result
        finally:
            conn.close()

    def put_tick(self, tick: Dict[str, Any]) -> Dict[str, Any]:
        def write(cur):
            cur.execute(_PUT_TICK, (tick["holder"], tick["lane"], tick["model"], tick["started"],
                                    tick["ended"], tick["outcome"], json.dumps(tick["decided"]),
                                    json.dumps(tick["left_open"]), json.dumps(tick["handed_off"])))
            tick_id = cur.fetchone()[0]
            opens = [(tick_id, c["item"], c["stage"], c["key"], c["what"], c["due"])
                     for c in tick["commitments"] if c["state"] == "open"]
            closes = [(tick_id, tick["ended"], c["item"], c["stage"], c["key"])
                      for c in tick["commitments"] if c["state"] != "open"]
            if opens:
                cur.executemany(_OPEN, opens)
            return {"id": tick_id, "opened": len(opens), "closed": _close_all(cur, closes)}
        return self._run(write)

    def close(self, closings: List[Dict[str, Any]]) -> int:
        at = _now()
        return self._run(lambda cur: _close_all(
            cur, [(None, at, c["item"], c["stage"], c["key"]) for c in closings]))

    def put_rules(self, rules: List[Dict[str, str]]) -> int:
        def write(cur):
            for rule in rules:
                cur.execute(_PUT_RULE, (rule["source"], rule["source_ref"], rule["text"]))
            return len(rules)
        return self._run(write)

    def handoff(self) -> Dict[str, List[Dict[str, Any]]]:
        return self._run(lambda cur: {
            "ticks": [_decoded(row) for row in _rows(cur, _TICKS, (PACKET_TICKS,))],
            "commitments": _rows(cur, _OPEN_COMMITMENTS, (COMMITMENTS_MAX,)),
            "rules": _rows(cur, _RULES)})


class Store:
    def __init__(self, backend=None):
        self.backend = (backend if backend is not None
                        else transcripts.choose_backend(MemoryBackend, PostgresBackend))

    def record_tick(self, raw: Any) -> Dict[str, Any]:
        """Store one tick and its commitments: {id, opened, closed}."""
        return self.backend.put_tick(validate_tick(raw))

    def close_commitments(self, raw: Any) -> Dict[str, int]:
        """Close the open commitments the done markers match: {closed}."""
        return {"closed": self.backend.close(validate_closings(raw))}

    def load_rules(self, raw: Any) -> Dict[str, int]:
        return {"loaded": self.backend.put_rules(validate_rules(raw))}

    def handoff(self) -> Dict[str, List[Dict[str, Any]]]:
        """The packet: {ticks, commitments, rules}, every time as UTC Z."""
        packet = self.backend.handoff()
        return {key: [transcripts.plain(row) for row in rows] for key, rows in packet.items()}
