"""The one-shot import of file ledgers into the shared backend (loom#3050).
Runs against the in-memory database stand-in from test_state_postgres."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from bus import state_import, state_postgres  # noqa: E402
from test_state_postgres import FakeDb, FakeDbapi, connected  # noqa: E402


class ImportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.db = FakeDb()
        patch = connected(self.db)
        patch.start()
        self.addCleanup(patch.stop)
        self.target = state_postgres.PostgresStore(self.home, FakeDbapi)
        self.lines = []
        self.put("station/reported.json", {"a": 1, "b": 2})
        self.put("station/asks-repo.json", [1, 2, 3])
        self.put("station/etags/abc.json", {"etag": "x"})
        self.put("station/disk.json", {"free": 1})
        self.put("brain.json", {"tick": 1})
        self.put("usage/lane.json", {"beat": 1})
        self.put("station/notes.txt", "not a ledger")

    def put(self, key, data):
        path = os.path.join(self.home, "state", *key.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh) if key.endswith(".json") else fh.write(data)

    def run_import(self, **kw):
        return state_import.run(self.home, self.target, say=self.lines.append, **kw)

    def test_only_shared_json_ledgers_are_listed(self):
        keys = [k for k, _ in state_import.ledgers(self.home)]
        self.assertEqual(keys, ["station/asks-repo.json", "station/reported.json"])

    def test_host_local_ledgers_and_the_etag_cache_are_never_copied(self):
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(sorted(self.db.state), ["station/asks-repo.json", "station/reported.json"])

    def test_copied_ledgers_read_back_equal(self):
        self.run_import()
        path = os.path.join(self.home, "state", "station", "reported.json")
        self.assertEqual(self.target.read_json(path, None), {"a": 1, "b": 2})

    def test_a_dry_run_writes_nothing_and_prints_counts_per_ledger(self):
        self.assertEqual(self.run_import(dry_run=True), 0)
        self.assertEqual(self.db.state, {})
        text = "\n".join(self.lines)
        self.assertRegex(text, r"station/reported.json\s+would copy\s+2 entries")
        self.assertRegex(text, r"station/asks-repo.json\s+would copy\s+3 entries")
        self.assertIn("dry run, nothing written", self.lines[-1])

    def test_a_second_run_changes_nothing(self):
        self.run_import()
        before = dict(self.db.state)
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.db.state, before)
        self.assertIn("kept 2", self.lines[-1])

    def test_a_ledger_the_store_holds_is_kept_not_clobbered(self):
        path = os.path.join(self.home, "state", "station", "reported.json")
        self.target.write_json(path, {"newer": True})
        self.run_import()
        self.assertEqual(self.target.read_json(path, None), {"newer": True})

    def test_overwrite_replaces_it(self):
        path = os.path.join(self.home, "state", "station", "reported.json")
        self.target.write_json(path, {"newer": True})
        self.run_import(overwrite=True)
        self.assertEqual(self.target.read_json(path, None), {"a": 1, "b": 2})

    def test_skip_leaves_a_named_ledger_out(self):
        self.run_import(skip=("station/asks-repo.json",))
        self.assertEqual(list(self.db.state), ["station/reported.json"])

    def test_an_unreadable_file_fails_the_run_but_not_the_others(self):
        self.put("station/broken.json", "{not json")
        with open(os.path.join(self.home, "state", "station", "broken.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.run_import(), 1)
        self.assertIn("station/reported.json", self.db.state)
        self.assertTrue(any("UNREADABLE" in line for line in self.lines))

    def test_a_database_error_fails_the_run(self):
        self.db.fail = FakeDbapi.Error("down")
        self.assertEqual(self.run_import(), 1)
        self.assertTrue(any("FAILED" in line for line in self.lines))


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        os.makedirs(os.path.join(self.home, "state"))
        self.lines = []

    def test_a_real_import_needs_the_postgres_backend(self):
        code = state_import.main({"LOOM_HOME": self.home}, False, False, (), self.lines.append)
        self.assertEqual(code, 2)
        self.assertIn("postgres", self.lines[0])

    def test_a_dry_run_without_a_database_still_counts_the_files(self):
        with open(os.path.join(self.home, "state", "x.json"), "w") as fh:
            json.dump([1], fh)
        code = state_import.main({"LOOM_HOME": self.home}, True, False, (), self.lines.append)
        self.assertEqual(code, 0)
        self.assertIn("x.json", self.lines[0])

    def test_a_missing_state_directory_is_an_error(self):
        code = state_import.main({"LOOM_HOME": os.path.join(self.home, "none")}, True, False, (),
                                 self.lines.append)
        self.assertEqual(code, 2)


try:
    import fcntl
except ImportError:  # not on a POSIX host
    fcntl = None


@unittest.skipIf(fcntl is None, "needs fcntl")
class BusyTest(unittest.TestCase):
    def test_a_held_station_lock_stops_a_real_import(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "state"))
            self.assertFalse(state_import.station_busy(home))
            fd = os.open(os.path.join(home, "state", "station.lock"), os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self.assertTrue(state_import.station_busy(home))
            finally:
                os.close(fd)

    def test_the_lock_is_held_for_the_whole_import_and_freed_after(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "state"))
            with state_import.hold_station_lock(home) as acquired:
                self.assertTrue(acquired)
                self.assertTrue(state_import.station_busy(home))
                with state_import.hold_station_lock(home) as second:
                    self.assertFalse(second)
            self.assertFalse(state_import.station_busy(home))


if __name__ == "__main__":
    unittest.main()
