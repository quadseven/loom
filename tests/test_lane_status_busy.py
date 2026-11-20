"""A local lane in the middle of a run reads busy, not off with a stale
heartbeat: the runner beats between runs but stamps its keepalive during one."""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bus import lane_state, station  # noqa: E402
from bus.github import GitHub  # noqa: E402
from test_bus_station import Base  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401


class BusyLaneTests(Base):
    def station(self):
        return station.Station(GitHub("token", transport=self.fake), self.settings,
                               now=time.time())

    def stamp_alive(self, lane, age):
        d = os.path.join(self.home, "state", "usage")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, lane + ".alive"), "w") as fh:
            fh.write("%d\n" % (time.time() - age))

    def row(self, lane):
        return next(r for r in self.station()._lane_rows() if r[0] == lane)

    def test_a_stale_heartbeat_with_a_fresh_keepalive_reads_busy(self):
        self.heartbeat("codex", age=40 * 60)
        self.stamp_alive("codex", age=30)
        name, state, _, reason = self.row("codex")
        self.assertEqual((state, reason), ("busy", "Run in progress"))
        # A busy lane is not offered new work.
        self.assertFalse(self.station()._heartbeat_ok("codex"))
        # A stale keepalive stamp keeps the old reading.
        self.stamp_alive("codex", age=station.ALIVE_MAX_AGE + 60)
        _, state, _, reason = self.row("codex")
        self.assertEqual((state, reason), ("off", "Heartbeat is stale"))

    def test_a_published_busy_row_is_read_back(self):
        line = "| codex | busy | 2026-01-01 00:00:00Z | Run in progress |"
        (row,) = lane_state.published_rows(line)
        self.assertEqual((row["state"], row["available"]), ("busy", False))


if __name__ == "__main__":
    unittest.main()
