"""Read-side transcript store contract tests."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
