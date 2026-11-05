"""The early lane republish carries the last read pull request counts."""
from pathlib import Path
import calendar
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_bus_station import Base, BODY_WITH_CRITERIA, REPO, AUTHORS, GitHub, station

READ_AT = calendar.timegm((2026, 11, 5, 16, 7, 30, 0, 0, 0))


class LanesPrCarryTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.default_body = BODY_WITH_CRITERIA
        self.settings.status_repo = REPO
        self.fake.pulls = [dict(number=100 + i, state="open", draft=False,
                                user={"login": AUTHORS[0]}, body="",
                                head={"ref": "loom/%d-work" % (100 + i)})
                           for i in range(6)]

    def publish(self, now, refresh_pr_counts, reads_allowed=True):
        runner = station.Station(GitHub("token", transport=self.fake), self.settings, now=now)
        published = []
        patches = [mock.patch.object(runner, "_lane_rows", return_value=[]),
                   mock.patch.object(runner, "_spend_shown", return_value=None),
                   mock.patch.object(runner, "_scorecard_lines", return_value=[]),
                   mock.patch.object(runner, "_remote_claim_count", return_value=0),
                   mock.patch.object(runner, "_lanes_issue", return_value=99),
                   mock.patch.object(runner, "_upsert_lanes_comment",
                                     side_effect=lambda *a: published.append(a[2]) or 1)]
        if not reads_allowed:
            patches += [mock.patch.object(runner, "_repo_pr_lines",
                                          side_effect=AssertionError("pr lines read")),
                        mock.patch.object(runner, "_repo_pr_count",
                                          side_effect=AssertionError("pr count read"))]
        for patch in patches:
            patch.start()
        try:
            runner.publish_lanes(refresh_pr_counts=refresh_pr_counts)
        finally:
            for patch in reversed(patches):
                patch.stop()
        return published, runner

    def test_early_republish_carries_counts_with_read_time_and_no_reads(self):
        first, _ = self.publish(READ_AT, True)
        self.assertIn("Open pull requests: %s 6/8" % REPO, first[0])
        later, runner = self.publish(READ_AT + 3700, False, reads_allowed=False)
        self.assertEqual(len(later), 1)
        self.assertIn("Open pull requests (read 16:07Z): %s 6/8" % REPO, later[0])
        self.assertNotIn("pending this pass", later[0])
        self.assertEqual(runner._repo_pr_counts, {})
        # The carry survives a second early republish too.
        again, _ = self.publish(READ_AT + 7400, False, reads_allowed=False)
        self.assertIn("Open pull requests (read 16:07Z): %s 6/8" % REPO, again[0])

    def test_counted_publish_after_carry_shows_fresh_counts(self):
        self.publish(READ_AT, True)
        self.publish(READ_AT + 3700, False, reads_allowed=False)
        fresh, _ = self.publish(READ_AT + 3710, True)
        self.assertEqual(len(fresh), 1)
        self.assertIn("Open pull requests: %s 6/8" % REPO, fresh[0])
        self.assertNotIn("(read ", fresh[0])

    def test_first_publish_without_stored_counts_says_pending(self):
        published, runner = self.publish(READ_AT, False, reads_allowed=False)
        self.assertIn("Open pull requests: pending this pass", published[0])
        self.assertEqual(runner._repo_pr_counts, {})


if __name__ == "__main__":
    unittest.main()
