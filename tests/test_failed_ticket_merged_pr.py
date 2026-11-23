"""A merged lane pull request moves its failed ticket to loom:merged."""

from bus.github import GitHub
from bus import station
from tests import test_bus_station as fixtures


class FailedTicketMergedPullRequestTest(fixtures.Base):
    def test_merged_lane_pull_moves_failed_ticket_to_merged_and_drops_lane(self):
        self.fake.add(12, state="failed", lane="codex",
                      body=fixtures.BODY + "\n## Acceptance criteria\n"
                           "- [ ] A separate criterion not described by the merged PR.\n")
        self.fake.pulls = [{
            "number": 99,
            "state": "closed",
            "merged_at": "2030-01-02T00:00:00Z",
            "merge_commit_sha": "a" * 40,
            "user": {"login": "alice-example"},
            "body": "A completed lane change.",
            "head": {"ref": "loom/12-fix-the-thing",
                     "repo": {"full_name": fixtures.REPO}},
        }]

        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()

        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:failed", self.fake.names(12))
        self.assertFalse(any(name.startswith("lane:") for name in self.fake.names(12)))
        self.assertEqual(self.fake.issues[12]["state"], "open")
        self.assertTrue(any("#99" in comment["body"] for comment in self.fake.comments[12]))

    def test_failed_ticket_without_merged_pull_keeps_labels_and_gets_no_comment(self):
        self.fake.add(12, state="failed", lane="codex")
        before = self.fake.names(12)

        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()

        self.assertEqual(self.fake.names(12), before)
        self.assertEqual(self.fake.comments[12], [])
        self.assertEqual(self.fake.issues[12]["state"], "open")
