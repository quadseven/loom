"""One station is active at a time: a lease with a fencing epoch (loom#3050).

A station takes the lease before it runs a pass, renews it while it works,
and loses it after a missed heartbeat, when a peer takes it over with a
higher epoch. A woken stale station is fenced out: its renewal and its state
writes fail instead of overwriting what the new holder owns."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import state_store, station, station_lease  # noqa: E402

TTL = 90


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class LeaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    def cell(self):
        return station_lease.FileCell(os.path.join(self.tmp.name, "station.lease.json"),
                                      clock=self.clock)

    def lease(self, holder):
        return station_lease.StationLease(self.cell(), holder, TTL)


class AcquireTest(LeaseTestCase):
    def test_the_first_station_takes_the_lease_at_epoch_one(self):
        lease = self.lease("alpha")
        self.assertTrue(lease.acquire())
        self.assertEqual(lease.epoch, 1)

    def test_a_peer_stands_down_while_the_lease_is_live(self):
        self.assertTrue(self.lease("alpha").acquire())
        self.clock.now += TTL - 1
        peer = self.lease("beta")
        self.assertFalse(peer.acquire())
        self.assertIsNone(peer.epoch)

    def test_a_peer_takes_over_after_a_missed_heartbeat_with_a_higher_epoch(self):
        self.assertTrue(self.lease("alpha").acquire())
        self.clock.now += TTL + 1
        peer = self.lease("beta")
        self.assertTrue(peer.acquire())
        self.assertEqual(peer.epoch, 2)

    def test_a_renewed_lease_holds_past_its_first_expiry(self):
        alpha = self.lease("alpha")
        alpha.acquire()
        self.clock.now += TTL - 10
        alpha.renew()
        self.clock.now += TTL - 10
        self.assertFalse(self.lease("beta").acquire())

    def test_the_same_station_starting_a_new_pass_gets_a_new_epoch(self):
        first = self.lease("alpha")
        first.acquire()
        again = self.lease("alpha")
        self.assertTrue(again.acquire())
        self.assertEqual(again.epoch, 2)
        with self.assertRaises(station_lease.LeaseLost):
            first.check()

    def test_two_stations_racing_for_a_free_lease_have_one_winner(self):
        results = []
        barrier = threading.Barrier(8)

        def race(name):
            lease = self.lease(name)
            barrier.wait()
            results.append(lease.acquire())

        threads = [threading.Thread(target=race, args=("s%d" % n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)


class FencingTest(LeaseTestCase):
    def test_a_woken_stale_holder_cannot_renew_or_check(self):
        stale = self.lease("alpha")
        stale.acquire()
        self.clock.now += TTL + 1            # alpha slept through its heartbeats
        self.assertTrue(self.lease("beta").acquire())
        with self.assertRaises(station_lease.LeaseLost):
            stale.renew()
        with self.assertRaises(station_lease.LeaseLost):
            stale.check()

    def test_a_lost_lease_stops_a_pass_wherever_a_state_error_does(self):
        self.assertTrue(issubclass(station_lease.LeaseLost, state_store.StateError))

    def test_a_stale_holder_writes_nothing_to_the_store(self):
        inner = RecordingStore()
        stale = self.lease("alpha")
        stale.acquire()
        fenced_stale = station_lease.FencedStore(inner, stale, clock=self.clock)
        fenced_stale.write_json("ledger.json", {"by": "alpha", "n": 1})
        self.clock.now += TTL + 1
        fresh = self.lease("beta")
        fresh.acquire()
        station_lease.FencedStore(inner, fresh, clock=self.clock).write_json(
            "ledger.json", {"by": "beta", "n": 2})
        with self.assertRaises(station_lease.LeaseLost):
            fenced_stale.write_json("ledger.json", {"by": "alpha", "n": 3})
        self.assertEqual(inner.data["ledger.json"], {"by": "beta", "n": 2})

    def test_reads_pass_through_and_writes_renew_the_lease(self):
        inner = RecordingStore()
        lease = self.lease("alpha")
        lease.acquire()
        fenced = station_lease.FencedStore(inner, lease, clock=self.clock)
        self.assertEqual(fenced.read_json("none.json", "d"), "d")
        self.clock.now += TTL - 5            # close to expiry, then a write
        fenced.write_json("a.json", {"n": 1})
        self.clock.now += TTL - 5
        self.assertFalse(self.lease("beta").acquire())


class RecordingStore(state_store.StateStore):
    def __init__(self):
        self.data = {}

    def read_json(self, path, default, strict=True):
        return self.data.get(path, default)

    def write_json(self, path, data):
        self.data[path] = data


class SelectCellTest(unittest.TestCase):
    def test_the_lease_is_off_by_default(self):
        self.assertIsNone(station_lease.select({}, "/unused", "alpha"))

    def test_on_gives_a_file_lease_under_the_state_directory(self):
        lease = station_lease.select({"LOOM_STATION_LEASE": "1", "LOOM_STATION_ID": "alpha",
                                      "LOOM_STATION_LEASE_TTL": "120"}, "home", "ignored")
        self.assertEqual(lease.holder, "alpha")
        self.assertEqual(lease.ttl, 120)
        self.assertEqual(lease.cell.path, os.path.join("home", "state", "station.lease.json"))

    def test_a_bad_ttl_or_station_name_is_refused(self):
        with self.assertRaises(ValueError):
            station_lease.select({"LOOM_STATION_LEASE": "1", "LOOM_STATION_LEASE_TTL": "soon"}, "h", "a")
        with self.assertRaises(ValueError):
            station_lease.select({"LOOM_STATION_LEASE": "1", "LOOM_STATION_ID": "a b"}, "h", "a")


class RunTest(unittest.TestCase):
    """station.run() with the lease on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.settings = station.Settings(self.home, ["example-owner/example-repo"], [], [])
        self.seen = []
        previous = state_store.active()
        self.addCleanup(state_store.use, previous)

        def run_pass(this):
            self.seen.append(state_store.active())

        patches = [mock.patch.object(station.Station, "run_pass", run_pass),
                   mock.patch.object(station, "_trust_app_login", lambda settings, gh: None),
                   mock.patch.object(station.Station, "sample_operator_budget", lambda this: None),
                   mock.patch.object(station.Station, "end_pass", lambda this: None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    class FakeGitHub:
        cache_dir = None

    def run_station(self, **env):
        environ = {"LOOM_STATION_LEASE": "1", "LOOM_STATION_ID": "alpha", **env}
        with mock.patch.dict(os.environ, environ, clear=False):
            return station.run(self.settings, self.FakeGitHub())

    def test_a_pass_runs_under_the_lease_with_fenced_writes(self):
        self.assertEqual(self.run_station(), 0)
        [store] = self.seen
        self.assertIsInstance(store, station_lease.FencedStore)

    def test_a_second_station_stands_down_while_the_lease_is_held(self):
        cell = station_lease.FileCell(os.path.join(self.home, "state", "station.lease.json"))
        self.assertTrue(station_lease.StationLease(cell, "beta", TTL).acquire())
        self.assertEqual(self.run_station(), 0)
        self.assertEqual(self.seen, [])

    def test_a_pass_that_loses_its_lease_stops_with_a_logged_reason(self):
        def lose(this):
            raise station_lease.LeaseLost("lease taken by beta at epoch 2")

        with mock.patch.object(station.Station, "run_pass", lose):
            self.assertEqual(self.run_station(), 1)

    def test_with_the_lease_off_nothing_changes(self):
        self.assertEqual(self.run_station(LOOM_STATION_LEASE="0"), 0)
        [store] = self.seen
        self.assertIsInstance(store, state_store.FileStore)
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "station.lease.json")))


if __name__ == "__main__":
    unittest.main()
