from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import invariants  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401 - shadow off: these tests close
from test_invariants import REPO, T0, StationBase, stamp  # noqa: E402


class RollClearCloseTest(StationBase):
    def tracking_issue(self, clear_since):
        key = invariants.Breach(
            "epic-over-cap", "epic-over-cap in %s" % REPO, "", (), T0).key
        number = 2366
        self.gh.issues[number] = {
            "number": number,
            "state": "open",
            "title": "Invariant: epic-over-cap in %s" % REPO,
            "body": "\n".join([
                invariants.MARKER % key,
                invariants.ROLL_MARKER,
                invariants.ROLL_NONE,
                "Clear since %s." % stamp(clear_since),
            ]),
            "labels": [{"name": invariants.LABEL}],
            "created_at": stamp(T0),
        }
        return self.gh.issues[number]

    def test_station_closes_epic_over_cap_list_after_six_clear_hours(self):
        now = T0 + 8 * 3600
        issue = self.tracking_issue(now - 7 * 3600)
        pressure = [invariants.Breach(
            "remote-silent", "lane-%d" % index, "Lane %d is silent" % index,
            ("No heartbeat",), now) for index in range(invariants.WRITES_MAX)]

        with mock.patch.object(invariants, "check", return_value=pressure):
            self.run_at(now)

        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue["state_reason"], "not_planned")
        (note,) = self.gh.comments[(REPO, issue["number"])]
        self.assertIn(invariants.ROLL_CLEARED_MARKER, note["body"])
        self.assertIn(invariants.CLEARED_TEXT + stamp(now), note["body"])

    def test_station_keeps_epic_over_cap_list_open_before_six_clear_hours(self):
        now = T0 + 8 * 3600
        issue = self.tracking_issue(now - invariants.CLEAR_AFTER + 60)

        self.run_at(now)

        self.assertEqual(issue["state"], "open")
        self.assertNotIn((REPO, issue["number"]), self.gh.comments)


if __name__ == "__main__":
    unittest.main()
