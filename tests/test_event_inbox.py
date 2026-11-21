"""The station as the fleet event inbox's consumer (LOOM_INBOX=1).

Events are hints: a pass claims its inbox's due events under an inbox lease
tied to the station lease, reads their repos first, and marks them done at
its end. Between passes the station listens for a NOTIFY with a debounce, so
a burst of events makes one pass. With LOOM_INBOX off nothing here connects
or changes a pass. Everything runs against an in-memory stand-in for the
fleet database; nothing connects anywhere."""
from __future__ import annotations

import collections
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import event_inbox, station, station_lease  # noqa: E402

INSTANCE = "example"
CONFIG = event_inbox.config


def ref(repo, number):
    """An issue subject, built so no fixture holds a literal cross-repo ref."""
    return "%s#%d" % (repo, number)
INBOX = "loom-station"
TTL = 90
ENV = {
    "LOOM_INBOX": "1",
    "LOOM_INBOX_DB_HOST": "db.example.internal",
    "LOOM_INBOX_DB_NAME": "events",
    "LOOM_INBOX_DB_USER": "inbox_role",
    "LOOM_INBOX_DB_PASSWORD": "placeholder",
    "LOOM_INBOX_INSTANCE": INSTANCE,
}


class FakeDb:
    """inbox_event and inbox_lease, and the statements the module sends.
    The database clock is `now`; each statement is atomic, as in Postgres."""

    def __init__(self):
        self.now = 10000.0
        self.events = {}   # id -> dict
        self.lease = {}    # inbox -> dict(holder, epoch, expires)
        self.sent = []
        self.connects = 0
        self.fail_connect = None
        self.notify_at = []  # (monotonic time, payload) a listener receives
        self.next_id = 1

    # -- producers, for the tests -------------------------------------------
    def append(self, subject, kind="github.pull_request.synchronize", inbox=INBOX, delay=0.0):
        eid = self.next_id
        self.next_id += 1
        self.events[eid] = dict(id=eid, instance=INSTANCE, inbox=inbox, kind=kind,
                                subject=subject, status="pending", attempts=0,
                                claimed_by=None, claim_epoch=None, run_ref=None,
                                result=None, received_at=self.now, not_before=self.now + delay)
        return eid

    def status(self, eid):
        return self.events[eid]["status"]

    # -- the statements -------------------------------------------------------
    def _held(self, inbox, holder, epoch):
        row = self.lease.get(inbox)
        return bool(row and row["holder"] == holder and row["epoch"] == epoch
                    and row["expires"] > self.now)

    def execute(self, sql, params):
        self.sent.append(sql)
        handler = {
            event_inbox.STATEMENT_TIMEOUT: lambda p: ([], 0),
            event_inbox.LISTEN: lambda p: ([], 0),
            event_inbox.PUMP: lambda p: ([(1,)], 1),
            event_inbox.ACQUIRE: self._acquire,
            event_inbox.RENEW: self._renew,
            event_inbox.RELEASE: self._release,
            event_inbox.BURY: self._bury,
            event_inbox.CLAIM: self._claim,
            event_inbox.COMPLETE: self._complete,
            event_inbox.PENDING_AGE: self._pending_age,
        }.get(sql)
        if handler is None:
            raise AssertionError("unexpected statement: %s" % sql)
        return handler(params)

    def _acquire(self, p):
        inbox, instance, holder, ttl = p
        assert instance == INSTANCE
        row = self.lease.get(inbox)
        if row is None:
            self.lease[inbox] = dict(holder=holder, epoch=1, expires=self.now + ttl)
            return [(1,)], 1
        if row["expires"] < self.now or row["holder"] == holder:
            row.update(holder=holder, epoch=row["epoch"] + 1, expires=self.now + ttl)
            return [(row["epoch"],)], 1
        return [], 0

    def _renew(self, p):
        ttl, inbox, holder, epoch = p
        row = self.lease.get(inbox)
        if row and row["holder"] == holder and row["epoch"] == epoch:
            row["expires"] = self.now + ttl
            return [], 1
        return [], 0

    def _release(self, p):
        inbox, holder, epoch = p
        row = self.lease.get(inbox)
        if row and row["holder"] == holder and row["epoch"] == epoch:
            row["expires"] = self.now - 0.001
            return [], 1
        return [], 0

    def _bury(self, p):
        _, instance, inbox, epoch, limit, l_inbox, holder, l_epoch = p
        if not self._held(l_inbox, holder, l_epoch):
            return [], 0
        n = 0
        for ev in self.events.values():
            if (ev["instance"] == instance and ev["inbox"] == inbox and ev["status"] == "claimed"
                    and ev["claim_epoch"] < epoch and ev["attempts"] >= limit):
                ev.update(status="dead", result="dead: abandoned")
                n += 1
        return [], n

    def _claim(self, p):
        instance, inbox, epoch, l_inbox, holder, l_epoch, limit, by, new_epoch = p
        if not self._held(l_inbox, holder, l_epoch):
            return [], 0
        out = []
        for ev in sorted(self.events.values(), key=lambda e: e["id"]):
            if len(out) >= limit:
                break
            due = ev["not_before"] <= self.now
            free = ev["status"] == "pending" or (ev["status"] == "claimed"
                                                 and ev["claim_epoch"] < epoch)
            if ev["instance"] == instance and ev["inbox"] == inbox and due and free:
                ev.update(status="claimed", claimed_by=by, claim_epoch=new_epoch,
                          attempts=ev["attempts"] + 1)
                out.append((ev["id"], ev["kind"], ev["subject"]))
        return out, len(out)

    def _complete(self, p):
        result, run_ref, ids, instance, inbox, epoch, l_inbox, l_epoch = p
        row = self.lease.get(l_inbox)
        if not row or row["epoch"] != l_epoch:
            return [], 0
        n = 0
        for eid in ids:
            ev = self.events.get(eid)
            if (ev and ev["instance"] == instance and ev["inbox"] == inbox
                    and ev["status"] == "claimed" and ev["claim_epoch"] == epoch):
                ev.update(status="done", result=result, run_ref=run_ref)
                n += 1
        return [], n

    def _pending_age(self, p):
        instance, inbox = p
        ages = [self.now - ev["received_at"] for ev in self.events.values()
                if ev["instance"] == instance and ev["inbox"] == inbox
                and ev["status"] == "pending" and ev["not_before"] <= self.now]
        return [(max(ages) if ages else None,)], 1

    # -- the driver -----------------------------------------------------------
    def connect(self, **kwargs):
        if self.fail_connect:
            raise self.fail_connect
        self.connects += 1
        self.kwargs = kwargs
        return FakeConn(self)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.description = None
        self.rowcount = -1

    def execute(self, sql, params=()):
        rows, count = self.conn.db.execute(sql, tuple(params))
        self.rows = list(rows)
        self.rowcount = count
        returns = sql.lstrip().upper().startswith("SELECT") or "RETURNING" in sql
        self.description = [("c",)] if returns else None
        if sql == event_inbox.PUMP:
            self.conn.deliver()

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.autocommit = False
        self.closed = False
        self.notifications = collections.deque()
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def deliver(self):
        """Hand over every NOTIFY sent up to the clock's time."""
        clock_now = self.db.clock()
        due = [n for n in self.db.notify_at if n[0] <= clock_now]
        self.db.notify_at = [n for n in self.db.notify_at if n[0] > clock_now]
        for _, payload in due:
            self.notifications.append((4242, event_inbox.CHANNEL, payload))


