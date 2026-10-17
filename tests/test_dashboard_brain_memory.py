"""Brain memory on the dashboard (docs/decisions/0002, item 3): the tick,
commitment and rules tables, and the handoff packet endpoint."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import brain_memory  # noqa: E402
from dashboard.server import _handler_for  # noqa: E402

TOKEN = "YOUR_UPLOAD_TOKEN_HERE"


def tick(n, commitments=()):
    return {"holder": "lane-machine", "lane": "codex", "model": "model-c",
            "started": "2026-10-17T%02d:00:00Z" % n, "ended": "2026-10-17T%02d:20:00Z" % n,
            "outcome": "ok", "decided": ["merge: merged example/repo#%d" % n],
            "left_open": [], "handed_off": [], "commitments": list(commitments)}


def defer(number, what="merge"):
    return {"item": "example/repo#%d" % number, "stage": "merge", "key": "", "what": what,
            "due": "2026-10-17T23:00:00Z", "state": "open"}


class Call:
    """One request through the dashboard's handler, as the usage tests drive it."""

    def __init__(self, state):
        self.handler_type = _handler_for(state)

    def __call__(self, method, path, body=None, auth="Bearer " + TOKEN):
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = Message()
        raw = b"" if body is None else json.dumps(body).encode()
        handler.headers["Content-Length"] = str(len(raw))
        if auth:
            handler.headers["Authorization"] = auth
        handler.rfile = io.BytesIO(raw)
        result = []
        handler.send_json = lambda code, value: result.append((code, value))
        handler.send_text = lambda code, value: result.append((code, value))
        (handler.do_POST if method == "POST" else handler.do_GET)()
        return result[0]


