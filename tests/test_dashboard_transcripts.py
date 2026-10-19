"""Read-side transcript store contract tests."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import transcripts as station_transcripts  # noqa: E402
from dashboard import transcripts  # noqa: E402


class StoreTests(unittest.TestCase):
    def body(self, started="2026-10-13T10:00:00Z", outcome="done"):
        return {
            "ticket": "example/project#12", "slot": "codex", "model": "model-x",
            "outcome": outcome, "started_at": started, "finished_at": "2026-10-13T10:05:00Z",
            "events": [{"ts": "2026-10-13T10:01:00Z", "role": "assistant",
                        "kind": "text", "text": "finished"}],
        }

    def test_validates_actual_station_body_and_rejects_invalid_shapes(self):
        with tempfile.TemporaryDirectory() as root:
            body = station_transcripts.brain_body(Path(root), 1791885600, 1791885900,
                                                  "done", "model-x", Path(root) / "projects")
        self.assertEqual(transcripts.validate(body), body)
        for bad in (
            dict(body, ticket="bad-ticket"),
            dict(body, started_at="2026-10-13T10:00:00"),
            dict(body, events=[{"ts": "", "role": "assistant", "kind": "text"}]),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    transcripts.validate(bad)

    def test_upsert_and_attempts_order(self):
        store = transcripts.Store(transcripts.MemoryBackend())
        store.upload(self.body("2026-10-13T10:00:00Z"))
        store.upload(self.body("2026-10-13T10:00:00Z", "failed"))
        store.upload(self.body("2026-10-13T11:00:00Z"))
        attempts = store.attempts("example/project#12")
        self.assertEqual(len(store.list()), 2)
        self.assertTrue(all("events" not in row for row in attempts))
        self.assertEqual([row["attempt"] for row in attempts], [1, 2])
        self.assertEqual(attempts[0]["outcome"], "failed")
        self.assertEqual(store.get(attempts[0]["id"])["events"][0]["text"], "finished")
        self.assertEqual(store.upload(self.body("2026-10-13T10:00:00Z"))["id"], attempts[0]["id"])

    def test_long_event_text_is_cut_and_marked(self):
        body = self.body()
        body["events"][0]["text"] = "x" * (transcripts.EVENT_TEXT_MAX + 10)
        event = transcripts.validate(body)["events"][0]
        self.assertTrue(event["truncated"])
        self.assertTrue(event["text"].endswith("\n[cut]"))
        self.assertEqual(len(event["text"]), transcripts.EVENT_TEXT_MAX)

    def test_sql_constants_are_literals_and_missing_pg8000_is_never_imported(self):
        constants = [value for name, value in vars(transcripts).items()
                     if name.startswith("_") and name.isupper() and isinstance(value, str)]
        self.assertTrue(constants)
        self.assertTrue(all(isinstance(value, str) for value in constants))
        self.assertEqual(transcripts.RETENTION_DAYS, 180)
        self.assertIn("now() - INTERVAL '180 days'", transcripts._PURGE)
        with patch.dict(os.environ, {"LOOM_DB_PASSWORD": "present"}, clear=True), \
                patch.object(transcripts.importlib.util, "find_spec", return_value=None), \
                patch.dict(sys.modules, {"pg8000": None, "pg8000.dbapi": None}):
            with patch.dict(os.environ, {"LOOM_TRANSCRIPTS_MEMORY": "1"}):
                self.assertIsInstance(transcripts._backend(), transcripts.MemoryBackend)

    def test_unconfigured_store_fails_and_explicit_memory_store_works(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "LOOM_DB_PASSWORD"):
                transcripts.Store()
            with patch.dict(os.environ, {"LOOM_TRANSCRIPTS_MEMORY": "1"}):
                self.assertIsInstance(transcripts.Store().backend, transcripts.MemoryBackend)


class FakeDBAPI:
    """A DB-API module double that records each connect and every statement."""
    class Error(Exception):
        pass

    def __init__(self):
        self.connects, self.statements, self.commits = [], [], 0

    def connect(self, **kwargs):
        self.connects.append(kwargs)
        return _FakeConnection(self)


class _FakeConnection:
    def __init__(self, module):
        self.module = module

    def cursor(self):
        return _FakeCursor(self.module)

    def commit(self):
        self.module.commits += 1

    def close(self):
        pass


class _FakeCursor:
    description = [("id",), ("ticket",)]

    def __init__(self, module):
        self.module = module

    def execute(self, sql, params=()):
        self.module.statements.append(sql)

    def fetchall(self):
        return [(1, "example/project#12")]

    def fetchone(self):
        return (1, "example/project#12")


DB_ENV = {"LOOM_DB_HOST": "db.invalid", "LOOM_DB_NAME": "loom", "LOOM_DB_USER": "loom",
          "LOOM_DB_PASSWORD": "placeholder"}


class PostgresBackendTests(unittest.TestCase):
    """loom#780: a read never deletes, and a connection never waits forever."""

    def test_reads_run_no_delete(self):
        module = FakeDBAPI()
        backend = transcripts.PostgresBackend(module)
        with patch.dict(os.environ, DB_ENV):
            backend.read("latest")
            backend.read("list")
            backend.read("attempts", "example/project#12")
            backend.get(1)
        self.assertTrue(module.statements)
        self.assertFalse([sql for sql in module.statements if "DELETE" in sql.upper()])

    def test_initialize_and_put_still_purge_and_commit(self):
        module = FakeDBAPI()
        backend = transcripts.PostgresBackend(module)
        with patch.dict(os.environ, DB_ENV):
            backend.initialize()
            backend.put(transcripts.validate(StoreTests().body()))
        self.assertEqual(module.statements.count(transcripts._PURGE), 2)
        self.assertGreaterEqual(module.commits, 2)

    def test_connect_passes_a_timeout_and_sets_a_statement_timeout(self):
        module = FakeDBAPI()
        with patch.dict(os.environ, DB_ENV):
            transcripts.connect(module)
        self.assertEqual(module.connects[0]["timeout"], transcripts.CONNECT_TIMEOUT_SECONDS)
        self.assertLessEqual(transcripts.CONNECT_TIMEOUT_SECONDS, 5)
        self.assertEqual(module.statements[0], "SET statement_timeout = 5000")


