"""A station on the file backend stands by while another station runs (loom#3050).

The cluster station holds a lease, but a station woken on a laptop with no
lease cannot see it, and two stations double-claim. Every station edits one
beat comment on the lane status issue at the start of a pass and stands by
while another station's beat is fresh; a leased station also yields to a lane
table written by a station that stamps nothing (code from before the guard)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from bus import state_store, station, station_beat  # noqa: E402
from bus.github import GitHubError, Response  # noqa: E402

REPO = "example-owner/example-repo"
NOW = 1_800_000_000.0
LANES = station.LANES_MARKER


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class FakeGitHub:
    """The lane status issue and its comments, as the guard reads them."""
    cache_dir = None

    def __init__(self, issue=7, fail=False):
        self.issue = issue
        self.fail = fail
        self.comments = []
        self.next_id = 100
        self.writes = []

    def add(self, body, age, ident=None):
        ident = ident or self.next_id
        self.next_id += 1
        self.comments.append({"id": ident, "body": body, "updated_at": iso(NOW - age)})
        return ident

    def rest(self, method, path, body=None, **kw):
        if self.fail:
            raise GitHubError("read refused")
        if method == "GET" and "issues?labels=loom:lanes" in path:
            return Response(200, [{"number": self.issue}] if self.issue else [])
        if method == "POST" and path.endswith("/issues/%s/comments" % self.issue):
            ident = self.add(body["body"], 0)
            self.writes.append(("POST", ident))
            return Response(201, {"id": ident})
        if method == "PATCH" and "/issues/comments/" in path:
            ident = int(path.rsplit("/", 1)[1])
            for comment in self.comments:
                if comment["id"] == ident:
                    comment["body"], comment["updated_at"] = body["body"], iso(NOW)
            self.writes.append(("PATCH", ident))
            return Response(200, {"id": ident})
        raise AssertionError("unexpected %s %s" % (method, path))

    def paginate(self, path):
        if self.fail:
            raise GitHubError("read refused")
        return list(self.comments)


def guard(gh, station_id="alpha", leased=False):
    return station_beat.Guard(gh, REPO, station_id, leased, LANES, clock=lambda: NOW)


def beat_body(station_id, leased=False):
    return station_beat.render_beat(station_id, leased)


class BeatTest(unittest.TestCase):
    def test_a_station_with_no_beat_to_read_runs(self):
        self.assertIsNone(guard(FakeGitHub()).verdict())

    def test_a_fresh_beat_from_another_station_stands_it_by(self):
        gh = FakeGitHub()
        gh.add(beat_body("beta"), age=station_beat.BEAT_TTL - 1)
        reason = guard(gh).verdict()
        self.assertIn("ran a pass", reason)

    def test_a_stale_beat_from_another_station_does_not(self):
        gh = FakeGitHub()
        gh.add(beat_body("beta"), age=station_beat.BEAT_TTL + 1)
        self.assertIsNone(guard(gh).verdict())

    def test_a_station_never_stands_by_for_its_own_beat(self):
        gh = FakeGitHub()
        gh.add(beat_body("alpha"), age=1)
        self.assertIsNone(guard(gh).verdict())

    def test_a_beat_stamped_in_the_future_is_not_believed(self):
        gh = FakeGitHub()
        gh.add(beat_body("beta"), age=-station_beat.FUTURE_SLACK - 5)
        self.assertIsNone(guard(gh).verdict())

    def test_an_unreadable_status_issue_lets_the_pass_run(self):
        said = []
        g = station_beat.Guard(FakeGitHub(fail=True), REPO, "alpha", False, LANES,
                               say=said.append, clock=lambda: NOW)
        self.assertIsNone(g.verdict())
        self.assertIn("running the pass", said[0])

    def test_no_status_issue_means_nothing_to_read_or_write(self):
        gh = FakeGitHub(issue=0)
        g = guard(gh)
        self.assertIsNone(g.verdict())
        g.stamp()
        self.assertEqual(gh.writes, [])


class StampTest(unittest.TestCase):
    def test_the_first_stamp_posts_one_comment_and_later_stamps_edit_it(self):
        gh = FakeGitHub()
        g = guard(gh)
        g.verdict()
        g.stamp()
        g.stamp()
        self.assertEqual([w[0] for w in gh.writes], ["POST", "PATCH"])
        self.assertEqual(len(gh.comments), 1)

    def test_a_station_takes_over_a_stale_beat_by_editing_it(self):
        gh = FakeGitHub()
        ident = gh.add(beat_body("beta"), age=station_beat.BEAT_TTL + 60)
        g = guard(gh)
        self.assertIsNone(g.verdict())
        g.stamp()
        self.assertEqual(gh.writes, [("PATCH", ident)])
        self.assertEqual(station_beat.parse_beat(gh.comments[0]["body"]).tag,
                         station_beat.public_tag("alpha"))

    def test_the_station_name_is_never_published(self):
        gh = FakeGitHub()
        g = guard(gh, station_id="my-laptop.example")
        g.verdict()
        g.stamp()
        self.assertNotIn("my-laptop", gh.comments[0]["body"])

    def test_a_failed_write_is_logged_and_not_raised(self):
        gh = FakeGitHub()
        said = []
        g = station_beat.Guard(gh, REPO, "alpha", False, LANES, say=said.append,
                               clock=lambda: NOW)
        g.verdict()
        gh.fail = True
        g.stamp()
        self.assertIn("could not write the beat", said[0])

    def test_a_long_pass_stamps_again_when_it_ends_and_a_short_one_does_not(self):
        now = [NOW]
        gh = FakeGitHub()
        g = station_beat.Guard(gh, REPO, "alpha", False, LANES, clock=lambda: now[0])
        g.verdict()
        g.stamp()
        g.renew()
        self.assertEqual(len(gh.writes), 1)
        now[0] += station_beat.BEAT_TTL
        g.renew()
        self.assertEqual(len(gh.writes), 2)


class ForeignTableTest(unittest.TestCase):
    """A leased station yields to a lane table an unstamped station wrote."""

    def table(self, stamp=None):
        return "\n".join([LANES, "Updated now"] + ([stamp] if stamp else []))

    def test_a_leased_station_yields_to_a_fresh_unstamped_lane_table(self):
        gh = FakeGitHub()
        gh.add(self.table(), age=60)
        self.assertIn("does not stamp", guard(gh, leased=True).verdict())

    def test_a_leased_station_yields_to_a_table_another_station_stamped(self):
        gh = FakeGitHub()
        gh.add(self.table(station_beat.stamp_line("beta")), age=60)
        self.assertIsNotNone(guard(gh, leased=True).verdict())

    def test_a_table_this_station_stamped_is_its_own(self):
        gh = FakeGitHub()
        gh.add(self.table(station_beat.stamp_line("alpha")), age=60)
        self.assertIsNone(guard(gh, leased=True).verdict())

    def test_an_old_unstamped_table_is_a_sleeping_station(self):
        gh = FakeGitHub()
        gh.add(self.table(), age=station_beat.FOREIGN_WINDOW + 1)
        self.assertIsNone(guard(gh, leased=True).verdict())

    def test_a_station_without_the_lease_does_not_read_the_table(self):
        gh = FakeGitHub()
        gh.add(self.table(), age=60)
        self.assertIsNone(guard(gh, leased=False).verdict())

    def test_the_window_covers_a_table_republished_every_fifteen_minutes(self):
        from bus import brain
        self.assertGreater(station_beat.FOREIGN_WINDOW, brain.LANES_REPUBLISH)


class RunTest(unittest.TestCase):
    """station.run() with the guard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = station.Settings(self.tmp.name, [REPO], [], [], status_repo=REPO)
        self.ran = []
        previous = state_store.active()
        self.addCleanup(state_store.use, previous)
        patches = [mock.patch.object(station.Station, "run_pass",
                                     lambda this: self.ran.append(True)),
                   mock.patch.object(station, "_trust_app_login", lambda settings, gh: None),
                   mock.patch.object(station.Station, "sample_operator_budget", lambda this: None),
                   mock.patch.object(station.Station, "end_pass", lambda this: None),
                   mock.patch.object(time, "time", lambda: NOW)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.gh = FakeGitHub()

    def run_station(self, **env):
        environ = {"LOOM_STATION_ID": "alpha", **env}
        with mock.patch.dict(os.environ, environ, clear=False):
            return station.run(self.settings, self.gh)

    def test_a_station_with_no_lease_stands_by_for_a_fresh_cluster_beat(self):
        self.gh.add(beat_body("cluster", leased=True), age=30)
        self.assertEqual(self.run_station(), 0)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.gh.writes, [])

    def test_a_pass_that_runs_writes_its_beat_first(self):
        self.assertEqual(self.run_station(), 0)
        self.assertEqual(self.ran, [True])
        self.assertEqual(self.gh.writes[0][0], "POST")
        self.assertEqual(station_beat.parse_beat(self.gh.comments[0]["body"]).leased, False)

    def test_a_leased_station_stands_by_for_a_woken_unstamped_station(self):
        self.gh.add(LANES + "\nUpdated now", age=30)
        self.assertEqual(self.run_station(LOOM_STATION_LEASE="1"), 0)
        self.assertEqual(self.ran, [])

    def test_a_slow_pass_renews_its_beat_even_when_end_pass_raises(self):
        clock = [NOW]

        def slow_end_pass(this):
            clock[0] += station_beat.BEAT_TTL
            raise OSError("disk")

        with mock.patch.object(station.Station, "end_pass", slow_end_pass), \
                mock.patch.object(station_beat.time, "time", lambda: clock[0]):
            with self.assertRaises(OSError):
                self.run_station()
        self.assertEqual([w[0] for w in self.gh.writes], ["POST", "PATCH"])

    def test_the_guard_can_be_turned_off(self):
        self.gh.add(beat_body("cluster", leased=True), age=30)
        self.assertEqual(self.run_station(LOOM_STATION_GUARD="0"), 0)
        self.assertEqual(self.ran, [True])
        self.assertEqual(self.gh.writes, [])

    def test_a_status_repo_that_is_not_set_runs_unguarded(self):
        self.settings.status_repo = ""
        self.gh.add(beat_body("cluster", leased=True), age=30)
        self.assertEqual(self.run_station(), 0)
        self.assertEqual(self.ran, [True])

    def test_the_lane_table_carries_the_stations_stamp(self):
        runner = station.Station(self.gh, self.settings, now=NOW)
        with mock.patch.dict(os.environ, {"LOOM_STATION_ID": "alpha"}), \
                mock.patch.object(runner, "_spend_shown", return_value=None), \
                mock.patch.object(runner, "_scorecard_lines", return_value=[]), \
                mock.patch.object(runner, "_remote_claim_count", return_value=0), \
                mock.patch.object(runner, "_disk_status_line", return_value=""):
            body = runner._lanes_body([("codex", "yes", "-")])
        self.assertEqual(station_beat.lanes_stamp(body), station_beat.public_tag("alpha"))
        self.assertTrue(body.startswith(LANES))


if __name__ == "__main__":
    unittest.main()
