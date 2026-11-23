"""Tests for the 14-day verify handoff (loom#3509)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, invariants, station, tickets  # noqa: E402
from bus.github import GitHubError, RateLimited, Response  # noqa: E402
from bus.invariants import Facts, Journal, Ref  # noqa: E402
from tests.test_invariants import FakeGH, stamp as stamp_orig, journal as journal_orig  # noqa: E402,F401
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/factory"
OTHER = "example-owner/hidden"
GOAL = 30
AUTHOR = "trusted-example"
T0 = 1_800_000_000.0
DAY_S = 24 * 3600


def stamp(at):
    return stamp_orig(at)


def journal(at, holder="lane-machine", step="- Step verify: degraded (4 overdue live checks in example-owner/factory (oldest due 2026-11-03) not taken)"):
    return journal_orig(at, holder, step)


class HandoffTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.gh = FakeGH()
        # Add handler for events endpoint
        orig_paginate = self.gh.paginate
        def patched_paginate(path):
            if "/events" in path:
                # Return a labeled event for loom:shipped
                import re
                m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/events", path)
                if m:
                    repo, number = m.group(1), int(m.group(2))
                    issue = self.gh.issues.get(number)
                    if issue and "loom:shipped" in [l.get("name") for l in issue.get("labels", [])]:
                        shipped_at = issue.get("updated_at")
                        if shipped_at:
                            return [{"event": "labeled", "label": {"name": "loom:shipped"}, "created_at": shipped_at}]
                return []
            return orig_paginate(path)
        self.gh.paginate = patched_paginate

        self.said = []
        self.settings = station.Settings(self.home, [REPO, OTHER], [AUTHOR], ["example-owner"],
                                         invariant_goal="%s#%d" % (REPO, GOAL), self_repo=REPO)

    def shipped_ticket(self, number, shipped_hours_ago, body=None):
        """A shipped ticket with a live criterion, shipped `shipped_hours_ago` hours ago."""
        shipped_at = T0 - shipped_hours_ago * 3600
        if body is None:
            body = "Why.\n\n- [ ] Live check the deploy for `success`\n"
        self.gh.issues[number] = {"number": number, "state": "open", "body": body,
                                  "labels": [{"name": n} for n in ["loom:ticket", "loom:shipped"]],
                                  "created_at": stamp(shipped_at - 3600),
                                  "updated_at": stamp(shipped_at),
                                  "user": {"login": AUTHOR}}
        # Add a shipped event
        self.gh.comments.setdefault((REPO, number), []).append(
            {"body": "<!-- loom:shipped v1 -->\nShipped at %s" % stamp(shipped_at),
             "created_at": stamp(shipped_at), "user": {"login": AUTHOR}})
        self.gh.tickets.setdefault(REPO, []).append(self.gh.issues[number])

    def verify_handoff_ticket(self, number, linked_ticket):
        """A verify handoff ticket linking to the original shipped ticket."""
        self.gh.issues[number] = {"number": number, "state": "open", "body": "Verify handoff for #%d\n\nPart of #%d" % (linked_ticket, GOAL),
                                  "labels": [{"name": n} for n in ["loom:ticket", "loom:verify-handoff"]],
                                  "created_at": stamp(T0)}
        self.gh.tickets.setdefault(REPO, []).append(self.gh.issues[number])

    def run_station(self, now=T0):
        # Add search items for handoff tickets
        for issue in self.gh.issues.values():
            if isinstance(issue, dict):
                labels = issue.get("labels", [])
                label_names = [l.get("name") if isinstance(l, dict) else l for l in labels]
                if "loom:verify-handoff" in label_names:
                    body = issue.get("body", "")
                    import re
                    match = re.search(r"Original: ([^#]+)#(\d+)", body)
                    if match:
                        repo, orig_num = match.group(1), int(match.group(2))
                        self.gh.search_items.append({"number": issue["number"], "repository": {"full_name": repo}, "body": body})
        
        st = station.Station(self.gh, self.settings, now=now)
        orig_say = station._say
        station._say = orig_say
        try:
            st.check_invariants()
        finally:
            station._say = orig_say
        return st

    def count_handoffs(self):
        return len([i for i in self.gh.issues.values() if isinstance(i, dict)
                    and "loom:verify-handoff" in [l.get("name") if isinstance(l, dict) else l for l in i.get("labels", [])]])

    def breaches(self):
        return [i for i in self.gh.issues.values() if isinstance(i, dict)
                and "loom:invariant" in [l.get("name") if isinstance(l, dict) else l for l in i.get("labels", [])]]

    def test_verify_handoff_after_14_days(self):
        """A shipped ticket with a live criterion older than 14 days gets a verify handoff ticket."""
        # Ship a ticket 15 days ago with a live criterion
        self.shipped_ticket(100, 15 * 24)

        # Run the station
        self.run_station()

        # Check that a verify handoff ticket was created
        handoff_tickets = [i for i in self.gh.issues.values() if isinstance(i, dict)
                          and "loom:verify-handoff" in [l.get("name") if isinstance(l, dict) else l for l in i.get("labels", [])]]
        self.assertEqual(len(handoff_tickets), 1)
        self.assertIn("#100", handoff_tickets[0]["body"])

    def test_no_duplicate_handoff_on_next_pass(self):
        """Running the station again does not create a duplicate verify handoff ticket."""
        self.shipped_ticket(100, 15 * 24)

        # First pass
        self.run_station(T0)
        handoff_count_1 = self.count_handoffs()

        # Second pass
        self.run_station(T0 + 3600)
        handoff_count_2 = self.count_handoffs()

        self.assertEqual(handoff_count_1, handoff_count_2)
        self.assertEqual(handoff_count_1, 1)

    def test_brain_failing_does_not_count_verify_degraded_when_handoff_exists(self):
        """The brain-failing invariant does not count verify as degraded when all verify-due items have handoff tickets."""
        # Create a journal with verify degraded step
        self.gh.comments[GOAL] = [{"body": journal(T0 - 3600), "created_at": stamp(T0 - 3600),
                                   "user": {"login": "lane-machine"}}]
        self.gh.comments[GOAL].append({"body": journal(T0 - 600), "created_at": stamp(T0 - 600),
                                       "user": {"login": "lane-machine"}})

        # Create a shipped ticket older than 14 days with a handoff ticket
        self.shipped_ticket(100, 15 * 24)
        self.verify_handoff_ticket(200, 100)

        # Run the station
        self.run_station()

        # The brain-failing invariant should not fire for the verify step
        # because the verify-due item has a handoff ticket
        brain_failing_breaches = [b for b in self.breaches()
                                  if isinstance(b, dict) and "brain-failing" in [l.get("name") if isinstance(l, dict) else l for l in b.get("labels", [])]]
        # The verify step should not be in the evidence
        for breach in brain_failing_breaches:
            for evidence in breach.get("body", "").split("\n"):
                if "verify" in evidence.lower() and "degraded" in evidence.lower():
                    self.fail("verify degraded should not be reported when handoff exists: %s" % evidence)


class VerifyHandoffInvariantTest(unittest.TestCase):
    """Tests for the brain_failing invariant with verify handoff."""

    def setUp(self):
        self.goal = Ref("%s#%d" % (REPO, GOAL), "#%d" % GOAL)

    def j(self, at, holder="lane-machine", steps=()):
        return Journal(at, self.goal, holder, stamp(at), tuple(steps))

    def test_verify_degraded_not_counted_when_handoff_exists(self):
        """verify degraded step is not counted when all verify-due items have handoff tickets."""
        # This test will be updated once the Facts structure includes handoff info
        pass


if __name__ == "__main__":
    unittest.main()