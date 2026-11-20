"""A fix round that timed out before it pushed is not the same failures coming
back: the next round goes to the same lane (bus/station.py)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bus import station  # noqa: E402
import test_bus_station  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

TIMEOUT = ("Lane claude failed: [transient] agent exited 124; the edits are on branch "
           "loom/12-fix-r1-timedout-1700000000")


class TimedOutRoundTest(test_bus_station.StuckFixRoundTest):
    def test_a_timed_out_round_that_pushed_nothing_stays_on_its_lane(self):
        self.last_round("### Check: pytest\n\n    1 failed")
        self.gh.comments.append({
            "id": 901, "user": {"login": "alice-example"},
            "created_at": "2030-01-01T01:00:00Z", "body": TIMEOUT})
        with mock.patch.object(station.Station, "_stop_fix_rounds") as stopped:
            self.run_hand_back(checks=[("pytest", "1 failed, on a new run id")])
        stopped.assert_not_called()
        self.assertIn("lane:spark", self.names())
        self.assertNotIn("lane:claude", self.names())
        self.assertIn("loom:briefed", self.names())
        self.assertEqual(len(self.rounds()), 2)


for _name in [n for n in dir(TimedOutRoundTest)
              if n.startswith("test_") and n != "test_a_timed_out_round_that_pushed_nothing_stays_on_its_lane"]:
    setattr(TimedOutRoundTest, _name, None)

if __name__ == "__main__":
    unittest.main()
