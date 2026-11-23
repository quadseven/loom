"""A queued ticket an operator session claims by label gets no lane (loom#3466)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_bus_station as base  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO, GitHub, station = base.REPO, base.GitHub, base.station


def factory(test):
    runner = station.Station(GitHub("token", transport=test.fake), test.settings)
    return runner._load_factory()


class DispatchSkipsOperatorClaim(base.FactoryNoIncidentHoldTest):
    def setUp(self):
        super().setUp()
        self.fake.default_body = base.BODY_WITH_CRITERIA

    def queued_claimed(self):
        self.fake.add(12, state="queued")
        self.fake.issues[12]["labels"].append({"name": "claim:alice-example"})
        return self.fake.issues[12]

    def test_a_claimed_queued_ticket_gets_no_lane(self):
        issue = self.queued_claimed()
        reason = factory(self)._assign_queued_ticket(REPO, issue)
        self.assertTrue(reason)
        names = self.fake.names(12)
        self.assertIn("loom:queued", names)
        self.assertNotIn("loom:briefed", names)
        self.assertEqual(self.fake.paths("PUT") + self.fake.paths("POST"), [])

    def test_the_wait_reason_names_the_claim_label(self):
        issue = self.queued_claimed()
        reason = factory(self)._assign_queued_ticket(REPO, issue)
        self.assertIn("claim:alice-example", reason)


if __name__ == "__main__":
    unittest.main()
