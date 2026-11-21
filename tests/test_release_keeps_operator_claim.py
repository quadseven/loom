"""A release from an incident hold keeps an operator session's claim (loom#2975)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_bus_station as base  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO, labeled = base.REPO, base.labeled


def claimed(login, when):
    event = labeled("claim:" + login, login)
    event["created_at"] = when
    return event


class ReleaseKeepsOperatorClaim(base.FactoryNoIncidentHoldTest):
    NOW = 1700000000.0
    BEFORE = "2023-11-14T18:00:00Z"
    AFTER = "2023-11-14T22:00:00Z"

    def claim(self, when):
        self.fake.issues[12]["labels"].append({"name": "claim:alice-example"})
        self.fake.events[12] = [claimed("alice-example", when)]

    def test_a_claim_added_after_the_hold_began_is_kept_and_not_requeued(self):
        self.held(at=self.NOW - 3 * 3600)
        self.claim(self.AFTER)
        self.release(self.NOW)
        names = self.fake.names(12)
        self.assertIn("claim:alice-example", names)
        self.assertNotIn("loom:queued", names)
        self.assertNotIn("%s#12" % REPO, self.held_entry() or {})
        self.assertIn("operator session claimed", self.status_boxes())

    def test_a_claim_left_from_before_the_hold_is_stripped_as_before(self):
        self.held(at=self.NOW - 3 * 3600)
        self.claim(self.BEFORE)
        self.release(self.NOW)
        names = self.fake.names(12)
        self.assertIn("loom:queued", names)
        self.assertNotIn("claim:alice-example", names)


if __name__ == "__main__":
    unittest.main()
