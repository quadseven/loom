"""The claim pass leaves the step rotation room, and a due hourly step runs
ahead of the rotation (loom#3014): on 2026-11-11 claim_new spent about 1000
requests against a 500 target, the rotation then ran a step or two a pass,
and the hourly scorecard refresh went 4.5 hours without running."""
import contextlib
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bus_station import Base, REPO, GitHub, station
from bus import scorecard
from bus.github import TargetReached


def spend_comments(this, free):
    """A claim read that would spend far more than the pass target: each call
    pages 100 requests of comments, and a call past the target carries over."""
    for _ in range(20):
        this.gh.paginate("/repos/%s/issues/12/comments" % REPO)
    return free


class ClaimBudgetTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add(12, state="briefed")
        self.fake.comments[12] = [
            {"id": i, "body": "c%d" % i, "user": {"login": "alice-example"}}
            for i in range(10000)]

    def rotation(self):
        path = os.path.join(self.home, "state", "station", station.STEP_ROTATION_FILE)
        with open(path) as fh:
            return json.load(fh)

    def seed_rotation(self, step):
        self.write(os.path.join(self.home, "state", "station", station.STEP_ROTATION_FILE),
                   json.dumps({"next": step}))

    def test_a_claim_pass_over_the_target_still_leaves_a_rotation_step(self):
        first = mock.Mock(__name__="roll_source_pins")
        err = io.StringIO()
        with mock.patch.object(station.Station, "_reserve_verify", spend_comments), \
                mock.patch.object(station.Station, "roll_source_pins", first), \
                contextlib.redirect_stderr(err):
            self.run_pass()
        first.assert_called()
        self.assertRegex(err.getvalue(), r"claim pass: \d+ of \d+ requests")

    def test_a_due_score_lanes_runs_though_the_rotation_stands_far_from_it(self):
        refresh = mock.Mock(__name__="score_lanes")
        self.seed_rotation(0)
        with mock.patch.object(station, "PASS_STEP_BUDGET", 0), \
                mock.patch.object(station.Station, "score_lanes", refresh):
            self.run_pass()
        refresh.assert_called_once()
        self.assertEqual(self.rotation()["next"], 1)

    def test_a_scorecard_refreshed_within_the_hour_is_not_run_ahead(self):
        refresh = mock.Mock(__name__="score_lanes")
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        card = scorecard.Scorecard(st.state, st.now)
        card.data["refreshed"] = st.now
        card.save()
        self.seed_rotation(0)
        with mock.patch.object(station, "PASS_STEP_BUDGET", 0), \
                mock.patch.object(station.Station, "score_lanes", refresh):
            self.run_pass()
        refresh.assert_not_called()


class ThreadedTargetGateTest(unittest.TestCase):
    """The rotation's shipped-row step reads ticket events on a thread pool
    (bus/metrics.py). Two reads at one under the target both passed the gate
    before either was counted, and the pass went one over the target."""

    def test_a_read_let_through_and_not_yet_sent_holds_its_place_under_the_target(self):
        def transport(method, url, headers, payload):
            return 200, {}, b"[]"

        gh = GitHub("token", transport=transport, target=1)
        entered, go = threading.Event(), threading.Event()
        real = gh.rest

        def paused(method, url, *args, **kwargs):
            if threading.current_thread().name == "first-read":
                entered.set()
                go.wait(5)
            return real(method, url, *args, **kwargs)

        gh.rest = paused
        first = threading.Thread(
            target=gh.paginate, args=("/repos/%s/issues/12/events" % REPO,),
            name="first-read")
        first.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(TargetReached):
                gh.paginate("/repos/%s/issues/13/events" % REPO)
        finally:
            go.set()
            first.join(5)
        self.assertEqual(gh.counted, 1)
        self.assertEqual(gh.carried_over, {"events": 1})
