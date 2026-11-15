"""The station's shared backend: its ledgers and its lease in the dashboard's
Postgres, reached the way the dashboard reaches it (loom#3050). Everything
here runs against an in-memory stand-in for the database; nothing connects
anywhere, and the backend is off unless LOOM_STATE_BACKEND=postgres."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import state_postgres, state_store, station, station_lease  # noqa: E402

HOME = os.path.join("loom-home")


class FakeDb:
    """The three tables the backend uses, and the statements it sends. The
    database clock is `now`; one conditional write is atomic, as in Postgres."""

    def __init__(self):
        self.state = {}        # key -> (value json text, updated_at)
        self.lease = {}        # name -> [holder, epoch, expires]
        self.now = 5000.0
        self.created = []
        self.fail = None

    def execute(self, sql, params):
        if self.fail:
            raise self.fail
        if sql.startswith("CREATE TABLE IF NOT EXISTS"):
            self.created.append(sql.split()[5])
            return [], 0
        if sql == state_postgres.GET_STATE:
            row = self.state.get(params[0])
            return ([(row[0],)] if row else []), 0
        if sql == state_postgres.PUT_STATE:
            self.state[params[0]] = (params[1], params[2])
            return [], 1
        if sql == state_postgres.PUT_STATE_FENCED:
            key, value, at, epoch = params
            held = self.lease.get("station")
            if held is None or held[1] != epoch:
                return [], 0
            self.state[key] = (value, at)
            return [], 1
        if sql == state_postgres.GET_LEASE:
            row = self.lease.get("station")
            return ([tuple(row)] if row else []), 0
        if sql == state_postgres.INSERT_LEASE:
            if "station" in self.lease:
                return [], 0
            self.lease["station"] = [params[0], params[1], params[2]]
            return [], 1
        if sql == state_postgres.UPDATE_LEASE:
            holder, epoch, expires, expected = params
            row = self.lease.get("station")
            if row is None or row[1] != expected:
                return [], 0
            self.lease["station"] = [holder, epoch, expires]
            return [], 1
        if sql == state_postgres.DB_NOW:
            return [(self.now,)], 0
        raise AssertionError("unexpected statement " + sql)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
        self.rowcount = -1

    def execute(self, sql, params=()):
        self.rows, self.rowcount = self.db.execute(sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass

    def close(self):
        pass


class FakeDbapi:
    class Error(Exception):
        pass


def connected(db):
    return mock.patch.object(state_postgres.transcripts, "connect",
                             lambda dbapi: FakeConnection(db))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        patch = connected(self.db)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = state_postgres.PostgresStore(HOME, FakeDbapi)

    def path(self, *parts):
        return os.path.join(HOME, "state", *parts)

    def test_a_ledger_reads_back_under_its_name_below_the_state_directory(self):
        self.store.write_json(self.path("station", "reports.json"), {"sent": [1, 2]})
        self.assertEqual(list(self.db.state), ["station/reports.json"])
        self.assertEqual(self.store.read_json(self.path("station", "reports.json"), None),
                         {"sent": [1, 2]})

    def test_a_missing_ledger_reads_as_the_default(self):
        self.assertEqual(self.store.read_json(self.path("station", "none.json"), {"d": 1}), {"d": 1})

    def test_tables_are_made_once_on_first_use(self):
        self.store.write_json(self.path("a.json"), 1)
        self.store.write_json(self.path("b.json"), 2)
        self.store.read_json(self.path("a.json"), None)
        self.assertEqual(sorted(self.db.created), ["loom_state", "station_lease"])

    def test_a_path_outside_the_state_directory_is_refused(self):
        with self.assertRaises(state_store.StateError):
            self.store.write_json(os.path.join("elsewhere", "x.json"), 1)

    def test_an_unreadable_row_raises_when_strict_and_defaults_when_not(self):
        self.db.state["station/bad.json"] = ("{not json", 1.0)
        with self.assertRaises(state_store.StateError):
            self.store.read_json(self.path("station", "bad.json"), {})
        self.assertEqual(self.store.read_json(self.path("station", "bad.json"), "d", strict=False), "d")

    def test_a_database_error_is_a_state_error_on_a_strict_read_and_a_write(self):
        self.db.fail = FakeDbapi.Error("connection refused")
        with self.assertRaises(state_store.StateError):
            self.store.read_json(self.path("station", "a.json"), {})
        with self.assertRaises(state_store.StateError):
            self.store.write_json(self.path("station", "a.json"), {})
        self.assertEqual(self.store.read_json(self.path("station", "a.json"), "d", strict=False), "d")


class LeaseCellTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        patch = connected(self.db)
        patch.start()
        self.addCleanup(patch.stop)

    def lease(self, holder):
        return station_lease.StationLease(state_postgres.PostgresCell(FakeDbapi), holder, 90)

    def test_two_stations_share_one_lease_and_the_database_clock_rules(self):
        alpha, beta = self.lease("alpha"), self.lease("beta")
        self.assertTrue(alpha.acquire())
        self.assertFalse(beta.acquire())
        self.db.now += 91                      # alpha stops renewing
        self.assertTrue(beta.acquire())
        self.assertEqual(beta.epoch, 2)
        with self.assertRaises(station_lease.LeaseLost):
            alpha.renew()

    def test_a_lost_race_for_a_free_lease_has_one_winner(self):
        alpha, beta = self.lease("alpha"), self.lease("beta")
        # Both read "no lease", then both try to insert it.
        cell_a, cell_b = alpha.cell, beta.cell
        self.assertIsNone(cell_a.read())
        self.assertIsNone(cell_b.read())
        self.assertTrue(cell_a.swap(None, station_lease.Record("alpha", 1, 9000.0)))
        self.assertFalse(cell_b.swap(None, station_lease.Record("beta", 1, 9000.0)))


class RoutedTest(unittest.TestCase):
    """Ledgers every station must see go to the database; what describes this
    host (a lane runner's heartbeat, this disk's record) stays a file."""

    def setUp(self):
        self.db = FakeDb()
        patch = connected(self.db)
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.store = state_store.RoutedStore(
            self.home, state_postgres.PostgresStore(self.home, FakeDbapi), state_store.FileStore())

    def path(self, *parts):
        return os.path.join(self.home, "state", *parts)

    def test_a_shared_ledger_goes_to_the_database_and_not_to_disk(self):
        self.store.write_json(self.path("station", "reports.json"), {"n": 1})
        self.assertIn("station/reports.json", self.db.state)
        self.assertFalse(os.path.exists(self.path("station", "reports.json")))

    def test_a_lane_heartbeat_the_runner_wrote_as_a_file_is_read_from_disk(self):
        os.makedirs(self.path("usage"))
        with open(self.path("usage", "codex.json"), "w", encoding="utf-8") as fh:
            json.dump({"available": True}, fh)
        self.assertEqual(self.store.read_json(self.path("usage", "codex.json"), None),
                         {"available": True})
        self.assertEqual(self.db.state, {})

    def test_this_hosts_own_records_stay_files(self):
        for name in (("station", "disk.json"), ("station", "deployed.json"),
                     ("station", "pass-deadline.json"), ("station", "etags", "a.json"),
                     ("brain.json",)):
            with self.subTest(name=name):
                self.store.write_json(self.path(*name), {"n": 1})
                self.assertTrue(os.path.exists(self.path(*name)))
        self.assertEqual(self.db.state, {})

    def test_the_fence_reaches_the_shared_store_only(self):
        self.store.fence(7)
        self.assertEqual(self.store.shared.epoch, 7)


class FencedWriteTest(unittest.TestCase):
    """The write is one conditional statement, so a stale holder is refused
    by the database even if it passed its own lease check a moment before."""

    def setUp(self):
        self.db = FakeDb()
        patch = connected(self.db)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_stale_epoch_writes_nothing(self):
        cell = state_postgres.PostgresCell(FakeDbapi)
        stale = station_lease.StationLease(cell, "alpha", 90)
        stale.acquire()
        store = state_postgres.PostgresStore(HOME, FakeDbapi)
        store.fence(stale.epoch)
        store.write_json(os.path.join(HOME, "state", "ledger.json"), {"by": "alpha"})
        self.db.now += 91
        self.assertTrue(station_lease.StationLease(cell, "beta", 90).acquire())
        with self.assertRaises(station_lease.LeaseLost):
            store.write_json(os.path.join(HOME, "state", "ledger.json"), {"by": "stale alpha"})
        self.assertEqual(json.loads(self.db.state["ledger.json"][0]), {"by": "alpha"})

    def test_the_fenced_store_fences_the_postgres_store_it_wraps(self):
        cell = state_postgres.PostgresCell(FakeDbapi)
        lease = station_lease.StationLease(cell, "alpha", 90)
        lease.acquire()
        inner = state_postgres.PostgresStore(HOME, FakeDbapi)
        station_lease.FencedStore(inner, lease)
        self.assertEqual(inner.epoch, lease.epoch)


class SelectTest(unittest.TestCase):
    def test_the_backend_is_files_unless_postgres_is_named(self):
        self.assertIsInstance(state_store.select({}), state_store.FileStore)

    def test_postgres_is_chosen_by_the_setting_and_needs_its_database_settings(self):
        with self.assertRaises(ValueError) as caught:
            state_store.select({"LOOM_STATE_BACKEND": "postgres"}, home=HOME)
        self.assertIn("LOOM_DB_", str(caught.exception))

    def test_postgres_with_its_settings_and_driver_gives_the_store(self):
        env = {"LOOM_STATE_BACKEND": "postgres", "LOOM_DB_HOST": "db.example.invalid",
               "LOOM_DB_NAME": "loom", "LOOM_DB_USER": "station", "LOOM_DB_PASSWORD": "x"}
        with mock.patch.object(state_postgres, "driver", lambda: FakeDbapi):
            store = state_store.select(env, home=HOME)
        self.assertIsInstance(store, state_store.RoutedStore)
        self.assertIsInstance(store.shared, state_postgres.PostgresStore)
        self.assertIsInstance(store.local, state_store.FileStore)

    def test_a_missing_driver_is_refused_not_ignored(self):
        env = {"LOOM_STATE_BACKEND": "postgres", "LOOM_DB_HOST": "h", "LOOM_DB_NAME": "n",
               "LOOM_DB_USER": "u", "LOOM_DB_PASSWORD": "p"}
        with mock.patch.object(state_postgres, "driver", lambda: None):
            with self.assertRaises(ValueError):
                state_store.select(env, home=HOME)

    def test_the_lease_cell_follows_the_backend(self):
        env = {"LOOM_STATION_LEASE": "1", "LOOM_STATION_ID": "alpha",
               "LOOM_STATE_BACKEND": "postgres", "LOOM_DB_HOST": "h", "LOOM_DB_NAME": "n",
               "LOOM_DB_USER": "u", "LOOM_DB_PASSWORD": "p"}
        with mock.patch.object(state_postgres, "driver", lambda: FakeDbapi):
            lease = station_lease.select(env, HOME)
        self.assertIsInstance(lease.cell, state_postgres.PostgresCell)


class RunTest(unittest.TestCase):
    """Two stations, one database: the second stands down; when the first
    stops renewing, the second runs, and the first is fenced out."""

    def setUp(self):
        self.db = FakeDb()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous = state_store.active()
        self.addCleanup(state_store.use, previous)
        self.ran = []

        def fake_pass(this):
            station._write_json(os.path.join(this.s.home, "state", "station", "reports.json"),
                                {"by": this.s.home})
            self.ran.append(this.s.home)

        patches = [connected(self.db),
                   mock.patch.object(state_postgres, "driver", lambda: FakeDbapi),
                   mock.patch.object(station.Station, "run_pass", fake_pass),
                   mock.patch.object(station, "_trust_app_login", lambda settings, gh: None),
                   mock.patch.object(station.Station, "sample_operator_budget", lambda this: None),
                   mock.patch.object(station.Station, "end_pass", lambda this: None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    class FakeGitHub:
        cache_dir = None

    def run_station(self, name):
        # Each station has its own home (its own disk) and the same database.
        home = os.path.join(self.tmp.name, name)
        settings = station.Settings(home, ["example-owner/example-repo"], [], [])
        env = {"LOOM_STATE_BACKEND": "postgres", "LOOM_STATION_LEASE": "1", "LOOM_STATION_ID": name,
               "LOOM_DB_HOST": "h", "LOOM_DB_NAME": "n", "LOOM_DB_USER": "u", "LOOM_DB_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env):
            return station.run(settings, self.FakeGitHub()), home

    def test_the_second_station_stands_down_then_takes_over_when_the_first_stops_renewing(self):
        code, alpha_home = self.run_station("alpha")
        self.assertEqual(code, 0)
        code, beta_home = self.run_station("beta")      # alpha's lease is live
        self.assertEqual(code, 0)
        self.assertEqual(self.ran, [alpha_home])
        self.db.now += 181                              # alpha's host went to sleep
        code, beta_home = self.run_station("beta")
        self.assertEqual(code, 0)
        self.assertEqual(self.ran, [alpha_home, beta_home])
        # The ledger is the shared one: beta's write is what it holds.
        self.assertEqual(json.loads(self.db.state["station/reports.json"][0]), {"by": beta_home})
        # Nothing was written to either station's disk for that ledger.
        self.assertFalse(os.path.exists(os.path.join(beta_home, "state", "station", "reports.json")))

    def test_a_station_asked_for_the_database_without_its_settings_stops_and_uses_no_files(self):
        home = os.path.join(self.tmp.name, "gamma")
        settings = station.Settings(home, ["example-owner/example-repo"], [], [])
        env = {"LOOM_STATE_BACKEND": "postgres", "LOOM_DB_HOST": "", "LOOM_DB_NAME": "",
               "LOOM_DB_USER": "", "LOOM_DB_PASSWORD": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(station.run(settings, self.FakeGitHub()), 1)
        self.assertEqual(self.ran, [])
        self.assertFalse(os.path.exists(os.path.join(home, "state", "station.lock")))


if __name__ == "__main__":
    unittest.main()