class Driver:
    """A stand-in for pg8000.dbapi."""

    def __init__(self, db):
        self.db = db
        self.Error = RuntimeError

    def connect(self, **kwargs):
        return self.db.connect(**kwargs)


class FakeLease:
    """The station lease's surface the consumer uses."""

    def __init__(self, holder="station-a", epoch=7, ttl=TTL):
        self.holder = holder
        self.epoch = epoch
        self.ttl = ttl
        self.lost = False
        self.checks = 0

    def check(self):
        self.checks += 1
        if self.lost:
            raise station_lease.LeaseLost("lease %s at epoch %s was taken" % (self.holder, self.epoch))


class Clock:
    def __init__(self, now=500.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class InboxTestCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        self.clock = Clock()
        self.db.clock = self.clock
        self.driver = Driver(self.db)
        self.said = []

    def consumer(self, lease=None, env=None):
        return event_inbox.select(env or ENV, lease or FakeLease(), self.said.append, self.driver)


class InboxOffTest(InboxTestCase):
    def test_the_inbox_is_off_by_default(self):
        for env in ({}, {"LOOM_INBOX": "0"}, {"LOOM_INBOX": "yes"}, dict(ENV, LOOM_INBOX="")):
            self.assertIsNone(event_inbox.select(env, FakeLease(), self.said.append, self.driver))
        self.assertEqual(self.db.connects, 0)
        self.assertEqual(self.said, [])

    def test_the_wait_with_the_inbox_off_is_the_interval_and_opens_nothing(self):
        slept = []
        why = event_inbox.wait({"LOOM_STATION_INTERVAL": "120"}, self.said.append,
                               clock=self.clock, sleep=slept.append, dbapi=self.driver)
        self.assertEqual((why, slept), ("interval", [120.0]))
        self.assertEqual(self.db.connects, 0)

    def test_the_wait_reads_the_interval_as_the_installer_does(self):
        for raw, want in (("", 60), ("59", 60), ("abc", 60), ("90", 90)):
            self.assertEqual(event_inbox.interval({"LOOM_STATION_INTERVAL": raw}), want)

    def test_a_pass_with_the_inbox_off_is_the_locked_pass_alone(self):
        with mock.patch.object(station, "_locked_pass", return_value=0) as locked, \
                mock.patch.dict(os.environ, {"LOOM_INBOX": ""}):
            code = station._inbox_pass(mock.sentinel.settings, "state", None, {}, FakeLease())
        self.assertEqual(code, 0)
        locked.assert_called_once_with(mock.sentinel.settings, "state", None, {})
        self.assertEqual(self.db.connects, 0)

    def test_a_missing_setting_leaves_the_inbox_off_and_says_which(self):
        env = dict(ENV)
        del env["LOOM_INBOX_INSTANCE"]
        self.assertIsNone(event_inbox.select(env, FakeLease(), self.said.append, self.driver))
        self.assertIn("LOOM_INBOX_INSTANCE", self.said[0])
        self.assertEqual(self.db.connects, 0)

    def test_the_inbox_connects_with_its_own_settings_not_the_station_database(self):
        self.consumer().begin()
        self.assertEqual(self.db.kwargs["user"], "inbox_role")
        self.assertEqual(self.db.kwargs["database"], "events")


class LeaseTieTest(InboxTestCase):
    def test_the_inbox_needs_the_station_lease(self):
        self.assertIsNone(event_inbox.select(ENV, None, self.said.append, self.driver))
        self.assertIn("LOOM_STATION_LEASE=1", self.said[0])
        self.assertEqual(self.db.connects, 0)

    def test_the_inbox_lease_is_never_taken_without_the_station_lease(self):
        self.db.append(ref("example-owner/one", 5))
        lease = FakeLease()
        lease.lost = True
        consumer = self.consumer(lease)
        self.assertEqual(consumer.begin(), [])
        consumer.close()
        self.assertNotIn(event_inbox.ACQUIRE, self.db.sent)
        self.assertEqual(self.db.lease, {})
        self.assertEqual(self.db.status(1), "pending")

    def test_the_inbox_lease_takes_the_station_holder_and_ttl(self):
        consumer = self.consumer(FakeLease(holder="station-b", ttl=120))
        consumer.begin()
        self.assertEqual(self.db.lease[INBOX]["holder"], "station-b")
        self.assertEqual(self.db.lease[INBOX]["expires"], self.db.now + 120)

    def test_a_station_that_loses_its_lease_mid_pass_marks_nothing_done_and_gives_the_inbox_up(self):
        eid = self.db.append(ref("example-owner/one", 5))
        lease = FakeLease()
        consumer = self.consumer(lease)
        consumer.begin()
        lease.lost = True
        self.assertEqual(consumer.finish("pass-1"), 0)
        consumer.close()
        self.assertNotIn(event_inbox.COMPLETE, self.db.sent)
        self.assertEqual(self.db.status(eid), "claimed")
        self.assertLess(self.db.lease[INBOX]["expires"], self.db.now)

    def test_a_peers_live_inbox_lease_means_no_events_this_pass(self):
        self.db.lease[INBOX] = dict(holder="station-z", epoch=3, expires=self.db.now + 50)
        self.db.append(ref("example-owner/one", 5))
        consumer = self.consumer()
        self.assertEqual(consumer.begin(), [])
        self.assertEqual(self.db.status(1), "pending")
        self.assertIn("another consumer", self.said[-1])

    def test_a_takeover_fences_out_the_old_holders_done(self):
        eid = self.db.append(ref("example-owner/one", 5))
        consumer = self.consumer()
        consumer.begin()
        # The old holder paused past its TTL; a peer took the inbox over.
        self.db.now += TTL + 1
        peer = self.consumer(FakeLease(holder="station-b"))
        self.assertEqual([c.id for c in peer.begin()], [eid])
        self.assertEqual(consumer.finish("pass-old"), 0)
        self.assertEqual(self.db.status(eid), "claimed")
        self.assertEqual(peer.finish("pass-new"), 1)
        self.assertEqual(self.db.events[eid]["run_ref"], "pass-new")


class PassTest(InboxTestCase):
    def fake_config(self, env, dbapi=None):
        return CONFIG(env, self.driver)

    def test_a_pass_claims_due_events_and_marks_them_done_at_its_end(self):
        ids = [self.db.append("example-owner/one#%d" % n) for n in (1, 2)]
        later = self.db.append(ref("example-owner/two", 3), delay=30)
        consumer = self.consumer()
        claimed = consumer.begin()
        self.assertEqual([c.id for c in claimed], ids)
        self.assertEqual(consumer.finish("loom-pass:station-a:7:1"), 2)
        consumer.close()
        self.assertEqual([self.db.status(i) for i in ids], ["done", "done"])
        self.assertEqual(self.db.events[ids[0]]["run_ref"], "loom-pass:station-a:7:1")
        self.assertEqual(self.db.status(later), "pending")

    def test_a_pass_that_dies_returns_its_events_when_the_lease_expires(self):
        eid = self.db.append(ref("example-owner/one", 5))
        self.consumer().begin()
        # The process died: no finish, no close. Within the TTL a peer gets nothing.
        peer = self.consumer(FakeLease(holder="station-b"))
        self.assertEqual(peer.begin(), [])
        self.db.now += TTL + 1
        again = self.consumer(FakeLease(holder="station-b"))
        self.assertEqual([c.id for c in again.begin()], [eid])
        self.assertEqual(self.db.events[eid]["attempts"], 2)

    def test_a_pass_that_stops_early_gives_its_events_to_the_next_pass_at_once(self):
        eid = self.db.append(ref("example-owner/one", 5))
        with mock.patch.object(station, "_locked_pass", return_value=1) as locked, \
                mock.patch.dict(os.environ, ENV), \
                mock.patch.object(event_inbox, "config", side_effect=self.fake_config):
            settings = mock.Mock(repos=["example-owner/one"])
            code = station._inbox_pass(settings, "state", None, {}, FakeLease())
        self.assertEqual(code, 1)
        self.assertEqual(locked.call_args[0][4], ["example-owner/one"])
        self.assertEqual(self.db.status(eid), "claimed")
        self.assertNotIn(event_inbox.COMPLETE, self.db.sent)
        # The same station's next pass takes them straight back.
        self.assertEqual([c.id for c in self.consumer().begin()], [eid])

    def test_a_pass_that_ran_marks_its_events_done(self):
        eid = self.db.append(ref("example-owner/one", 5))

        def ran(settings, state, gh, hooks, priority=(), outcome=None):
            outcome["passed"] = True
            return 0

        with mock.patch.object(station, "_locked_pass", side_effect=ran), \
                mock.patch.dict(os.environ, ENV), \
                mock.patch.object(event_inbox, "config", side_effect=self.fake_config):
            code = station._inbox_pass(mock.Mock(repos=["example-owner/one"]), "state", None, {},
                                       FakeLease())
        self.assertEqual(code, 0)
        self.assertEqual(self.db.status(eid), "done")
        self.assertTrue(self.db.events[eid]["run_ref"].startswith("loom-pass:station-a:7:"))
        self.assertLess(self.db.lease[INBOX]["expires"], self.db.now)

    def test_an_event_that_kills_every_pass_ends_dead(self):
        eid = self.db.append(ref("example-owner/one", 5))
        for _ in range(event_inbox.MAX_ATTEMPTS):
            self.consumer().begin()
        self.consumer().begin()
        self.assertEqual(self.db.status(eid), "dead")

    def test_a_database_failure_leaves_the_pass_without_events(self):
        self.db.fail_connect = OSError("connection refused")
        consumer = self.consumer()
        self.assertEqual(consumer.begin(), [])
        self.assertEqual(consumer.finish("pass"), 0)
        consumer.close()
        self.assertIn("could not claim events", self.said[-1])

    def test_another_inbox_and_instance_are_not_read(self):
        self.db.append(ref("example-owner/one", 5), inbox="dispatcher")
        self.db.events[self.db.append(ref("example-owner/one", 6))]["instance"] = "other"
        self.assertEqual(self.consumer().begin(), [])


class PriorityTest(InboxTestCase):
    WATCHED = ["example-owner/one", "example-owner/two", "example-owner/three"]

    def test_subjects_become_watched_repos_in_order_once_each(self):
        subjects = [ref("example-owner/three", 4), "example-owner/one@refs/heads/main",
                    ref("example-owner/three", 9), "example-owner/two", None, ""]
        self.assertEqual(event_inbox.priority_repos(subjects, self.WATCHED),
                         ["example-owner/three", "example-owner/one", "example-owner/two"])

    def test_a_subject_outside_the_watched_repos_is_dropped(self):
        subjects = [ref("stranger/repo", 1), "example-owner/one/../x#2", ref("example-owner/onex", 3),
                    "../../etc#4"]
        self.assertEqual(event_inbox.priority_repos(subjects, self.WATCHED), [])

    def test_priority_repos_are_scanned_first_and_even_when_idle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = station.Station.__new__(station.Station)
        st.s = mock.Mock(repos=self.WATCHED)
        st.state = tmp.name
        st._due = None
        st._inbox_repos = lambda: {"example-owner/one"}
        st._scan_due = lambda rec: False
        st.priority_repos = ["example-owner/three"]
        self.assertEqual(st.scan_repos(), ["example-owner/three", "example-owner/one"])

    def test_no_priority_leaves_the_scan_order_as_it_was(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = station.Station.__new__(station.Station)
        st.s = mock.Mock(repos=self.WATCHED)
        st.state = tmp.name
        st._due = None
        st._inbox_repos = lambda: set()
        st._scan_due = lambda rec: True
        st.priority_repos = []
        self.assertEqual(st.scan_repos(), self.WATCHED)


class WaitTest(InboxTestCase):
    def wait(self, env=None):
        return event_inbox.wait(env or ENV, self.said.append, clock=self.clock,
                                sleep=self.clock.sleep, dbapi=self.driver)

    def test_with_no_event_the_wait_is_the_interval(self):
        start = self.clock.now
        self.assertEqual(self.wait(), "interval")
        self.assertAlmostEqual(self.clock.now - start, 60.0)
        self.assertIn(event_inbox.LISTEN, self.db.sent)

    def test_a_notify_starts_the_pass_after_the_debounce(self):
        start = self.clock.now
        self.db.notify_at = [(start + 10, INBOX)]
        self.assertEqual(self.wait(), "event")
        self.assertAlmostEqual(self.clock.now - start, 10 + event_inbox.DEFAULT_DEBOUNCE, delta=1.0)

    def test_a_burst_of_ten_events_makes_one_pass(self):
        start = self.clock.now
        # Ten events arrive one second apart, 5 s into the wait.
        self.db.notify_at = [(start + 5 + n, INBOX) for n in range(10)]
        passes = 0
        self.assertEqual(self.wait(), "event")
        passes += 1
        # The wait ended 15 s after the first event, not once per event.
        self.assertAlmostEqual(self.clock.now - start, 5 + event_inbox.DEFAULT_DEBOUNCE, delta=1.0)
        for n in range(10):
            self.db.append("example-owner/one#%d" % n)
        consumer = self.consumer()
        self.assertEqual(len(consumer.begin()), 10)
        consumer.finish("pass-1")
        consumer.close()
        self.assertEqual(passes, 1)
        self.assertEqual({e["status"] for e in self.db.events.values()}, {"done"})
        self.assertEqual(self.db.notify_at, [])

    def test_an_event_that_arrived_during_the_last_pass_counts_from_its_arrival(self):
        self.db.append(ref("example-owner/one", 5))
        self.db.now += 12  # it has waited 12 s by the database's clock
        start = self.clock.now
        self.assertEqual(self.wait(), "event")
        self.assertAlmostEqual(self.clock.now - start, event_inbox.DEFAULT_DEBOUNCE - 12, delta=1.0)

    def test_a_notify_for_another_inbox_does_not_wake_the_station(self):
        start = self.clock.now
        self.db.notify_at = [(start + 5, "dispatcher")]
        self.assertEqual(self.wait(), "interval")
        self.assertAlmostEqual(self.clock.now - start, 60.0)

    def test_the_debounce_is_a_setting(self):
        start = self.clock.now
        self.db.notify_at = [(start + 2, INBOX)]
        self.assertEqual(self.wait(dict(ENV, LOOM_INBOX_DEBOUNCE="30")), "event")
        self.assertAlmostEqual(self.clock.now - start, 32.0, delta=1.0)

    def test_a_broken_inbox_never_shortens_the_wait(self):
        self.db.fail_connect = OSError("connection refused")
        start = self.clock.now
        self.assertEqual(self.wait(), "fallback")
        self.assertAlmostEqual(self.clock.now - start, 60.0)
        self.assertIn("could not listen", self.said[-1])


if __name__ == "__main__":
    unittest.main()
