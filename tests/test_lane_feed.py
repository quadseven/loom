"""bus/lane_feed.py: lane pods on other nodes fed through the shared state
store (loom#3621). The station offers a slot's next brief as a row in the
store; a lane pod elsewhere claims it into its own loom home, runs it with the
usual lanes/run_lane.sh, and pushes its heartbeat and its report back as rows.
The station mirrors those rows into its own loom home where a local runner
writes, so the slot reads as free, busy, limited or silent exactly like a
local one.

The two sides run here against two separate loom homes. The store is either
the file backend rooted at the station's home or the Postgres backend on an
in-memory stand-in for one database both sides reach; nothing connects
anywhere. A fake lane run plays lanes/run_lane.sh in the pod's home."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from bus import factory, lane_feed, state_postgres, state_store, station, station_lease, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests.test_bus_station import Base, REPO  # noqa: E402
from tests.test_state_postgres import FakeDb, FakeDbapi, connected  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

SLOT = "zen"
NUMBER = 7


def brief_text(repo=REPO, number=NUMBER):
    return "Repo: %s\nIssue: %d\nBase: main\n\n## What\nDo the thing.\n" % (repo, number)


class FeedBase(Base):
    """A station home (Base.home) and a lane pod's home, one slot fed."""

    def setUp(self):
        super().setUp()
        self.pod_home = os.path.join(self._tmp.name, "pod")
        self.env = {lane_feed.SLOTS_SETTING: SLOT}
        self.said = []
        self.store = self.make_store()
        self.ticket = tickets.ticket_name(REPO, NUMBER)

    def make_store(self):
        return state_store.FileStore()

    # -- the station side --------------------------------------------------
    def tend(self, now=None):
        lane_feed.Feed(self.home, self.env, self.store, self.said.append,
                       now=time.time() if now is None else now).tend()

    def station(self):
        return station.Station(GitHub("token", transport=self.fake), self.settings,
                               now=time.time())

    def queue(self, number=NUMBER):
        name = tickets.ticket_name(REPO, number)
        self.write(self.inbox(SLOT, name + ".md"), brief_text(number=number))
        return name

    def row(self, name):
        return self.store.read_json(lane_feed.row_path(self.home, SLOT, name), None)

    # -- the pod side ------------------------------------------------------
    def sync(self, now=None):
        lane_feed.sync(self.pod_home, [SLOT], self.store, store_home=self.home,
                       pod="pod-a", now=time.time() if now is None else now)

    def pod_path(self, *parts):
        return os.path.join(self.pod_home, *parts)

    def pod_beat(self, at, alive=None, available=True):
        os.makedirs(self.pod_path("state", "usage"), exist_ok=True)
        with open(self.pod_path("state", "usage", SLOT + ".json"), "w") as fh:
            json.dump({"lane": SLOT, "at": int(at), "available": available,
                       "reason": "" if available else "usage limit"}, fh)
        if alive is not None:
            with open(self.pod_path("state", "usage", SLOT + ".alive"), "w") as fh:
                fh.write("%d\n" % int(alive))

    def pod_finish(self, name, kind="done"):
        """What run_lane.sh leaves when a run ends: its log and the brief
        moved to done/ (or failed/)."""
        log = self.pod_path("logs", SLOT, name + ".log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w") as fh:
            fh.write("[t] %s done: https://github.com/%s/pull/9\n" % (name, REPO)
                     if kind == "done" else "[t] %s failed: [work] the check failed\n" % name)
        os.makedirs(self.pod_path("briefs", SLOT, kind), exist_ok=True)
        os.replace(self.pod_path("briefs", SLOT, name + ".md"),
                   self.pod_path("briefs", SLOT, kind, name + ".md"))


