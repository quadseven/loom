"""Station-posted snapshots shared by every dashboard replica (loom#2530):
two dashboard processes over one database serve the same usage readings,
chain snapshot and heartbeat, goal caps, brain holds and bench list, and a
restart keeps the last snapshot."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import bench  # noqa: E402
from dashboard import bench_store, fleet, station_state  # noqa: E402
from dashboard.server import _handler_for  # noqa: E402
from dashboard.tiers_sources import SourceState  # noqa: E402

TOKEN = "YOUR_UPLOAD_TOKEN_HERE"
STAMP = "2030-01-08T10:00:00+00:00"
READING = {"provider": "codex", "lanes": ["codex"], "window": "weekly", "used_pct": 65,
           "remaining": None, "unit": "percent", "resets_at": None, "source": "session fixture",
           "as_of": STAMP, "note": ""}
CHAIN = {"at": 1000, "shadow_on": True, "repos": [{"repo": "example-owner/sample", "shadow": True}],
         "held": [], "acted": []}
REPO = "example-owner/sample"
GOAL = {"goal": "%s#5" % REPO, "since": 0.0, "in_flight": 32, "cap": 28, "no_lane": 20}
HOLD = {"repo": "example-owner/sample", "old": 7, "person": 1, "unknown": 2}
CFG = bench.Config(threshold=0.5, min_sample=10, cooldown=3600, max_cooldown=86400)


class State:
    """A dashboard process: its own memory, the shared store it was given."""

    def __init__(self, store):
        self.station_state = store
        self.bench = bench_store.BenchStore("", CFG)


def call(state, method, path, body=None):
    handler_type = _handler_for(state)
    handler = handler_type.__new__(handler_type)
    handler.path = path
    handler.headers = Message()
    raw = b"" if body is None else json.dumps(body).encode()
    handler.headers["Content-Length"] = str(len(raw))
    handler.headers["Authorization"] = "Bearer " + TOKEN
    handler.rfile = io.BytesIO(raw)
    result = []
    handler.send_json = lambda code, value: result.append((code, value))
    handler.send_text = lambda code, value: result.append((code, value))
    (handler.do_POST if method == "POST" else handler.do_GET)()
    return result[0]


class SharedBase(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)
        every = patch.object(station_state, "SYNC_EVERY", 0.0)
        every.start()
        self.addCleanup(every.stop)
        self.db = station_state.MemoryBackend()
        self.first = State(station_state.Store(self.db))
        self.second = State(station_state.Store(self.db))


class TwoReplicasTest(SharedBase):
    def test_a_usage_post_on_one_process_is_served_by_the_other(self):
        self.assertEqual(call(self.first, "POST", "/api/usage", [READING])[0], 201)
        code, body = call(self.second, "GET", "/api/usage")
        self.assertEqual(code, 200)
        self.assertEqual([r["provider"] for r in body["readings"]], ["codex"])
        self.assertIsNotNone(body["received_at"])
        # A second provider posted to the other process merges with the first.
        other = dict(READING, provider="anthropic", lanes=["anthropic"])
        self.assertEqual(call(self.second, "POST", "/api/usage", [other])[0], 201)
        _, body = call(self.first, "GET", "/api/usage")
        self.assertEqual(sorted(r["provider"] for r in body["readings"]), ["anthropic", "codex"])

    def test_a_chain_post_on_one_process_is_the_heartbeat_on_the_others_fleet(self):
        self.assertEqual(call(self.first, "POST", "/api/chain", CHAIN), (201, {"repos": 1}))
        call(self.second, "GET", "/api/usage")
        self.assertEqual(self.second.chain["repos"], CHAIN["repos"])
        self.assertEqual(list(self.second.chain_seen), list(self.first.chain_seen))
        now = datetime.fromtimestamp(self.first.chain_seen[0], timezone.utc)
        rows = fleet.build_fleet({}, [], list(self.second.chain_seen), now)["rows"]
        station = next(row for row in rows if row["kind"] == "station")
        self.assertNotIn("no chain snapshot", station.get("note") or "")
        # The next beat lands on the second process; both keep both beats.
        self.assertEqual(call(self.second, "POST", "/api/chain", CHAIN)[0], 201)
        call(self.first, "GET", "/api/usage")
        self.assertEqual(len(self.first.chain_seen), 2)

    def test_goal_caps_and_brain_holds_posted_to_one_process_show_on_the_other(self):
        self.assertEqual(call(self.first, "POST", "/api/goal-caps", {"goals": [GOAL]}), (201, {"goals": 1}))
        self.assertEqual(call(self.first, "POST", "/api/brain-holds", {"repos": [HOLD]}), (201, {"repos": 1}))
        call(self.second, "GET", "/api/usage")
        self.assertEqual(self.second.goal_caps["goals"][0]["goal"], GOAL["goal"])
        self.assertEqual(self.second.brain_holds, self.first.brain_holds)

    def test_two_processes_publish_one_bench_list(self):
        gateway = {"gateway_24h": SourceState(name="g", data={"rows": [
            {"target": "free-kilo", "model": "kilo-coder", "hits": 20, "ok": 0, "limit": 0}]},
            last_good_at=1, read_at=1)}
        events = station_state.bench_pass(self.first, gateway)
        self.assertEqual([e["kind"] for e in events], ["bench"])
        code, listed = call(self.second, "GET", "/api/bench")
        self.assertEqual(code, 200)
        self.assertEqual([b["model"] for b in listed["benched"]], ["kilo-coder"])
        _, first = call(self.first, "GET", "/api/bench")
        self.assertEqual(listed["benched"], first["benched"])

    def test_a_restart_keeps_the_last_snapshot(self):
        call(self.first, "POST", "/api/usage", [READING])
        call(self.first, "POST", "/api/chain", CHAIN)
        restarted = State(station_state.Store(self.db))
        _, body = call(restarted, "GET", "/api/usage")
        self.assertEqual([r["provider"] for r in body["readings"]], ["codex"])
        self.assertEqual(restarted.chain["repos"], CHAIN["repos"])
        self.assertEqual(len(restarted.chain_seen), 1)

    def test_an_older_write_never_replaces_a_newer_one(self):
        self.db.put("goal_caps", {"goals": [], "received_at": 2.0}, 2.0)
        self.db.put("goal_caps", {"goals": [], "received_at": 1.0}, 1.0)
        self.assertEqual(self.db.all()["goal_caps"]["value"]["received_at"], 2.0)


class FailingBackend(station_state.MemoryBackend):
    def put(self, key, value, at):
        raise OSError("database down")

    def all(self):
        raise OSError("database down")


class DatabaseDownTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_post_the_database_refuses_is_503_so_the_station_sends_it_again(self):
        state = State(station_state.Store(FailingBackend()))
        with self.assertLogs("dashboard.station_state", level="WARNING"):
            code, _ = call(state, "POST", "/api/goal-caps", {"goals": [GOAL]})
        self.assertEqual(code, 503)
        # This process still shows what it received.
        self.assertEqual(state.goal_caps["goals"][0]["goal"], GOAL["goal"])

    def test_a_page_read_with_the_database_down_serves_this_process_memory(self):
        state = State(station_state.Store(FailingBackend()))
        state.goal_caps = {"goals": [GOAL], "received_at": 1.0}
        with self.assertLogs("dashboard.station_state", level="WARNING"):
            code, _ = call(state, "GET", "/api/usage")
        self.assertEqual(code, 200)
        # The failed read waits RETRY_AFTER before the next try.
        state.station_state.backend = station_state.MemoryBackend()
        station_state.sync(state)
        self.assertGreater(state._station_state_next, 0)


class NoDatabaseTest(unittest.TestCase):
    def test_without_a_database_each_process_keeps_its_own_memory_as_before(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(station_state.from_env().backend, station_state.MemoryBackend)
        with patch.dict(os.environ, {"LOOM_TRANSCRIPTS_MEMORY": "1", "LOOM_DB_PASSWORD": "x"}):
            self.assertIsInstance(station_state.from_env().backend, station_state.MemoryBackend)
        # Two processes without a database each keep their own snapshots.
        with patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": TOKEN}, clear=True):
            one, two = State(station_state.from_env()), State(station_state.from_env())
            self.assertEqual(call(one, "POST", "/api/usage", [READING])[0], 201)
            self.assertEqual(call(two, "GET", "/api/usage")[1]["readings"], [])
            self.assertEqual(len(call(one, "GET", "/api/usage")[1]["readings"]), 1)

        class Plain:
            pass

        state = Plain()
        with patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": TOKEN}):
            self.assertEqual(call(state, "POST", "/api/usage", [READING])[0], 201)
            self.assertEqual(call(state, "POST", "/api/chain", CHAIN)[0], 201)
            _, body = call(state, "GET", "/api/usage")
        self.assertEqual([r["provider"] for r in body["readings"]], ["codex"])
        self.assertEqual(len(state.chain_seen), 1)

    def test_the_dashboard_store_wires_the_shared_snapshots_from_its_env(self):
        from dashboard import github
        source = Path(github.__file__).read_text(encoding="utf-8")
        self.assertIn("self.station_state = station_state.from_env()", source)
        self.assertIn("station_state.bench_pass(self, self.tier_sources.snapshot())", source)


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=()):
        self.db.statements.append((sql, params))

    def fetchall(self):
        return [("chain", '{"repos": []}', 5.0), ("not-a-key", "{}", 1.0)]


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db.commits += 1

    def close(self):
        pass


class PostgresBackendTest(unittest.TestCase):
    def test_the_table_is_made_on_first_use_and_the_write_keeps_the_newest(self):
        class Db:
            statements = []
            commits = 0

        db = Db()
        backend = station_state.PostgresBackend(object())
        with patch.object(station_state.transcripts, "connect", lambda dbapi: FakeConnection(db)):
            store = station_state.Store(backend)
            store.put("chain", {"repos": []}, 5.0)
            rows = store.all()
        create, put = db.statements[0][0], db.statements[1]
        self.assertIn("CREATE TABLE IF NOT EXISTS dashboard_snapshots", create)
        self.assertEqual(sum("CREATE" in sql for sql, _ in db.statements), 1)
        self.assertIn("ON CONFLICT (key) DO UPDATE", put[0])
        self.assertIn("WHERE dashboard_snapshots.posted_at <= EXCLUDED.posted_at", put[0])
        self.assertEqual(put[1], ("chain", '{"repos": []}', 5.0))
        self.assertEqual(rows, {"chain": {"value": {"repos": []}, "at": 5.0}})
        with self.assertRaises(ValueError):
            store.put("unknown", {}, 1.0)


if __name__ == "__main__":
    unittest.main()
