"""The station hands stale live verification to a lane ticket."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, invariants, station, tickets  # noqa: E402
from bus.github import Response  # noqa: E402
from tests.test_invariants import AUTHOR, FakeGH, GOAL, REPO, stamp  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

T0 = 1_800_000_000.0
DAY = 24 * 60 * 60


class VerifyHandoffTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.gh = FakeGH()
        self.gh.api = "https://api.github.com"
        self.home = temp.name
        self.settings = station.Settings(
            self.home, [REPO], [AUTHOR], ["example-owner"],
            invariant_goal="%s#%d" % (REPO, GOAL), self_repo=REPO)
        self.source = self.shipped_ticket(100, 15 * DAY)
        original_paginate = self.gh.paginate
        original_rest = self.gh.rest

        def paginate(path):
            if re.search(r"/issues/\d+/events$", path):
                return []
            return original_paginate(path)

        self.gh.paginate = paginate

        def rest(method, path, body=None):
            labels = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)/labels", path)
            if method == "PUT" and labels:
                self.gh.calls.append((method, path))
                self.gh.writes.append((method, path, body))
                issue = self.gh.issues[int(labels.group(1))]
                issue["labels"] = [{"name": name} for name in body["labels"]]
                return Response(200, issue["labels"])
            result = original_rest(method, path, body)
            if method == "POST" and path == "/repos/%s/issues" % REPO:
                created = self.gh.issues[result.data["number"]]
                created["labels"] = [{"name": name} if isinstance(name, str) else name
                                      for name in created.get("labels", [])]
                self.gh.tickets.setdefault(REPO, []).append(created)
                self.gh.search_items.append(dict(created, repository={"full_name": REPO}))
            return result

        self.gh.rest = rest
        self.station = station.Station(self.gh, self.settings, now=T0)
        self.station._tickets_open = lambda repo: [self.source]
        self.shipped_at = T0 - 15 * DAY
        self.station._shipped_stamp = lambda repo, issue: self.shipped_at
        self.station._proven = lambda repo, number: False

    def shipped_ticket(self, number, shipped_at):
        body = ("## Why\nThe deployed change needs a live read.\n\n"
                "## Acceptance criteria\n"
                "- [ ] The deployed behavior is present.\n"
                "- [ ] Live: the service reports `ready` during the current window.\n\n"
                "## Size\nS\n\nPart of #%d\n" % GOAL)
        issue = {"number": number, "state": "open", "body": body,
                 "title": "Check deployed behavior", "user": {"login": AUTHOR},
                 "labels": [{"name": name} for name in ("loom:ticket", "loom:shipped")],
                 "created_at": stamp(shipped_at), "updated_at": stamp(shipped_at)}
        self.gh.issues[number] = issue
        self.gh.tickets.setdefault(REPO, []).append(issue)
        return issue

    def handoffs(self):
        return [issue for issue in self.gh.issues.values()
                if tickets.verify_handoff_source(issue) is not None
                and "loom:verify-handoff" in self.label_names(issue)]

    @staticmethod
    def label_names(issue):
        return [label.get("name") if isinstance(label, dict) else label
                for label in issue.get("labels", [])]

    def test_handoff_candidates_start_only_after_fourteen_days(self):
        self.station._shipped_stamp = lambda repo, issue: T0 - 14 * DAY
        self.assertEqual(self.station._find_verify_handoff_candidates(), [])
        self.station._shipped_stamp = lambda repo, issue: T0 - 14 * DAY - 1
        candidates = self.station._find_verify_handoff_candidates()
        self.assertEqual([(repo, issue["number"]) for repo, issue in candidates],
                         [(REPO, 100)])

    def test_a_proven_live_ticket_is_not_handed_off(self):
        self.station._proven = lambda repo, number: True
        self.assertEqual(self.station._find_verify_handoff_candidates(), [])

    def test_prose_live_box_without_search_needles_is_handed_off(self):
        self.source["body"] = self.source["body"].replace("`ready`", "ready")
        candidates = self.station._find_verify_handoff_candidates()
        self.assertEqual([(repo, issue["number"]) for repo, issue in candidates],
                         [(REPO, 100)])

    def test_coverage_stays_false_while_newer_verify_items_remain(self):
        self.shipped_ticket(101, DAY)
        self.station._shipped_stamp = lambda repo, issue: (
            T0 - (15 if issue["number"] == 100 else 1) * DAY)
        self.station._create_verify_handoff_ticket(REPO, self.source, self.shipped_at)
        self.station._find_verify_handoff_candidates()

        existing = self.station._verify_handoff_tickets
        self.assertEqual(existing, {"%s#100" % REPO})
        self.assertEqual(self.station._verify_due_tickets, existing | {"%s#101" % REPO})
        self.assertFalse(self.station._verify_handoff_coverage(
            existing, self.station._verify_due_tickets))

    def test_unhanded_stale_live_item_keeps_coverage_false(self):
        self.station._find_verify_handoff_candidates()
        self.assertEqual(self.station._verify_due_tickets, {"%s#100" % REPO})
        self.assertFalse(self.station._verify_handoff_coverage(
            self.station._verify_handoff_tickets, self.station._verify_due_tickets))

    def test_old_live_ticket_gets_one_linked_lane_verify_ticket(self):
        candidates = self.station._find_verify_handoff_candidates()
        facts = invariants.Facts(
            T0, verify_handoff_tickets=self.station._verify_handoff_tickets,
            verify_handoff_candidates=candidates,
            verify_due_tickets=self.station._verify_due_tickets)
        self.station._create_verify_handoffs(facts)

        (handoff,) = self.handoffs()
        self.assertEqual(handoff["state"], "open")
        self.assertIn("loom:shipped", self.label_names(handoff))
        self.assertIn("Verify handoff for #100", handoff["body"])
        self.assertIn("Part of #%d" % GOAL, handoff["body"])
        self.assertIn("loom:verify-handoff", self.label_names(handoff))
        self.assertNotIn("loom:verify-handoff", self.label_names(self.source))
        self.assertTrue(facts.verify_handoff_coverage)

    def test_next_pass_does_not_create_a_duplicate_and_resume_omits_both_tickets(self):
        self.station._create_verify_handoff_ticket(REPO, self.source, self.shipped_at)
        candidates = self.station._find_verify_handoff_candidates()
        existing = self.station._verify_handoff_tickets
        self.assertEqual(existing, {"%s#%d" % (REPO, self.source["number"])})
        self.assertEqual(self.station._verify_due_tickets, existing)
        self.assertTrue(self.station._verify_handoff_coverage(
            existing, self.station._verify_due_tickets))
        self.assertFalse(self.station._verify_handoff_coverage(
            existing, existing | {"%s#101" % REPO}))
        self.station._create_verify_handoffs(invariants.Facts(T0,
            verify_handoff_tickets=existing, verify_handoff_candidates=candidates,
            verify_due_tickets=self.station._verify_due_tickets))
        self.assertTrue(self.station._verify_handoff_coverage(
            existing, self.station._verify_due_tickets))

        self.assertEqual(len(self.handoffs()), 1)
        due = brain.verify_due(self.gh, REPO, [self.source, self.handoffs()[0]],
                               brain._stamp(stamp(T0)))
        self.assertEqual(due, [])
        station_due = station.verify_due(
            [(REPO, self.source), (REPO, self.handoffs()[0])], {}, T0,
            check_live=False, handoff_tickets={"%s#%d" % (REPO, self.source["number"])})
        self.assertEqual([issue["number"] for _, issue in station_due],
                         [self.handoffs()[0]["number"]])

    def test_stale_source_marker_does_not_hide_a_ticket_without_a_routable_child(self):
        self.station._create_verify_handoff_ticket(REPO, self.source, self.shipped_at)
        handoff = self.handoffs()[0]
        self.source["labels"].append({"name": "loom:verify-handoff"})
        handoff["labels"] = [label for label in handoff["labels"]
                             if label["name"] != "loom:shipped"]

        due = brain.verify_due(self.gh, REPO, [self.source, handoff], brain._stamp(stamp(T0)))
        self.assertEqual([issue["number"] for _, issue in due], [self.source["number"]])
        station_due = station.verify_due(
            [(REPO, self.source), (REPO, handoff)], {}, T0, check_live=False)
        self.assertEqual([issue["number"] for _, issue in station_due],
                         [self.source["number"]])

    def test_station_verify_pool_dispatches_the_child_and_not_its_source(self):
        self.station._create_verify_handoff_ticket(REPO, self.source, self.shipped_at)
        handoff = self.handoffs()[0]
        self.station._tickets_open = lambda repo: [self.source, handoff]
        self.station._issue = lambda repo, number: (
            self.source if number == self.source["number"] else handoff)
        self.station._audit_holds = lambda: set()
        self.station._verify_refused_ledger = lambda: {}
        self.station._verify_missing_ledger = lambda: {}
        self.station._verify_proof_state = lambda repo, number: False
        self.station._verify_has_merge = lambda repo, issue: True
        self.station._unmeetable_live = lambda repo, issue, reads: False
        self.station._paginate = lambda path: []

        pool, _ = self.station._verify_pool({}, queued_holds=False)
        self.assertEqual([issue["number"] for _, issue in pool], [handoff["number"]])

    def test_verify_due_omits_live_items_that_need_a_log_read_without_evidence(self):
        # The source's Live criterion names `ready`: a log read the lane-machine
        # brain tick cannot make without LOOM_EVIDENCE_CMDS, so verify_due
        # leaves it out when the environment names no evidence command.
        self.station._create_verify_handoff_ticket(REPO, self.source, self.shipped_at)
        due = brain.verify_due(self.gh, REPO, [self.source], brain._stamp(stamp(T0)), env={})
        self.assertEqual(due, [])
        # With evidence access named, or with no env given, it is verify-due.
        due = brain.verify_due(self.gh, REPO, [self.source], brain._stamp(stamp(T0)),
                               env={"LOOM_EVIDENCE_CMDS": '{"%s":"echo"}' % REPO})
        self.assertEqual([issue["number"] for _, issue in due], [self.source["number"]])
        due = brain.verify_due(self.gh, REPO, [self.source], brain._stamp(stamp(T0)))
        self.assertEqual([issue["number"] for _, issue in due], [self.source["number"]])

    def test_lane_proof_is_applied_to_the_source_ticket(self):
        self.station._create_verify_handoff_ticket(REPO, self.source, self.shipped_at)
        handoff = self.handoffs()[0]
        applied = []
        self.station._issue = lambda repo, number: (
            self.source if number == self.source["number"] else handoff)
        self.station._verify_clear = lambda lane: None
        self.station._read_verify = lambda lane, name: ({"criteria": []}, "")
        self.station._verify_model = lambda lane, name: "example-model"
        self.station._adapter_can_verify = lambda repo, issue: False
        self.station._apply_proof = lambda lane, repo, number, result, model: applied.append(number)
        self.station._settle_verify_handoff = lambda repo, number, source: None

        self.station._report_verify("codex", "done", REPO, handoff["number"], "verify-run")
        self.assertEqual(applied, [self.source["number"]])


if __name__ == "__main__":
    unittest.main()
