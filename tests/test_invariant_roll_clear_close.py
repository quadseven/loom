"""Cleared tracking issues must not wait behind unrelated new breaches."""
from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import invariants, station
from bus.github import GitHubError
from shadow_env import setUpModule, tearDownModule  # noqa: F401
from test_invariants import AUTHOR, GOAL, REPO, T0, StationBase, epic, stamp, ticket


class RollClearCloseTest(StationBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": AUTHOR})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Each goal has too many epics, but every epic is empty. The real
        # station reads produce enough unrelated breaches to spend its budget.
        self.gh.goal_issues[REPO] = []
        for index in range(invariants.WRITES_MAX):
            goal = GOAL + index
            self.gh.goal_issues[REPO].append({
                "number": goal, "title": "Goal: group %d" % index,
                "labels": [{"name": "loom:goal"}], "body": ""})
            self.gh.subs[(REPO, goal)] = [
                dict(epic(200 + index * 10 + n), body="Part of #%d" % goal)
                for n in range(7)]
        self.number = 90
        self.key = invariants.Breach(
            "epic-over-cap", "epic-over-cap in %s" % REPO, "", (), T0).key
        self.file_clear(7 * 3600)

    def file_clear(self, age):
        self.issue = {
            "number": self.number, "id": 9000 + self.number, "state": "open",
            "title": "Invariant: epic-over-cap in %s" % REPO,
            "labels": [{"name": invariants.LABEL}],
            "body": "\n".join((invariants.MARKER % self.key, invariants.ROLL_MARKER,
                                invariants.ROLL_NONE,
                                invariants.CLEAR_SINCE % stamp(T0 - age),
                                "Part of #%d" % GOAL))}
        self.gh.issues[self.number] = self.issue

    def run_pass(self, unread=None):
        read_facts = station.Station._invariant_facts
        run_filer = invariants.Filer.run
        filers = []

        def facts(host, state):
            # settle_early has no current facts and must not close the roll.
            self.assertEqual(self.issue["state"], "open")
            result = read_facts(host, state)
            if unread is not None:
                result.unread["epic-over-cap"] = unread
            return result

        def run(filer, breaches):
            filers.append(filer)
            run_filer(filer, breaches)

        with mock.patch.object(station.Station, "_invariant_facts", facts), \
                mock.patch.object(invariants.Filer, "run", run):
            host = self.run_at(T0)
        self.assertEqual(len(filers), 1)
        self.assertLessEqual(filers[0].writes, invariants.WRITES_MAX)
        return host

    def assert_open_without_clear_note(self):
        self.assertEqual(self.issue["state"], "open", self.said)
        self.assertFalse(any(invariants.ROLL_CLEARED_MARKER in c["body"]
                             for c in self.gh.comments.get((REPO, self.number), [])))

    def test_seven_hour_clear_closes_before_unrelated_filings_spend_budget(self):
        host = self.run_pass()
        self.assertEqual(len(invariants.goal_epics_over_cap(host._read_facts)),
                         invariants.WRITES_MAX)
        self.assertEqual(invariants.epic_over_cap(host._read_facts), [])
        self.assertEqual(self.issue["state"], "closed", self.said)
        self.assertEqual(self.issue["state_reason"], "not_planned")
        notes = self.gh.comments[(REPO, self.number)]
        self.assertEqual(len(notes), 1)
        self.assertIn(invariants.ROLL_CLEARED_MARKER, notes[0]["body"])
        self.assertTrue(any("closed cleared tracking issue" in line for line in self.said))
        self.assertTrue(any(i["title"].startswith("Goal home")
                            for i in self.gh.issues.values()))

    def test_under_six_hours_stays_open_in_same_busy_pass(self):
        self.file_clear(invariants.CLEAR_AFTER - 1)
        self.run_pass()
        self.assert_open_without_clear_note()

    def test_exactly_six_hours_closes(self):
        self.file_clear(invariants.CLEAR_AFTER)
        self.run_pass()
        self.assertEqual(self.issue["state"], "closed", self.said)

    def test_unread_shapes_do_not_close_a_cleared_tracker(self):
        with mock.patch.object(station.Station, "_goal_shape",
                               side_effect=GitHubError("unavailable", 502)):
            host = self.run_pass()
        self.assertIsNone(host._read_facts.shapes)
        self.assert_open_without_clear_note()

    def test_unread_repo_does_not_close_a_cleared_tracker(self):
        self.run_pass(unread=(REPO,))
        self.assert_open_without_clear_note()

    def test_returning_violation_is_not_closed_as_clear(self):
        self.gh.subs[(REPO, 200)] = [ticket(n) for n in range(400, 416)]
        host = self.run_pass()
        self.assertEqual(len(invariants.epic_over_cap(host._read_facts)), 1)
        self.assert_open_without_clear_note()

    def test_ticket_rewritten_after_listing_is_not_closed_as_a_roll(self):
        rest = self.gh.rest

        def fresh_ticket(method, path, body=None):
            if method == "GET" and path == "/repos/%s/issues/%d" % (REPO, self.number):
                self.issue["labels"] = [{"name": invariants.LABEL}, {"name": "loom:ticket"}]
            return rest(method, path, body)

        with mock.patch.object(self.gh, "rest", fresh_ticket):
            self.run_pass()
        self.assert_open_without_clear_note()

    def test_write_gate_keeps_tracker_open(self):
        with mock.patch.object(station.Station, "_invariant_write_gate", return_value="budget"):
            self.run_pass()
        self.assert_open_without_clear_note()
        self.assertEqual(self.gh.writes, [])
