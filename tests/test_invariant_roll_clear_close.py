"""Station pass regression for closing a cleared epic-over-cap roll."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import invariants, station  # noqa: E402
from test_invariants import (AUTHOR, GOAL, REPO, T0, StationBase, stamp)  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401


class RollClearStationPassTest(StationBase):
    ISSUE = 2366

    def setUp(self):
        super().setUp()
        key = invariants.Breach(invariants.EPIC_OVER_CAP, REPO, "", (), T0).key
        self.gh.issues[self.ISSUE] = {
            "number": self.ISSUE,
            "id": 9000 + self.ISSUE,
            "state": "open",
            "state_reason": None,
            "title": "Invariant: epic-over-cap in %s" % REPO,
            "body": "\n".join((
                invariants.MARKER % key,
                invariants.ROLL_MARKER,
                "Current violations: none.",
                "Clear since %s." % stamp(T0 - 7 * 3600),
            )),
            "labels": [{"name": invariants.LABEL}],
            "created_at": stamp(T0 - 10 * 3600),
        }
        self.gh.next_issue = self.ISSUE
        patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": AUTHOR})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_station_pass(self, now):
        facts = invariants.Facts(now=now, shapes=[])
        with mock.patch.object(station.Station, "_invariant_facts", return_value=facts):
            self.run_at(now)

    def test_station_closes_a_roll_clear_older_than_six_hours(self):
        self.run_station_pass(T0)

        issue = self.gh.issues[self.ISSUE]
        self.assertEqual(issue["state"], "closed", self.said)
        self.assertEqual(issue["state_reason"], "not_planned")
        comments = self.gh.comments.get((REPO, self.ISSUE), [])
        self.assertEqual(len(comments), 1)
        self.assertIn(invariants.ROLL_CLEARED_MARKER, comments[0]["body"])
        self.assertIn(invariants.CLEARED_TEXT, comments[0]["body"])

    def test_station_keeps_a_roll_clear_younger_than_six_hours_open(self):
        issue = self.gh.issues[self.ISSUE]
        issue["body"] = issue["body"].replace(
            stamp(T0 - 7 * 3600), stamp(T0 - invariants.CLEAR_AFTER + 60))

        self.run_station_pass(T0)

        self.assertEqual(issue["state"], "open", self.said)
        self.assertEqual(self.gh.comments.get((REPO, self.ISSUE), []), [])


if __name__ == "__main__":
    unittest.main()
