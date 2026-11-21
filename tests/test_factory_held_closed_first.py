"""A ticket held behind an incident that closed before the hold was recorded
is released: the claim that ran into the hold is older than the hold."""
from __future__ import annotations

import calendar
import json
import os
import time
import unittest
from unittest import mock

from bus import station
from bus.github import GitHub
from tests import test_bus_station as base
from shadow_env import setUpModule, tearDownModule  # noqa: F401

REPO = base.REPO
CLAIM = "Claimed for lane muse (body sha256 0123456789ab)"


def epoch(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


class ClosedFirstTest(base.Base):
    incident = base.FactoryIncidentTest.incident
    held_directly = base.FactoryIncidentTest.held_directly

    def release(self, claimed_at, held_at):
        self.fake.add(12, lane="muse", body=base.PLAIN, state="running")
        self.held_directly(body_events=[
            ("2030-01-01T00:00:00Z", "Lane muse held: factory incident %s#99" % REPO),
            (claimed_at, CLAIM)])
        self.incident(closed_at="2030-01-01T00:10:00Z")
        path = os.path.join(self.home, "state", "station", "factory_held.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"%s#12" % REPO: {"incident": "%s#99" % REPO,
                                       "at": epoch(held_at)}}, fh)
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            station.Station(GitHub("token", transport=self.fake),
                            self.settings).release_factory_held()
        return path

    def test_a_hold_recorded_after_the_close_is_released(self):
        # Closed 00:10, claimed 00:12, held 00:15: the claim is the run that
        # met the hold, not a lane picking the ticket up after the close.
        path = self.release("2030-01-01T00:12:00Z", "2030-01-01T00:15:00Z")
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertNotIn("loom:running", self.fake.names(12))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {})

    def test_a_claim_after_the_hold_still_keeps_the_ticket_running(self):
        self.release("2030-01-01T00:20:00Z", "2030-01-01T00:15:00Z")
        self.assertIn("loom:running", self.fake.names(12))


if __name__ == "__main__":
    unittest.main()
