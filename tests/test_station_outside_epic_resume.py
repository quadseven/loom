"""The outside-epic pass resumes after a carried-over read."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import brain, state_store, station  # noqa: E402
from bus.github import GitHub, TargetReached  # noqa: E402


class OutsideEpicResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = station.Settings(self.tmp.name, [], [], [], disk_free_min_gb=0)
        self.previous_store = state_store.use(state_store.FileStore())
        self.addCleanup(state_store.use, self.previous_store)
        self.homes = [("example-owner/one", {"number": 1}),
                      ("example-owner/two", {"number": 2}),
                      ("example-owner/three", {"number": 3})]

    def run_pass(self, now, move):
        runner = station.Station(GitHub("token"), self.settings, now=now)
        runner._goal_homes = lambda: self.homes
        with mock.patch.object(brain, "move_outside_tickets", side_effect=move):
            runner.move_outside_tickets()

    def read_state(self):
        path = os.path.join(self.tmp.name, "state", "station", "outside-epic.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_carried_over_pass_resumes_at_first_unfinished_goal_home(self):
        calls = []

        def first_pass(gh, repo, goal):
            calls.append((repo, goal))
            if goal == 2:
                raise TargetReached("read carried over at request target")
            return 0, 0

        with self.assertRaises(TargetReached):
            self.run_pass(1000, first_pass)
        self.assertEqual(calls, [("example-owner/one", 1), ("example-owner/two", 2)])
        self.assertNotIn("checked", self.read_state())

        calls.clear()
        self.run_pass(1060, lambda gh, repo, goal: calls.append((repo, goal)) or (0, 0))
        self.assertEqual(calls, [("example-owner/two", 2), ("example-owner/three", 3)])

    def test_a_failed_home_is_retried_when_the_pass_resumes(self):
        from bus.github import GitHubError
        calls = []

        def first_pass(gh, repo, goal):
            calls.append(goal)
            if goal == 1:
                raise GitHubError("boom")
            if goal == 2:
                raise TargetReached("read carried over at request target")
            return 0, 0

        with self.assertRaises(TargetReached):
            self.run_pass(1000, first_pass)
        path = os.path.join(self.tmp.name, "state", "station", "outside-epic.json")
        finished = self.read_state().get("finished", []) if os.path.exists(path) else []
        self.assertEqual(finished, [])

        calls.clear()
        self.run_pass(1060, lambda gh, repo, goal: calls.append(goal) or (0, 0))
        self.assertEqual(calls, [1, 2, 3])

    def test_checked_is_written_after_last_home_then_next_round_restarts(self):
        calls = []

        def move(gh, repo, goal):
            calls.append(goal)
            return 0, 0

        self.run_pass(1000, move)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(self.read_state(), {"checked": 1000, "finished": []})
        calls.clear()
        self.run_pass(1000 + station.OUTSIDE_EPIC_EVERY, move)
        self.assertEqual(calls, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
