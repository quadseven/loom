"""bus/lane_feed.py: lane pods on other nodes fed by the station through the
lane_feed table (loom#3621). The station offers a slot's next brief as a row;
the lane pod the setting names for that slot claims it into its own loom
home, runs it with the usual lanes/run_lane.sh, and writes its heartbeat and
report back as rows. The station mirrors those rows into its own loom home
where a local runner writes, so the slot reads as free, busy, limited or
silent exactly like a local one, and believes a row only from that pod.

The two sides run here against two separate loom homes, on the file rows or
on an in-memory stand-in for Postgres that keeps the table's row-level
security rules per connected role; nothing connects anywhere. A fake lane run
plays lanes/run_lane.sh in the pod's home."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from bus import factory, lane_feed, state_store, station, station_lease, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests.test_bus_station import Base, REPO  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

SLOT = "zen"
NUMBER = 7


def brief_text(repo=REPO, number=NUMBER):
    return "Repo: %s\nIssue: %d\nBase: main\n\n## What\nDo the thing.\n" % (repo, number)


# -- an in-memory Postgres with the lane_feed rules ------------------------------
class FakeDbapi:
    class Error(Exception):
        pass


class FeedDb:
    """The lane_feed and lane_feed_grant tables, the station lease row, and
    the table's row-level security as the station's policies state it: the
    owner sees and writes everything; another role reads only the rows of
    slots granted to it and writes only their claim, beat and report rows,
    as itself. Every statement is recorded with its role."""

    OWNER = "loom_station"

    def __init__(self):
        self.rows = {}         # (slot, name) -> (value json text, writer)
        self.grants = {}       # slot -> principal
        self.epoch = None
        self.setup_done = False
        self.sent = []         # (role, sql)

    def connect(self, role):
        return FakeConnection(self, role)

    def refuse(self, why):
        raise FakeDbapi.Error(why)

    def execute(self, role, sql, params):
        self.sent.append((role, sql))
        owner = role == self.OWNER
        if sql == lane_feed.STATEMENT_TIMEOUT:
            return [], 0
        if sql in lane_feed.SETUP:
            if not owner:
                self.refuse("must be owner of table lane_feed")
            self.setup_done = True
            return [], 0
        if sql == lane_feed.GET_ROW:
            slot, name = params
            if not owner and self.grants.get(slot) != role:
                return [], 0
            row = self.rows.get((slot, name))
            return ([row] if row else []), 0
        if sql == lane_feed.PUT_ROW:
            slot, name, value, _ = params
            if not owner and (name not in lane_feed.POD_ROWS or self.grants.get(slot) != role):
                self.refuse("new row violates row-level security policy for table lane_feed")
            self.rows[(slot, name)] = (value, role)
            return [], 1
        if sql == lane_feed.PUT_ROW_FENCED:
            slot, name, value, _, epoch = params
            if not owner:
                self.refuse("permission denied for table station_lease")
            if self.epoch != epoch:
                return [], 0
            self.rows[(slot, name)] = (value, role)
            return [], 1
        if sql == lane_feed.PUT_GRANT:
            if not owner:
                self.refuse("permission denied for table lane_feed_grant")
            self.grants[params[0]] = params[1]
            return [], 1
        if sql == lane_feed.DROP_GRANTS:
            if not owner:
                self.refuse("permission denied for table lane_feed_grant")
            self.grants = {s: p for s, p in self.grants.items() if s in params[0]}
            return [], 0
        if sql == lane_feed.DROP_ALL_GRANTS:
            if not owner:
                self.refuse("permission denied for table lane_feed_grant")
            self.grants = {}
            return [], 0
        self.refuse("permission denied: %s" % sql[:40])


class FakeCursor:
    def __init__(self, db, role):
        self.db, self.role = db, role
        self.rows, self.rowcount = [], -1

    def execute(self, sql, params=()):
        self.rows, self.rowcount = self.db.execute(self.role, sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, db, role):
        self.db, self.role = db, role

    def cursor(self):
        return FakeCursor(self.db, self.role)

    def commit(self):
        pass

    def close(self):
        pass


class FakeLease:
    def __init__(self, epoch=3, lost=False):
        self.epoch = epoch
        self.lost = lost

    def check(self):
        if self.lost:
            raise station_lease.LeaseLost("taken by a peer")


# -- the two sides ---------------------------------------------------------------
class FeedBase(Base):
    """A station home (Base.home) and a lane pod's home, one slot fed to POD."""

    POD = "pod-a"

    def setUp(self):
        super().setUp()
        self.pod_home = os.path.join(self._tmp.name, "pod")
        self.env = {lane_feed.SLOTS_SETTING: "%s=%s" % (SLOT, self.POD)}
        self.said = []
        self.station_rows = self.make_station_rows()
        self.lease = None

    def make_station_rows(self):
        return lane_feed.FileRows(self.home, "station")

    def pod_rows(self, principal=None):
        return lane_feed.FileRows(self.home, principal or self.POD)

    # -- the station side --------------------------------------------------
    def tend(self, now=None):
        lane_feed.Feed(self.home, self.env, self.station_rows, self.said.append,
                       now=time.time() if now is None else now, lease=self.lease).tend()

    def station(self):
        return station.Station(GitHub("token", transport=self.fake), self.settings,
                               now=time.time())

    def queue(self, number=NUMBER):
        name = tickets.ticket_name(REPO, number)
        self.write(self.inbox(SLOT, name + ".md"), brief_text(number=number))
        return name

    def row(self, name):
        found = self.station_rows.read(SLOT, name)
        return found[0] if found else None

    # -- the pod side ------------------------------------------------------
    def sync(self, now=None, principal=None):
        principal = principal or self.POD
        lane_feed.sync(self.pod_home, [SLOT], self.pod_rows(principal), principal,
                       now=time.time() if now is None else now)

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
        offer = self.row("offer")
        self.assertEqual((offer["ticket"], offer["state"], offer["pod"]),
                         (name, "offered", self.POD))

        # (a) The pod gets its next claim: the brief, in its own inbox.
        self.sync(now + 1)
        with open(self.pod_path("briefs", SLOT, name + ".md")) as fh:
            self.assertEqual(fh.read(), brief_text())
        self.assertEqual(self.row("claim")["id"], offer["id"])
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
        self.assertTrue(seen._keepalive_fresh(SLOT))
        self.assertNotIn(SLOT, seen._silent_lanes())

        # (b) The outcome: the run's log and the brief's move land where a
        # local runner leaves them, and the offer is done.
        self.pod_finish(name)
        self.pod_beat(at=now + 5)
        os.remove(self.pod_path("state", "usage", SLOT + ".alive"))
        self.sync(now + 6)
        self.tend(now + 7)
        self.assertTrue(os.path.exists(self.inbox(SLOT, "done", name + ".md")))
        self.assertFalse(os.path.exists(self.inbox(SLOT, name + ".md")))
        with open(os.path.join(self.home, "logs", SLOT, name + ".log")) as fh:
            self.assertIn("done: https://github.com/", fh.read())
        self.assertEqual(self.row("offer")["state"], "done")
        # Free: a fresh available heartbeat, no keepalive and no brief waiting.
        seen = self.station()
        self.assertTrue(seen._heartbeat_ok(SLOT))
        self.assertFalse(seen._keepalive_fresh(SLOT))
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
        self.assertEqual(self.row("offer")["ticket"], first)
        self.sync(now + 2)
        self.pod_finish(first)
        self.sync(now + 3)
        self.tend(now + 4)
        self.assertEqual(self.row("offer")["ticket"], second)
        self.assertEqual(self.row("offer")["state"], "offered")

    def test_a_limited_remote_lane_reads_as_limited(self):
        self.tend()
        self.pod_beat(at=time.time(), available=False)
        self.sync()
        self.tend()
        self.assertFalse(self.station()._heartbeat_ok(SLOT))

    def test_a_run_that_ends_without_moving_its_brief_is_parked_transient(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        self.sync(now + 1)
        report = {"id": self.row("offer")["id"], "ticket": name, "files": {}}
        self.pod_rows().write(SLOT, "report", report)
        self.tend(now + 2)
        self.assertTrue(os.path.exists(self.inbox(SLOT, "failed", name + ".md")))
        with open(os.path.join(self.home, "logs", SLOT, name + ".log")) as fh:
            self.assertIn("[transient]", fh.read())

    def test_a_report_naming_a_file_outside_its_run_is_refused_and_parked(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        self.sync(now + 1)
        report = {"id": self.row("offer")["id"], "ticket": name,
                  "files": {"state/brain.json": lane_feed.encode(b"{}")}}
        self.pod_rows().write(SLOT, "report", report)
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
        self.station()._return_silent_briefs({SLOT: [(REPO, NUMBER)]})
        self.assertFalse(os.path.exists(self.inbox(SLOT, name + ".md")))
        # The next pass withdraws the offer, so no pod takes it again ...
        self.tend(now + 1)
        self.assertEqual(self.row("offer")["state"], "withdrawn")
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


class OnlyTheSlotsPodIsBelievedTest(FeedBase):
    """The station believes a row only from the pod its setting names for
    the slot, and applies a report only when that pod also claimed the offer."""

    def test_a_report_from_another_pod_is_ignored(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        self.sync(now + 1)
        report = {"id": self.row("offer")["id"], "ticket": name,
                  "files": {"briefs/%s/done/%s.md" % (SLOT, name): lane_feed.encode(b"x")}}
        self.pod_rows("pod-b").write(SLOT, "report", report)
        self.tend(now + 2)
        self.assertTrue(os.path.exists(self.inbox(SLOT, name + ".md")))
        self.assertEqual(self.row("offer")["state"], "offered")
        self.assertTrue(any("written by pod-b" in line for line in self.said))

    def test_a_report_whose_claim_another_pod_wrote_is_ignored(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        offer = self.row("offer")
        self.pod_rows("pod-b").write(SLOT, "claim", {"id": offer["id"], "ticket": name})
        report = {"id": offer["id"], "ticket": name,
                  "files": {"briefs/%s/done/%s.md" % (SLOT, name): lane_feed.encode(b"x")}}
        self.pod_rows().write(SLOT, "report", report)
        self.tend(now + 2)
        self.assertTrue(os.path.exists(self.inbox(SLOT, name + ".md")))
        self.assertEqual(self.row("offer")["state"], "offered")

    def test_a_heartbeat_from_another_pod_is_not_mirrored(self):
        self.pod_rows("pod-b").write(SLOT, "beat", {"beat": {"at": time.time(), "available": True},
                                                    "alive": time.time()})
        self.tend()
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "usage", SLOT + ".json")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "usage", SLOT + ".alive")))

    def test_a_pod_takes_no_brief_offered_to_another_pod(self):
        name = self.queue()
        self.tend()
        self.sync(principal="pod-b")
        self.assertFalse(os.path.exists(self.pod_path("briefs", SLOT, name + ".md")))

    def test_moving_the_slot_to_another_pod_withdraws_the_offer(self):
        self.queue()
        now = time.time()
        self.tend(now)
        first = self.row("offer")["id"]
        self.env = {lane_feed.SLOTS_SETTING: "%s=pod-b" % SLOT}
        self.tend(now + 1)
        offer = self.row("offer")
        self.assertNotEqual(offer["id"], first)
        self.assertEqual((offer["pod"], offer["state"]), ("pod-b", "offered"))