class HandoffEndpointTest(unittest.TestCase):
    def setUp(self):
        class State:
            pass
        self.state = State()
        self.state.brain_memory = brain_memory.Store(brain_memory.MemoryBackend())
        self.call = Call(self.state)
        patcher = patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_handoff_returns_last_five_ticks_open_commitments_and_rules_with_their_ref(self):
        for n in range(1, 8):
            code, _ = self.call("POST", "/api/brain/ticks", tick(n, [defer(100 + n)]))
            self.assertEqual(code, 201)
        done = dict(defer(101), state="done", due=None)
        self.assertEqual(self.call("POST", "/api/brain/ticks", tick(8, [done]))[0], 201)
        rules = {"rules": [{"source": "docs/MUSE.md", "source_ref": "a" * 40,
                            "text": "- Write ASCII only."},
                           {"source": "example/repo#1", "source_ref": "comment 55",
                            "text": "- Ship on Tuesdays."}]}
        self.assertEqual(self.call("POST", "/api/brain/rules", rules)[0], 201)
        newer = {"rules": [{"source": "docs/MUSE.md", "source_ref": "b" * 40,
                            "text": "- Write ASCII only. Stop at 25 minutes."}]}
        self.assertEqual(self.call("POST", "/api/brain/rules", newer)[0], 201)

        code, packet = self.call("GET", "/api/brain/handoff")
        self.assertEqual(code, 200)
        self.assertEqual([t["started"] for t in packet["ticks"]],
                         ["2026-10-17T%02d:00:00Z" % n for n in (8, 7, 6, 5, 4)])
        self.assertEqual(packet["ticks"][0]["decided"], ["merge: merged example/repo#8"])
        self.assertEqual(sorted(c["item"] for c in packet["commitments"]),
                         ["example/repo#%d" % n for n in range(102, 108)])
        self.assertTrue(all(c["state"] == "open" for c in packet["commitments"]))
        self.assertEqual({r["source"]: (r["source_ref"], r["text"]) for r in packet["rules"]},
                         {"docs/MUSE.md": ("b" * 40, "- Write ASCII only. Stop at 25 minutes."),
                          "example/repo#1": ("comment 55", "- Ship on Tuesdays.")})

    def test_every_brain_endpoint_refuses_a_request_without_the_token(self):
        for method, path, body in (("GET", "/api/brain/handoff", None),
                                   ("POST", "/api/brain/ticks", tick(1)),
                                   ("POST", "/api/brain/rules", {"rules": []})):
            for auth in ("", "Bearer wrong", TOKEN):
                with self.subTest(path=path, auth=auth):
                    self.assertEqual(self.call(method, path, body, auth=auth),
                                     (401, {"error": "unauthorized"}))
        self.assertEqual(self.state.brain_memory.handoff()["ticks"], [])

    def test_the_handoff_token_reads_the_packet_and_writes_nothing(self):
        reader = "Bearer YOUR_HANDOFF_TOKEN_HERE"
        with patch.dict(os.environ, {"LOOM_HANDOFF_TOKEN": "YOUR_HANDOFF_TOKEN_HERE"}):
            self.assertEqual(self.call("GET", "/api/brain/handoff", auth=reader)[0], 200)
            for path, body in (("/api/brain/ticks", tick(1)),
                               ("/api/brain/rules", {"rules": []}),
                               ("/api/brain/commitments", {"commitments": []})):
                with self.subTest(path=path):
                    self.assertEqual(self.call("POST", path, body, auth=reader)[0], 401)

    def test_done_markers_close_commitments_between_ticks(self):
        self.call("POST", "/api/brain/ticks", tick(1, [defer(5), defer(6)]))
        done = dict(defer(5), state="done", due=None)
        code, answer = self.call("POST", "/api/brain/commitments", {"commitments": [done]})
        self.assertEqual((code, answer), (201, {"closed": 1}))
        packet = self.call("GET", "/api/brain/handoff")[1]
        self.assertEqual([c["item"] for c in packet["commitments"]], ["example/repo#6"])
        code, answer = self.call("POST", "/api/brain/commitments", {"commitments": [defer(7)]})
        self.assertEqual((code, answer), (400, {"error": "only done commitments close without a tick"}))

    def test_no_configured_token_refuses_everything(self):
        with patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": ""}):
            self.assertEqual(self.call("GET", "/api/brain/handoff")[0], 401)

    def test_a_bad_body_is_refused_with_its_reason(self):
        code, answer = self.call("POST", "/api/brain/ticks", dict(tick(1), started="noon"))
        self.assertEqual((code, answer), (400, {"error": "invalid started"}))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = brain_memory.Store(brain_memory.MemoryBackend())

    def test_a_tick_posted_again_is_one_row_and_its_defer_one_commitment(self):
        first = self.store.record_tick(tick(1, [defer(5)]))
        again = self.store.record_tick(tick(1, [defer(5, what="merge: still red")]))
        self.assertEqual(first["id"], again["id"])
        packet = self.store.handoff()
        self.assertEqual(len(packet["ticks"]), 1)
        self.assertEqual([c["what"] for c in packet["commitments"]], ["merge: still red"])

    def test_a_done_closes_only_the_commitment_with_its_stage_and_key(self):
        brief = dict(defer(1), stage="brief", key="0123abcd", what="brief: one problem")
        other = dict(brief, key="89abcdef", what="brief: another problem")
        self.store.record_tick(tick(1, [defer(1), brief, other]))
        closed = self.store.record_tick(tick(2, [dict(brief, state="done", due=None)]))
        self.assertEqual(closed["closed"], 1)
        left = sorted((c["stage"], c["key"]) for c in self.store.handoff()["commitments"])
        self.assertEqual(left, [("brief", "89abcdef"), ("merge", "")])

    def test_a_retired_source_leaves_the_packet(self):
        self.store.load_rules({"rules": [{"source": "example/repo#1", "source_ref": "comment 5",
                                          "text": "- Old rule."}]})
        self.store.load_rules({"rules": [{"source": "example/repo#1", "source_ref": "removed",
                                          "text": ""}]})
        self.assertEqual(self.store.handoff()["rules"], [])

    def test_a_defer_and_its_done_in_one_tick_leave_nothing_open(self):
        self.store.record_tick(tick(1, [defer(3), dict(defer(3), state="done", due=None)]))
        self.assertEqual(self.store.handoff()["commitments"], [])

    def test_validation_refuses_bad_shapes(self):
        for bad in (dict(tick(1), holder="two words"),
                    dict(tick(1), ended="2026-10-17T01:20:00"),
                    dict(tick(1), outcome="maybe"),
                    dict(tick(1), decided="not a list"),
                    dict(tick(1), decided=["x" * (brain_memory.LINE_MAX + 1)]),
                    dict(tick(1), commitments=[dict(defer(1), item="not-an-item")]),
                    dict(tick(1), commitments=[dict(defer(1), state="maybe")]),
                    dict(tick(1), commitments=[dict(defer(1), key="XYZ")]),
                    dict(tick(1), commitments=[dict(defer(1), due="tomorrow")])):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    brain_memory.validate_tick(bad)
        for bad in ({}, {"rules": [{"source": "", "source_ref": "x", "text": "y"}]},
                    {"rules": [{"source": "s", "source_ref": "x", "text": 3}]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    brain_memory.validate_rules(bad)

    def test_unconfigured_store_fails_and_explicit_memory_store_works(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "LOOM_DB_PASSWORD"):
                brain_memory.Store()
            with patch.dict(os.environ, {"LOOM_TRANSCRIPTS_MEMORY": "1"}):
                self.assertIsInstance(brain_memory.Store().backend, brain_memory.MemoryBackend)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.description = None
        self.rows = []

    def execute(self, sql, params=()):
        self.db.statements.append((sql, params))
        answer = self.db.answers.pop(0) if self.db.answers and sql.startswith(
            ("SELECT", "INSERT INTO brain_ticks")) else ([], [])
        names, self.rows = answer
        self.description = [(name,) for name in names]
        self.rowcount = len(self.rows) if sql.startswith("UPDATE") else 0

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeDb:
    """A DB-API module stand-in that records each statement it runs."""

    class Error(Exception):
        pass

    def __init__(self, answers=()):
        self.statements = []
        self.answers = list(answers)
        self.commits = 0

    def connect(self, **config):
        db = self

        class Conn:
            def cursor(self):
                return FakeCursor(db)

            def commit(self):
                db.commits += 1

            def close(self):
                pass
        return Conn()


class PostgresBackendTest(unittest.TestCase):
    ENV = {"LOOM_DB_HOST": "db.example.internal", "LOOM_DB_NAME": "loom",
           "LOOM_DB_USER": "loom", "LOOM_DB_PASSWORD": "YOUR_DB_PASSWORD"}

    def test_a_tick_creates_the_tables_once_then_upserts_and_closes(self):
        db = FakeDb([(["id"], [(7,)]), (["id"], [(8,)])])
        backend = brain_memory.PostgresBackend(db)
        with patch.dict(os.environ, self.ENV):
            store = brain_memory.Store(backend)
            self.assertEqual(store.record_tick(tick(1, [defer(5)]))["id"], 7)
            store.record_tick(tick(2, [dict(defer(5), state="done", due=None)]))
        sql = [s for s, _ in db.statements]
        creates = [s for s in sql if s.startswith("CREATE")]
        self.assertEqual(len(creates), len(brain_memory._SCHEMA))
        self.assertTrue(all(s.startswith("CREATE") for s in sql[:len(creates)]))
        self.assertEqual(sum(1 for s in sql if s.startswith("INSERT INTO brain_ticks")), 2)
        opened = [p for s, p in db.statements if s.startswith("INSERT INTO brain_commitments")]
        self.assertEqual(opened, [(7, "example/repo#5", "merge", "", "merge",
                                   "2026-10-17T23:00:00Z")])
        closed = [p for s, p in db.statements if s.startswith("UPDATE brain_commitments")]
        self.assertEqual(closed, [(8, "2026-10-17T02:20:00Z", "example/repo#5", "merge", "")])
        self.assertEqual(db.commits, 3)


if __name__ == "__main__":
    unittest.main()