class RemoteLaneClaimsBeatsFinishesTest(FeedBase):
    def test_a_remote_lane_claims_the_brief_beats_while_it_runs_and_its_outcome_lands(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        offer = self.row("offer.json")
        self.assertEqual((offer["ticket"], offer["state"]), (name, "offered"))

        # (a) The pod gets its next claim: the brief, in its own inbox.
        self.sync(now + 1)
        with open(self.pod_path("briefs", SLOT, name + ".md")) as fh:
            self.assertEqual(fh.read(), brief_text())
        self.assertEqual(self.row("claim.json")["id"], offer["id"])
        # A second round claims nothing more.
        self.sync(now + 2)
        self.assertEqual(sorted(n for n in os.listdir(self.pod_path("briefs", SLOT))
                                if n.endswith(".md")), [name + ".md"])

        # (b) Busy: the pod's heartbeat and keepalive stamp reach the station
        # home, where the station reads them as it reads a local runner's.
        self.pod_beat(at=now - 40 * 60, alive=now)
        self.sync(now + 3)
        self.tend(now + 4)
        with open(self.inbox(SLOT, name + ".md")) as fh:
            self.assertIn("Do the thing", fh.read())
        seen = self.station()
        self.assertTrue(seen._heartbeat_ok(SLOT) or seen._keepalive_fresh(SLOT))
        self.assertNotIn(SLOT, seen._silent_lanes())

        # (b) The outcome: the run's log and the brief's move land where a
        # local runner leaves them, and the offer is done.
        self.pod_finish(name)
        self.pod_beat(at=now + 5)
        self.sync(now + 6)
        self.tend(now + 7)
        self.assertTrue(os.path.exists(self.inbox(SLOT, "done", name + ".md")))
        self.assertFalse(os.path.exists(self.inbox(SLOT, name + ".md")))
        with open(os.path.join(self.home, "logs", SLOT, name + ".log")) as fh:
            self.assertIn("done: https://github.com/", fh.read())
        self.assertEqual(self.row("offer.json")["state"], "done")
        # Free: a fresh available heartbeat and no brief waiting.
        seen = self.station()
        self.assertTrue(seen._heartbeat_ok(SLOT))
        self.assertTrue(seen._inbox_empty(SLOT))

    def test_a_failed_run_lands_in_failed_with_its_reason(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        self.sync(now + 1)
        self.pod_finish(name, kind="failed")
        self.sync(now + 2)
        self.tend(now + 3)
        self.assertTrue(os.path.exists(self.inbox(SLOT, "failed", name + ".md")))
        with open(os.path.join(self.home, "logs", SLOT, name + ".log")) as fh:
            self.assertIn("failed: [work]", fh.read())

    def test_one_brief_at_a_time_and_the_next_is_offered_after_the_outcome(self):
        first = self.queue(NUMBER)
        second = self.queue(NUMBER + 1)
        now = time.time()
        self.tend(now)
        self.tend(now + 1)
        self.assertEqual(self.row("offer.json")["ticket"], first)
        self.sync(now + 2)
        self.pod_finish(first)
        self.sync(now + 3)
        self.tend(now + 4)
        self.assertEqual(self.row("offer.json")["ticket"], second)
        self.assertEqual(self.row("offer.json")["state"], "offered")

    def test_a_limited_remote_lane_reads_as_limited(self):
        self.pod_beat(at=time.time(), available=False)
        self.sync()
        self.tend()
        self.assertFalse(self.station()._heartbeat_ok(SLOT))

    def test_a_run_that_ends_without_moving_its_brief_is_parked_transient(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        self.sync(now + 1)
        # A report with no move of the brief: the run ended without one.
        report = {"id": self.row("offer.json")["id"], "ticket": name, "files": {}}
        self.store.write_json(lane_feed.row_path(self.home, SLOT, "report.json"), report)
        self.tend(now + 2)
        self.assertTrue(os.path.exists(self.inbox(SLOT, "failed", name + ".md")))
        with open(os.path.join(self.home, "logs", SLOT, name + ".log")) as fh:
            self.assertIn("[transient]", fh.read())

    def test_a_report_naming_a_file_outside_its_run_is_refused_and_parked(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        self.sync(now + 1)
        report = {"id": self.row("offer.json")["id"], "ticket": name,
                  "files": {"state/brain.json": lane_feed.encode(b"{}")}}
        self.store.write_json(lane_feed.row_path(self.home, SLOT, "report.json"), report)
        self.tend(now + 2)
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "brain.json")))
        self.assertTrue(os.path.exists(self.inbox(SLOT, "failed", name + ".md")))


class RemoteLaneGoesSilentTest(FeedBase):
    def test_a_silent_remote_lane_has_its_ticket_moved_like_a_silent_local_lane(self):
        name = self.queue()
        now = time.time()
        self.tend(now - 45 * 60)
        self.sync(now - 44 * 60)
        # The pod beat when it took the brief and stamped its keepalive for a
        # few minutes, then died: no more rows.
        self.pod_beat(at=now - 44 * 60, alive=now - 40 * 60)
        self.sync(now - 40 * 60)
        self.tend(now)
        silent = self.station()._silent_lanes()
        self.assertIn(SLOT, silent)
        self.assertGreaterEqual(silent[SLOT], factory.SILENT_MAX)
        self.assertNotIn(SLOT, factory.REMOTE_LANES)

        # The factory moves the running ticket off the silent lane and the
        # station returns its brief from the inbox, as for a local lane.
        found = {SLOT: [(REPO, NUMBER)]}
        self.station()._return_silent_briefs(found)
        self.assertFalse(os.path.exists(self.inbox(SLOT, name + ".md")))
        # The next pass withdraws the offer, so no pod takes it again ...
        self.tend(now + 1)
        self.assertEqual(self.row("offer.json")["state"], "withdrawn")
        # ... and a late report from the revived pod writes nothing.
        self.pod_finish(name)
        self.sync(now + 2)
        self.tend(now + 3)
        self.assertFalse(os.path.exists(self.inbox(SLOT, "done", name + ".md")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "logs", SLOT, name + ".log")))

    def test_a_withdrawn_brief_the_pod_has_not_started_leaves_its_inbox(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        self.sync(now + 1)
        os.remove(self.inbox(SLOT, name + ".md"))
        self.tend(now + 2)
        self.sync(now + 3)
        self.assertFalse(os.path.exists(self.pod_path("briefs", SLOT, name + ".md")))
        self.assertTrue(os.path.exists(self.pod_path("briefs", SLOT, "returned", name + ".md")))


class NoRemoteLaneTest(FeedBase):
    def test_with_no_fed_slot_nothing_is_offered_or_written(self):
        self.env = {}
        name = self.queue()
        self.tend()
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "lane-feed")))
        self.assertTrue(os.path.exists(self.inbox(SLOT, name + ".md")))
        self.assertEqual(lane_feed.feed_slots({}), [])

    def test_a_slot_that_is_also_a_runner_slot_or_not_a_lane_is_refused(self):
        with self.assertRaises(ValueError):
            lane_feed.feed_slots({lane_feed.SLOTS_SETTING: "zen",
                                  "LOOM_RUNNER_SLOTS": "zen"})
        with self.assertRaises(ValueError):
            lane_feed.feed_slots({lane_feed.SLOTS_SETTING: "not-a-lane"})

    def test_the_station_pass_offers_through_its_active_store(self):
        name = self.queue()
        with mock.patch.dict(os.environ, {lane_feed.SLOTS_SETTING: SLOT}):
            self.station().tend_lane_feed()
        self.assertEqual(self.row("offer.json")["ticket"], name)

    def test_a_bad_setting_is_logged_and_the_pass_goes_on(self):
        with mock.patch.dict(os.environ, {lane_feed.SLOTS_SETTING: "not-a-lane"}), \
                mock.patch.object(station, "_say") as said:
            self.station().tend_lane_feed()
        self.assertIn("not a lane slot", " ".join(str(c) for c in said.call_args_list))

    def test_an_unreadable_offer_is_logged_but_a_lost_lease_stops_the_pass(self):
        path = lane_feed.row_path(self.home, SLOT, "offer.json")
        self.write(path, "{not json")
        with mock.patch.dict(os.environ, {lane_feed.SLOTS_SETTING: SLOT}), \
                mock.patch.object(station, "_say") as said:
            self.station().tend_lane_feed()
        self.assertIn("unreadable state file", " ".join(str(c) for c in said.call_args_list))
        lost = station_lease.LeaseLost("taken")
        with mock.patch.dict(os.environ, {lane_feed.SLOTS_SETTING: SLOT}), \
                mock.patch.object(lane_feed.Feed, "tend", side_effect=lost):
            with self.assertRaises(station_lease.LeaseLost):
                self.station().tend_lane_feed()

    def test_a_fed_slot_is_not_held_by_the_station_hosts_load(self):
        self.settings.host_load_gate = True
        loaded = mock.patch.object(station.subprocess, "check_output",
                                   return_value="load 40 over 12 cores")
        with loaded, mock.patch.dict(os.environ, {lane_feed.SLOTS_SETTING: SLOT}):
            self.assertTrue(self.station()._host_load_holds("codex"))
            self.assertFalse(self.station()._host_load_holds(SLOT))


