"""The station clears ended local runs without disturbing live runs."""
import json
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
        Path(path).write_text("Issue: %s#%d\n" % (REPO, number))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def settle(self, after=0):
        runner = station.Station(GitHub("token", transport=self.fake), self.settings,
                                 now=time.time() + after)
        runner.settle_ended_runs()
        return runner

    def test_a_reported_park_clears_the_running_label_and_keeps_its_lane(self):
        path = self.brief("failed", 0)
        self.write(os.path.join(self.home, "logs", "workersai",
                                Path(path).stem + ".log"),
                   "[t] ticket failed: [work] validation failed\n")
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        runner.report_outcomes()
        self.assertEqual(set(self.fake.names(12)), {"loom:ticket", "loom:failed", "lane:workersai"})

    def test_a_pass_requeues_a_stale_ticket_on_a_lane_with_a_live_run(self):
        path = self.brief("failed", 0)
        self.write(os.path.join(self.home, "state", "station", "reported.json"),
                   json.dumps({"workersai/" + Path(path).stem: "failed"}))
        self.fake.add(13, lane="workersai", state="running")
        self.brief(None, 60, number=13)
        lock = os.path.join(self.home, "run", "workersai.lock")
        self.write(os.path.join(lock, "pid"), "%d\n" % os.getpid())

        self.run_pass()

        self.assertEqual(set(self.fake.names(12)), {"loom:ticket", "loom:queued"})
        self.assertEqual(set(self.fake.names(13)),
                         {"loom:ticket", "loom:running", "lane:workersai"})

    def test_one_pass_clears_more_than_ten_stale_runs(self):
        for number in range(13, 25):
            self.fake.add(number, lane="workersai", state="running")

        self.run_pass()

        self.assertEqual([n for n in range(12, 25) if "loom:running" in self.fake.names(n)], [])

    def test_a_run_that_failed_hours_ago_frees_its_lane(self):
        self.brief("failed", 0)
        self.settle(after=4 * 3600)
        self.assertEqual(set(self.fake.names(12)), {"loom:ticket", "loom:queued"})

    def test_a_run_whose_brief_finished_as_done_also_frees_its_lane(self):
        self.brief("done", 0)
        self.settle(after=4 * 3600)
        self.assertIn("loom:queued", self.fake.names(12))

    def test_a_brief_still_in_the_inbox_keeps_the_ticket_running(self):
        self.brief("failed", 4 * 3600, attempt="-r1")
        self.brief(None, 4 * 3600)
        self.settle()
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_run_that_just_ended_is_left_for_its_report(self):
        self.brief("failed", 60)
        self.settle()
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_ticket_with_no_brief_on_record_returns_to_the_queue(self):
        self.settle()
        self.assertEqual(set(self.fake.names(12)), {"loom:ticket", "loom:queued"})

    def test_a_half_written_claim_keeps_the_ticket_running(self):
        path = Path(self.brief(None, 0))
        path.rename(path.with_suffix(".claiming"))
        self.settle()
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_factory_incident_hold_survives_requeue(self):
        self.fake.issues[12]["labels"].append({"name": "wait:incident"})
        self.settle()
        self.assertEqual(set(self.fake.names(12)), {"loom:ticket", "loom:queued", "wait:incident"})

    def test_an_existing_queue_state_survives_reconciliation(self):
        self.fake.issues[12]["labels"].append({"name": "loom:pr"})
        self.settle()
        self.assertEqual(set(self.fake.names(12)), {"loom:ticket", "loom:pr"})

    def test_a_settled_run_no_longer_counts_as_lane_load(self):
        runner = self.settle()
        counter = runner._factory()
        counter.count_load({REPO: {12: self.fake.issues[12]}})
        self.assertEqual(counter.load.get("workersai", 0), 0)


if __name__ == "__main__":
    unittest.main()
