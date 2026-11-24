"""One base-check probe per repo per pass (loom#3532).

Two open base-check incidents on one repo each got a free lane in the same
pass. Both briefs were named <repo>-base-check-<now>, so the two runners
cloned into one worktree in the same second and failed each other's clone;
on 2026-11-24 every quadseven/infra probe failed that way on two lanes."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_bus_station as tbs  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

LANES = ("codex", "spark")


class OneProbePerRepo(tbs.Base):
    open_incident = tbs.IncidentProbeTest.open_incident
    hold = tbs.IncidentProbeTest.hold
    probe = tbs.IncidentProbeTest.probe
    station = tbs.IncidentProbeTest.station

    def second_incident_holds(self, number):
        """Incident #98 on the same repo, holding ticket `number`."""
        self.fake.issues[98] = dict(self.fake.issues[99], number=98)
        self.fake.comments[98] = []
        path = os.path.join(self.home, "state", "station", "factory_held.json")
        with open(path, encoding="utf-8") as fh:
            held = json.load(fh)
        held["%s#%d" % (tbs.REPO, number)] = {"incident": "%s#98" % tbs.REPO, "at": time.time()}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(held, fh)

    def all_briefs(self):
        return [(lane, n) for lane in LANES for n in self.listing(lane) if n.endswith(".md")]

    def test_two_incidents_on_one_repo_get_one_probe_and_leave_a_lane_free(self):
        self.open_incident()
        self.hold(12, 13)
        self.second_incident_holds(13)
        free = self.probe(lanes=LANES)
        self.assertEqual(len(self.all_briefs()), 1, self.all_briefs())
        self.assertEqual(len(free), 1, free)


if __name__ == "__main__":
    unittest.main()