class PodCommandTest(FeedBase):
    def test_the_pod_command_claims_through_the_store_its_settings_name(self):
        name = self.queue()
        self.tend()
        env = {"LOOM_HOME": self.pod_home, "LOOM_POD_FEED_HOME": self.home,
               "LOOM_POD_NAME": "pod-b"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("LOOM_STATE_BACKEND", None)
            self.assertEqual(lane_feed.main(["lane_feed", "sync", SLOT]), 0)
        self.assertTrue(os.path.exists(self.pod_path("briefs", SLOT, name + ".md")))
        self.assertEqual(self.row("claim.json")["pod"], "pod-b")

    def test_the_pod_command_refuses_a_bad_slot_or_a_missing_home(self):
        with mock.patch.dict(os.environ, {"LOOM_HOME": self.pod_home}):
            self.assertEqual(lane_feed.main(["lane_feed", "sync", "../x"]), 2)
            self.assertEqual(lane_feed.main(["lane_feed"]), 2)
        with mock.patch.dict(os.environ, {"LOOM_HOME": ""}):
            self.assertEqual(lane_feed.main(["lane_feed", "sync", SLOT]), 2)

    def test_the_pod_command_refuses_postgres_without_its_settings(self):
        env = {"LOOM_HOME": self.pod_home, "LOOM_STATE_BACKEND": "postgres"}
        with mock.patch.dict(os.environ, env):
            for key in state_postgres.SETTINGS:
                os.environ.pop(key, None)
            self.assertEqual(lane_feed.main(["lane_feed", "sync", SLOT]), 1)


class PostgresFeedTest(RemoteLaneClaimsBeatsFinishesTest):
    """The same path with both sides on the shared Postgres backend: the
    station's home and the pod's home differ, and the rows meet in one
    database."""

    def make_store(self):
        self.db = FakeDb()
        patch = connected(self.db)
        patch.start()
        self.addCleanup(patch.stop)
        return state_postgres.PostgresStore(self.home, FakeDbapi)

    def sync(self, now=None):
        pod_store = state_postgres.PostgresStore(self.pod_home, FakeDbapi)
        lane_feed.sync(self.pod_home, [SLOT], pod_store, store_home=self.pod_home,
                       pod="pod-a", now=time.time() if now is None else now)

    def test_the_rows_live_in_the_shared_database(self):
        self.queue()
        self.tend()
        self.sync()
        self.assertIn("lane-feed/%s/offer.json" % SLOT, self.db.state)
        self.assertIn("lane-feed/%s/claim.json" % SLOT, self.db.state)


if __name__ == "__main__":
    unittest.main()
