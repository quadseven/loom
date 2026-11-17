"""A loom:running ticket whose run already ended stops holding its lane.

On 2026-11-16 six tickets sat at loom:running with their newest brief in
the lane's done or failed folder for 4 hours to 14 days, and none in an
inbox. The factory counts a running ticket as lane load, so three live lanes
(workersai, opencode-go-3, opencode-go-4) took no queued ticket while 49
queued tickets waited "no free lane in tier medium".
"""
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_bus_station import Base, REPO, GitHub, station


class EndedRunTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add(12, lane="workersai", state="running")

    def brief(self, folder, age, number=12, lane="workersai", attempt="-r2"):
        """A brief of the ticket in the lane's folder, `age` seconds old."""
        stem = "%s-%d%s.md" % (REPO.replace("/", "."), number, attempt)
        path = self.inbox(lane, *([folder] if folder else []), stem)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("Issue: https://github.com/%s/issues/%d\n" % (REPO, number))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def settle(self):
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        runner.settle_ended_runs()
        return runner

    def test_a_run_that_failed_hours_ago_frees_its_lane(self):
        self.brief("failed", 4 * 3600)
        self.settle()
        names = self.fake.names(12)
        self.assertIn("loom:failed", names)
        self.assertNotIn("loom:running", names)
        self.assertIn("nothing moved the ticket", str(self.fake.comments[12]))

    def test_a_run_whose_brief_finished_as_done_also_frees_its_lane(self):
        self.brief("done", 4 * 3600)
        self.settle()
        self.assertIn("loom:failed", self.fake.names(12))

    def test_a_brief_still_in_the_inbox_keeps_the_ticket_running(self):
        self.brief("failed", 4 * 3600, attempt="-r1")
        self.brief(None, 4 * 3600)
        self.settle()
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_run_that_just_ended_is_left_for_its_report(self):
        self.brief("failed", 60)
        self.settle()
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_ticket_with_no_brief_on_record_is_left_alone(self):
        self.settle()
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_locked_lane_keeps_the_ticket_running(self):
        self.brief("failed", 4 * 3600)
        lock = os.path.join(self.home, "run", "workersai.lock")
        os.makedirs(lock)
        Path(lock, "pid").write_text("%d\n" % os.getpid())
        self.settle()
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_factory_incident_hold_stays_running(self):
        self.brief("failed", 4 * 3600)
        self.fake.issues[12]["labels"].append({"name": "wait:incident"})
        self.settle()
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_settled_run_no_longer_counts_as_lane_load(self):
        self.brief("failed", 4 * 3600)
        runner = self.settle()
        counter = runner._factory()
        counter.count_load({REPO: {12: self.fake.issues[12]}})
        self.assertEqual(counter.load.get("workersai", 0), 0)


if __name__ == "__main__":
    unittest.main()