class LeaseTest(FeedBase):
    def test_a_station_that_lost_its_lease_offers_nothing(self):
        self.queue()
        self.lease = FakeLease(lost=True)
        with self.assertRaises(station_lease.LeaseLost):
            self.tend()
        self.assertIsNone(self.row("offer"))


class NoRemoteLaneTest(FeedBase):
    def test_with_no_fed_slot_nothing_is_offered_or_written(self):
        self.env = {}
        name = self.queue()
        self.tend()
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "lane-feed")))
        self.assertTrue(os.path.exists(self.inbox(SLOT, name + ".md")))
        self.assertEqual(lane_feed.feed_slots({}), {})

    def test_bad_settings_are_refused(self):
        for raw, extra in (("zen=pod-a", {"LOOM_RUNNER_SLOTS": "zen"}), ("not-a-lane=pod-a", {}),
                           ("zen", {}), ("zen=Bad Role", {}), ("zen=pod-a zen=pod-b", {})):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lane_feed.feed_slots(dict({lane_feed.SLOTS_SETTING: raw}, **extra))

    def test_the_station_pass_offers_through_its_rows(self):
        name = self.queue()
        with mock.patch.dict(os.environ, self.env):
            os.environ.pop("LOOM_STATE_BACKEND", None)
            self.station().tend_lane_feed()
        self.assertEqual(self.row("offer")["ticket"], name)

    def test_a_bad_setting_is_logged_and_the_pass_goes_on(self):
        with mock.patch.dict(os.environ, {lane_feed.SLOTS_SETTING: "not-a-lane=pod-a"}), \
                mock.patch.object(station, "_say") as said:
            self.station().tend_lane_feed()
        self.assertIn("not a lane slot", " ".join(str(c) for c in said.call_args_list))

    def test_an_unreadable_offer_is_logged_but_a_lost_lease_stops_the_pass(self):
        self.write(self.station_rows.path(SLOT, "offer"), "{not json")
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(station, "_say") as said:
            os.environ.pop("LOOM_STATE_BACKEND", None)
            self.station().tend_lane_feed()
        self.assertIn("unreadable state file", " ".join(str(c) for c in said.call_args_list))
        lost = station_lease.LeaseLost("taken")
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(lane_feed.Feed, "tend", side_effect=lost):
            os.environ.pop("LOOM_STATE_BACKEND", None)
            with self.assertRaises(station_lease.LeaseLost):
                self.station().tend_lane_feed()

    def test_a_fed_slot_is_not_held_by_the_station_hosts_load(self):
        self.settings.host_load_gate = True
        loaded = mock.patch.object(station.subprocess, "check_output",
                                   return_value="load 40 over 12 cores")
        with loaded, mock.patch.dict(os.environ, self.env):
            self.assertTrue(self.station()._host_load_holds("codex"))
            self.assertFalse(self.station()._host_load_holds(SLOT))


