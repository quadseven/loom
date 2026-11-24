"""Failed tickets move to merged when their lane pull request lands."""

from bus import station
from tests.test_bus_station import Base, GitHub, REPO


class FailedTicketMergedPullRequestTest(Base):
    def test_merged_pull_request_moves_failed_ticket_without_closing_it(self):
        self.fake.add(12, state="failed")
        self.fake.pulls = [{"number": 99, "state": "closed",
                            "merged_at": "2030-01-02T00:00:00Z",
                            "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix-the-thing"}}]

        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()

        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:failed", self.fake.names(12))
        self.assertFalse(any(name.startswith("lane:") for name in self.fake.names(12)))
        self.assertEqual("open", self.fake.issues[12]["state"])
        self.assertTrue(any("Pull request #99 merged" in comment["body"]
                            for comment in self.fake.comments[12]))

    def test_failed_ticket_without_merged_pull_request_is_untouched(self):
        self.fake.add(12, state="failed")
        before = list(self.fake.names(12))
        self.fake.pulls = [{"number": 99, "state": "closed", "merged_at": None,
                            "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix-the-thing"}}]

        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()

        self.assertEqual(before, self.fake.names(12))
        self.assertEqual([], self.fake.comments[12])
