"""The station's durable state goes through a store interface whose default
file backend behaves exactly as the station's own helpers did (loom#3050).
Nothing here needs a database: the default stays files."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import state_store, station  # noqa: E402


class RecordingStore(state_store.StateStore):
    """A store that keeps JSON in a dict and notes each call."""

    def __init__(self):
        self.data = {}
        self.calls = []

    def read_json(self, path, default, strict=True):
        self.calls.append(("read", path))
        return self.data.get(path, default)

    def write_json(self, path, data):
        self.calls.append(("write", path))
        self.data[path] = data


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = state_store.FileStore()

    def path(self, *parts):
        return os.path.join(self.tmp.name, *parts)

    def test_a_missing_file_reads_as_the_default(self):
        self.assertEqual(self.store.read_json(self.path("none.json"), {"a": 1}), {"a": 1})

    def test_a_write_makes_its_directory_and_reads_back(self):
        path = self.path("state", "station", "reports.json")
        self.store.write_json(path, {"x": [1, 2]})
        self.assertEqual(self.store.read_json(path, None), {"x": [1, 2]})
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_a_corrupt_file_raises_when_strict_and_defaults_when_not(self):
        path = self.path("bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(state_store.StateError):
            self.store.read_json(path, {})
        self.assertEqual(self.store.read_json(path, "fallback", strict=False), "fallback")

    def test_the_station_keeps_one_state_error_class(self):
        self.assertIs(station.StateError, state_store.StateError)


class SelectTest(unittest.TestCase):
    def test_files_are_the_default(self):
        self.assertIsInstance(state_store.select({}), state_store.FileStore)
        self.assertIsInstance(state_store.select({"LOOM_STATE_BACKEND": "file"}),
                              state_store.FileStore)

    def test_an_unknown_backend_is_refused_not_ignored(self):
        with self.assertRaises(ValueError):
            state_store.select({"LOOM_STATE_BACKEND": "carrier-pigeon"})


class StationHelpersGoThroughTheActiveStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = RecordingStore()
        self.previous = state_store.use(self.store)
        self.addCleanup(state_store.use, self.previous)

    def test_read_and_write_json_use_the_active_store(self):
        station._write_json("state/station/a.json", {"n": 1})
        self.assertEqual(station._read_json("state/station/a.json", None), {"n": 1})
        self.assertEqual(station._read_json("state/station/b.json", "d"), "d")
        self.assertEqual(self.store.calls, [("write", "state/station/a.json"),
                                            ("read", "state/station/a.json"),
                                            ("read", "state/station/b.json")])

    def test_the_default_store_is_the_file_store(self):
        state_store.use(self.previous)
        self.assertIsInstance(state_store.active(), state_store.FileStore)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.json")
            station._write_json(path, {"k": "v"})
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"k": "v"})


if __name__ == "__main__":
    unittest.main()