class ModelOutcomes(unittest.TestCase):
    """loom#968: run outcomes per lane slot and model over 24 hours and 7 days,
    for the Tiers and allowances page."""

    def run_body(self, slot, model, outcome, started, minutes, ticket="example/project#12"):
        start = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc) - started
        return {"ticket": ticket, "slot": slot, "model": model, "outcome": outcome,
                "started_at": start.isoformat().replace("+00:00", "Z"),
                "finished_at": (start + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
                "events": []}

    def test_memory_store_counts_each_outcome_per_window_and_skips_brain_and_old_runs(self):
        store = transcripts.Store(transcripts.MemoryBackend())
        now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        store.upload(self.run_body("codex", "model-x", "done", timedelta(hours=1), 10))
        # A numbered slot counts with its lane: one row per provider and model.
        store.upload(self.run_body("codex-2", "model-x", "done", timedelta(hours=2), 20,
                                   ticket="example/project#13"))
        store.upload(self.run_body("codex", "model-x", "failed", timedelta(days=3), 5))
        store.upload(self.run_body("anthropic-2", "model-y", "done", timedelta(days=10), 5))
        store.upload(self.run_body("brain", "model-z", "done", timedelta(hours=1), 5,
                                   ticket="brain/2026-10-19T11:00:00Z"))
        rows = {(r["lane"], r["model"], r["outcome"]): r for r in store.model_outcomes(now)}
        self.assertEqual(set(rows), {("codex", "model-x", "done"), ("codex", "model-x", "failed")})
        done = rows[("codex", "model-x", "done")]
        self.assertEqual((done["runs_24h"], done["runs_7d"]), (2, 2))
        self.assertEqual(sorted(done["slots"]), ["codex", "codex-2"])
        self.assertEqual(done["p50_24h_s"], 900.0)
        failed = rows[("codex", "model-x", "failed")]
        self.assertEqual((failed["runs_24h"], failed["runs_7d"]), (0, 1))
        self.assertIsNone(failed["p50_24h_s"])
        self.assertEqual(failed["p50_7d_s"], 300.0)

    def test_postgres_reads_one_aggregate_and_deletes_nothing(self):
        module = FakeDBAPI()
        backend = transcripts.PostgresBackend(module)
        with patch.dict(os.environ, DB_ENV):
            backend.outcomes()
        self.assertIn(transcripts._OUTCOMES, module.statements)
        self.assertFalse([sql for sql in module.statements if "DELETE" in sql.upper()])
        # pg8000's format paramstyle reads a bare percent sign as a placeholder.
        self.assertNotIn("%", transcripts._OUTCOMES)
        self.assertIn("interval '7 days'", transcripts._OUTCOMES)


if __name__ == "__main__":
    unittest.main()