class PodCommandTest(FeedBase):
    def test_the_pod_command_claims_through_the_rows_its_settings_name(self):
        name = self.queue()
        self.tend()
        env = {"LOOM_HOME": self.pod_home, "LOOM_POD_FEED_HOME": self.home,
               "LOOM_POD_NAME": self.POD}
        with mock.patch.dict(os.environ, env):
            for key in lane_feed.POD_SETTINGS:
                os.environ.pop(key, None)
            self.assertEqual(lane_feed.main(["lane_feed", "sync", SLOT]), 0)
        self.assertTrue(os.path.exists(self.pod_path("briefs", SLOT, name + ".md")))
        self.assertEqual(self.station_rows.read(SLOT, "claim")[1], self.POD)

    def test_the_pod_command_refuses_a_bad_slot_or_a_missing_home(self):
        with mock.patch.dict(os.environ, {"LOOM_HOME": self.pod_home}):
            self.assertEqual(lane_feed.main(["lane_feed", "sync", "../x"]), 2)
            self.assertEqual(lane_feed.main(["lane_feed"]), 2)
        with mock.patch.dict(os.environ, {"LOOM_HOME": ""}):
            self.assertEqual(lane_feed.main(["lane_feed", "sync", SLOT]), 2)

    def test_the_pod_command_needs_its_own_rows(self):
        # Neither a database role of its own nor a shared home: refused,
        # and the station's own settings are never used in their place.
        env = {"LOOM_HOME": self.pod_home, "LOOM_STATE_BACKEND": "postgres",
               "LOOM_DB_HOST": "db.example.com", "LOOM_DB_NAME": "loom",
               "LOOM_DB_USER": "loom_station", "LOOM_DB_PASSWORD": "x"}
        with mock.patch.dict(os.environ, env):
            for key in lane_feed.POD_SETTINGS + ("LOOM_POD_FEED_HOME",):
                os.environ.pop(key, None)
            self.assertEqual(lane_feed.main(["lane_feed", "sync", SLOT]), 1)
        with self.assertRaises(ValueError):
            lane_feed.pod_rows({"LOOM_LANE_DB_HOST": "db.example.com"})

    def test_a_pod_on_the_database_connects_as_its_own_role(self):
        db = FeedDb()
        seen = {}

        class Dbapi(FakeDbapi):
            @staticmethod
            def connect(host, database, user, password, timeout):
                seen.update(host=host, user=user)
                return db.connect(user)
        env = {"LOOM_LANE_DB_HOST": "db.example.com", "LOOM_LANE_DB_NAME": "loom",
               "LOOM_LANE_DB_USER": "lane_pod_a", "LOOM_LANE_DB_PASSWORD": "x",
               "LOOM_DB_USER": "loom_station"}
        rows, principal = lane_feed.pod_rows(env, dbapi=Dbapi)
        self.assertEqual(principal, "lane_pod_a")
        rows.read(SLOT, "offer")
        self.assertEqual(seen["user"], "lane_pod_a")


