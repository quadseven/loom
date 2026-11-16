"""Tests for the outside-epic step resume logic using outside-epic.json.

The step processes goal homes in batches and saves progress so a station run
that processes a subset resumes on the next run within the same interval.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import station  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests.test_bus_station import FakeGitHub, REPO, AUTHORS, OWNERS  # noqa: E402


class OutsideEpicResumeTest(unittest.TestCase):
    """The outside-epic step resumes from carry-over within the interval."""

    GOAL_URL = "https://api.github.com/repos/%s/issues/1" % REPO

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "loom")
        self.fake = FakeGitHub()
        self.settings = station.Settings(self.home, [REPO], list(AUTHORS), list(OWNERS),
                                         disk_free_min_gb=0)

    def _make_goal(self, number, tickets=()):
        """Create a goal home with an intake epic and optional tickets."""
        self.fake.add_goal(number)
        self.fake.issues[number]["id"] = 100 + number
        epic = {"number": 60 + number, "id": 1060 + number, "state": "open",
                "title": station.brain.INTAKE_TITLE,
                "body": "Done when: done.\n\nSize: at most 15 tickets.\n\nPart of #%d\n" % number,
                "labels": [{"name": "epic"}]}
        self.fake.issues[60 + number] = epic
        self.fake.epics = [epic]
        self.fake.sub_issues[number] = []
        for t in tickets:
            self.fake.issues[t["number"]] = t
            self.fake.sub_issues[number].append(t)
        self.fake.sub_issues[60 + number] = []

    def _ticket(self, number, goal):
        return {"number": number, "id": 1000 + number, "state": "open",
                "title": "Ticket %d" % number,
                "body": "## Why\nwhy\n\nPart of #%d\n" % goal,
                "created_at": "2030-01-01T00:00:00Z",
                "labels": [{"name": "loom:ticket"}],
                "parent_issue_url": self.GOAL_URL}

    def _runner(self, now=None):
        return station.Station(GitHub("token", transport=self.fake), self.settings, now=now)

    def _state_path(self):
        return os.path.join(self.home, "state", "station", "outside-epic.json")

    def _read_state(self):
        import json
        try:
            with open(self._state_path(), "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _run_quiet(self, runner):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            runner.move_outside_tickets()
        return out.getvalue()

    def test_resume_after_partial_processing(self):
        """Two goal homes: first run processes one, second run processes the other."""
        self._make_goal(1, tickets=[self._ticket(10, 1)])
        self._make_goal(2, tickets=[self._ticket(20, 2)])

        now = time.time()
        runner = self._runner(now=now)
        self._run_quiet(runner)

        state = self._read_state()
        self.assertIn("homes", state)
        self.assertEqual(len(state["homes"]), 2)
        self.assertEqual(len(state["processed"]), 1)
        self.assertIn("%s#1" % REPO, state["processed"])

        runner2 = self._runner(now=now + 60)
        self._run_quiet(runner2)

        state = self._read_state()
        self.assertNotIn("homes", state)
        self.assertNotIn("processed", state)
        self.assertEqual(state["checked"], now + 60)

    def test_skip_within_interval_when_cycle_complete(self):
        """After a full cycle, subsequent runs within interval do nothing."""
        self._make_goal(1, tickets=[self._ticket(10, 1)])

        now = time.time()
        runner = self._runner(now=now)
        self._run_quiet(runner)

        runner2 = self._runner(now=now + 60)
        self._run_quiet(runner2)

        state = self._read_state()
        self.assertNotIn("homes", state)

        runner3 = self._runner(now=now + 120)
        log = self._run_quiet(runner3)

        state = self._read_state()
        self.assertEqual(state["checked"], now)
        self.assertNotIn("outside-epic: moved", log)

    def test_new_cycle_after_interval_elapsed(self):
        """After interval elapses, a new cycle starts and processes all homes."""
        self._make_goal(1, tickets=[self._ticket(10, 1)])

        now = time.time()
        runner = self._runner(now=now)
        self._run_quiet(runner)

        runner2 = self._runner(now=now + station.OUTSIDE_EPIC_EVERY)
        log = self._run_quiet(runner2)

        state = self._read_state()
        self.assertEqual(state["checked"], now + station.OUTSIDE_EPIC_EVERY)
        self.assertNotIn("homes", state)
        self.assertIn("outside-epic: moved", log)

    def test_resume_after_error_on_goal(self):
        """If a goal fails, it is marked processed and the next goal is tried."""
        self._make_goal(1, tickets=[self._ticket(10, 1)])
        self._make_goal(2, tickets=[self._ticket(20, 2)])

        now = time.time()
        runner = self._runner(now=now)

        original_move = station.brain.move_outside_tickets
        def failing_move(gh, repo, goal):
            if goal == 1:
                raise ValueError("simulated error")
            return original_move(gh, repo, goal)
        station.brain.move_outside_tickets = failing_move
        try:
            self._run_quiet(runner)
        finally:
            station.brain.move_outside_tickets = original_move

        state = self._read_state()
        self.assertIn("%s#1" % REPO, state["processed"])
        self.assertNotIn("%s#2" % REPO, state["processed"])

        runner2 = self._runner(now=now + 60)
        self._run_quiet(runner2)

        state = self._read_state()
        self.assertNotIn("homes", state)


if __name__ == "__main__":
    unittest.main()