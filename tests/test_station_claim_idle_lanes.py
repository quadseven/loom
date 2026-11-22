"""The claim pass shares its request budget among the free lanes, so one lane
with many held tickets cannot leave the lanes after it with no brief
(loom#3210)."""
import contextlib
import io
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bus_station import Base, REPO, GitHub, station
from bus.github import TargetReached


class ClaimIdleLanesTest(Base):
    def setUp(self):
        super().setUp()
        self.first, self.second = [l for l in station.LANES
                                   if l not in station.REMOTE_LANES][:2]
        for lane in (self.first, self.second):
            self.heartbeat(lane)
        self.fake.add(12, state="briefed")
        self.fake.comments[12] = [
            {"id": i, "body": "c%d" % i, "user": {"login": "alice-example"}}
            for i in range(10000)]
        self.fake.add(13, state="briefed")
        self.claimed = []

    def try_ticket(self, this, repo, lane, issue, free):
        if lane == self.first:
            # A lane that reads comments until the pass stops it.
            while True:
                this.gh.paginate("/repos/%s/issues/12/comments" % REPO)
        this.gh.paginate("/repos/%s/issues/13/comments" % REPO)
        self.claimed.append(lane)
        return True

    def run_claim(self):
        gh = GitHub("token", transport=self.fake)
        gh.target = 300
        st = station.Station(gh, self.settings)
        cands = {l: [(REPO, {"number": 12 + i, "labels": []})]
                 for i, l in enumerate((self.first, self.second))}
        err = io.StringIO()
        with mock.patch.object(station.Station, "_try_ticket",
                                  lambda this, *a: self.try_ticket(this, *a)), \
                mock.patch.object(station.Station, "_claim_candidates",
                                  lambda this, free: {l: cands[l] for l in free}), \
                mock.patch.object(station.Station, "_code_ready", lambda *a: True), \
                mock.patch.object(station.Station, "_apply_unproven_gate",
                                  lambda this, c: c), \
                mock.patch.object(station.Station, "_hand_verifies", lambda *a: None), \
                mock.patch.object(station.Station, "_reserve_verify",
                                  lambda this, free: free), \
                mock.patch.object(station.Station, "_probe_incidents",
                                  lambda this, free: free), \
                mock.patch.object(station.Station, "_prioritize_verifies",
                                  lambda this, free: free), \
                mock.patch.object(station.Station, "_not_demoted",
                                  lambda this, free: free), \
                mock.patch.object(station.Station, "_release_dead_lane_runaways",
                                  lambda *a: None), \
                contextlib.redirect_stderr(err):
            st.claim_new()
        return err.getvalue()

    def test_a_lane_that_spends_its_slice_leaves_the_next_lane_a_turn(self):
        log = self.run_claim()
        self.assertEqual(self.claimed, [self.second])
        self.assertIn("1 briefed ticket(s) left unclaimed for budget", log)
