"""A base-check probe waits for the repo's default branch to move (loom#3324).

A probe on the head the last probe ran on can only fail again and takes a free
lane from real work."""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_bus_station as tbs  # noqa: E402
from bus import incident_probe, station  # noqa: E402
from bus.github import GitHubError  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

LATER = incident_probe.PROBE_EVERY + 60


class SameBaseProbe(tbs.Base):
    open_incident = tbs.IncidentProbeTest.open_incident
    hold = tbs.IncidentProbeTest.hold
    briefs = tbs.IncidentProbeTest.briefs

    def probe_at(self, head, later=0, lanes=("codex",)):
        runner = station.Station(tbs.GitHub("token", transport=self.fake),
                                 self.settings, now=time.time() + later)
        with mock.patch.object(station, "FACTORY_REPO", tbs.REPO), \
                mock.patch.object(station.Station, "_default_head", return_value=head):
            return runner._probe_incidents(list(lanes))

    def clear_briefs(self):
        for name in self.briefs():
            os.remove(self.inbox("codex", name))

    def test_a_probe_waits_for_the_head_to_move_and_a_failed_read_probes(self):
        self.open_incident()
        self.hold(12)
        self.assertEqual(self.probe_at("a" * 40), [])
        self.assertEqual(len(self.briefs()), 1)
        self.clear_briefs()
        # Past the hour with the default branch still on the probed head.
        self.assertEqual(self.probe_at("a" * 40, LATER), ["codex"])
        self.assertEqual(self.briefs(), [])
        # The head moved: one probe.
        self.assertEqual(self.probe_at("b" * 40, LATER), [])
        self.assertEqual(len(self.briefs()), 1)
        self.clear_briefs()
        # An unreadable head probes as before.
        self.assertEqual(self.probe_at("", 2 * LATER), [])
        self.assertEqual(len(self.briefs()), 1)

    def test_a_head_read_that_fails_is_an_empty_head(self):
        runner = station.Station(tbs.GitHub("token", transport=self.fake), self.settings)
        with mock.patch.object(runner.gh, "rest", side_effect=GitHubError("boom")):
            self.assertEqual(runner._default_head(tbs.REPO), "")


if __name__ == "__main__":
    unittest.main()
