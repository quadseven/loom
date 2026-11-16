"""Test for the bug where a factory hold recorded AFTER the incident closed
never gets released, because _claimed_since treats the hold event itself as
a claim newer than the incident close."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendar
import json
import time
import tempfile
import unittest
from unittest import mock

from bus import station, status, tickets
from bus.github import GitHub, GitHubError
from tests.test_bus_station import Base, REPO, PLAIN, AUTHORS, OWNERS, BODY


class FactoryHeldClosedFirstTest(Base):
    """The hold's 'at' timestamp is newer than the incident's closed_at.
    The ticket should be re-queued, not kept in loom:running."""

    def setUp(self):
        super().setUp()
        # Ensure station uses our test repo - override disk_free_min_gb=0
        self.settings = station.Settings(self.home, [REPO], list(AUTHORS), list(OWNERS),
                                         disk_free_min_gb=0)

    def incident(self, closed_at="2030-01-01T00:10:00Z", labels=None):
        if labels is None:
            labels = [{"name": "loom:factory"}]
        self.fake.factory_incidents = [{
            "number": 99,
            "title": "Factory incident: %s validation environment" % REPO,
            "state": "closed",
            "closed_at": closed_at,
            "body": ("<!-- loom:factory-incident v1 -->\n\nRepo: %s\nKind: environment\n"
                     "Ticket: %s#12\n\ncheck: make check (exit 127)\n" % (REPO, REPO)),
            "labels": labels}]
        self.fake.issues[99] = dict(self.fake.factory_incidents[0])

    def held_with_at(self, hold_at_epoch, body_events=()):
        """Record a hold with a specific epoch timestamp in factory_held.json"""
        issue = self.fake.issues[12]
        issue["labels"] = [{"name": "loom:ticket"}, {"name": "lane:muse"},
                           {"name": "loom:running"}, {"name": "wait:incident"}]
        if body_events:
            issue["body"] = status.with_body_events(issue.get("body", ""), list(body_events))
        path = os.path.join(self.home, "state", "station", "factory_held.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"%s#12" % REPO: {"incident": "%s#99" % REPO, "at": hold_at_epoch}}, fh)

    def test_hold_recorded_after_incident_closed_should_release(self):
        """Incident closed at 00:10Z, hold recorded at 00:20Z (after close).
        The ticket should be re-queued because the incident is closed,
        not kept in loom:running because the hold timestamp is newer."""
        self.fake.add(12, lane="muse", body=PLAIN, state="running")
        
        # Incident closed at 00:10Z
        closed_at = "2030-01-01T00:10:00Z"
        self.incident(closed_at=closed_at)
        self.fake.issues[99] = dict(self.fake.factory_incidents[0])
        
        # Hold recorded at 00:20Z (AFTER incident closed)
        hold_epoch = station.Station._epoch("2030-01-01T00:20:00Z")
        self.held_with_at(hold_epoch, body_events=[
            ("2030-01-01T00:20:00Z", "Lane muse held: factory incident %s#99" % REPO)
        ])
        
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            station.Station(GitHub("token", transport=self.fake), self.settings).release_factory_held()
        
        # Should be re-queued, not kept in loom:running
        self.assertIn("loom:queued", self.fake.names(12),
            "Ticket should be re-queued when incident closed before hold was recorded")

    def test_hold_recorded_before_incident_closed_releases(self):
        """Incident closed at 00:20Z, hold recorded at 00:10Z (before close).
        This is the working case - should release."""
        self.fake.add(12, lane="muse", body=PLAIN, state="running")
        
        # Incident closed at 00:20Z
        closed_at = "2030-01-01T00:20:00Z"
        self.incident(closed_at=closed_at)
        self.fake.issues[99] = dict(self.fake.factory_incidents[0])
        
        # Hold recorded at 00:10Z (BEFORE incident closed)
        hold_epoch = station.Station._epoch("2030-01-01T00:10:00Z")
        self.held_with_at(hold_epoch, body_events=[
            ("2030-01-01T00:10:00Z", "Lane muse held: factory incident %s#99" % REPO)
        ])
        
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            station.Station(GitHub("token", transport=self.fake), self.settings).release_factory_held()
        
        self.assertIn("loom:queued", self.fake.names(12))


if __name__ == "__main__":
    unittest.main()