class PostgresFeedTest(RemoteLaneClaimsBeatsFinishesTest):
    """The same path on the lane_feed table: the station as the table's
    owner, the pod as a role of its own that the row-level security rules
    hold to its own slot's rows."""

    POD = "lane_pod_a"

    def make_station_rows(self):
        self.db = FeedDb()
        return lane_feed.PostgresRows(lambda: self.db.connect(FeedDb.OWNER), FakeDbapi, setup=True)

    def pod_rows(self, principal=None):
        role = principal or self.POD
        return lane_feed.PostgresRows(lambda: self.db.connect(role), FakeDbapi, setup=False)

    def pod_statements(self):
        return {sql for role, sql in self.db.sent if role != FeedDb.OWNER}

    def test_the_station_makes_the_table_and_grants_the_slot_to_its_pod(self):
        self.queue()
        self.tend()
        self.assertTrue(self.db.setup_done)
        self.assertEqual(self.db.grants, {SLOT: self.POD})
        self.env = {lane_feed.SLOTS_SETTING: "spark=%s" % self.POD}
        self.tend()
        self.assertEqual(self.db.grants, {"spark": self.POD})

    def station_pass(self, env):
        """One station tend_lane_feed on the shared backend, its rows on
        this test's database."""
        env = dict(env, LOOM_STATE_BACKEND="postgres")
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(lane_feed, "station_rows", lambda e, h: self.station_rows), \
                mock.patch.object(station, "_say") as said:
            if lane_feed.SLOTS_SETTING not in env:
                os.environ.pop(lane_feed.SLOTS_SETTING, None)
            self.station().tend_lane_feed()
        return " ".join(str(c) for c in said.call_args_list)

    def test_removing_every_slot_revokes_every_grant(self):
        self.station_pass(self.env)
        self.assertEqual(self.db.grants, {SLOT: self.POD})
        self.station_pass({})
        self.assertEqual(self.db.grants, {})
        self.assertIsNone(self.pod_rows().read(SLOT, "offer"))

    def test_a_bad_setting_revokes_every_grant(self):
        self.station_pass(self.env)
        said = self.station_pass({lane_feed.SLOTS_SETTING: "zen=pod-a zen=pod-b"})
        self.assertEqual(self.db.grants, {})
        self.assertIn("no lane pod keeps a grant", said)
        with self.assertRaises(state_store.StateError):
            self.pod_rows().write(SLOT, "beat", {"alive": 1})

    def test_a_pod_sends_only_lane_feed_reads_and_its_own_writes(self):
        name = self.queue()
        now = time.time()
        self.tend(now)
        self.sync(now + 1)
        self.pod_beat(at=now + 1, alive=now + 1)
        self.pod_finish(name)
        self.sync(now + 2)
        self.assertEqual(self.pod_statements(), {lane_feed.GET_ROW, lane_feed.PUT_ROW})
        self.assertEqual({w for (s, n), (v, w) in self.db.rows.items() if n in lane_feed.POD_ROWS},
                         {self.POD})

    def test_a_pod_cannot_write_the_offer_or_another_slots_rows(self):
        self.queue()
        self.tend()
        pod = self.pod_rows()
        with self.assertRaises(state_store.StateError):
            pod.write(SLOT, "offer", {"state": "offered", "pod": self.POD, "brief": "x"})
        with self.assertRaises(state_store.StateError):
            pod.write("spark", "report", {"id": "x"})
        self.assertIsNone(pod.read("spark", "offer"))
        self.assertIsNone(self.pod_rows("lane_pod_b").read(SLOT, "offer"))
        with self.assertRaises(state_store.StateError):
            pod.grant({SLOT: "lane_pod_b"})

    def test_a_fenced_offer_needs_the_station_lease_epoch(self):
        self.queue()
        self.db.epoch = 3
        self.lease = FakeLease(epoch=4)
        with self.assertRaises(station_lease.LeaseLost):
            self.tend()
        self.lease = FakeLease(epoch=3)
        self.tend()
        self.assertEqual(self.row("offer")["state"], "offered")


if __name__ == "__main__":
    unittest.main()
