from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import invariants, station  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_invariants import GOAL, REPO as FACTORY_REPO, T0, StationBase, epic  # noqa: E402

TARGET_REPO = "example-owner/target"


class RollClearTest(StationBase):
    def setUp(self):
        super().setUp()
        self.settings.repos = [FACTORY_REPO, TARGET_REPO]
        self.gh.goal_issues[TARGET_REPO] = [
            {"number": GOAL, "title": "Target goal", "labels": [{"name": "loom:goal"}],
             "sub_issues_summary": {"total": 40}},
        ]

    def test_epic_title_shared_tracking_issue_rewrites_to_none_when_shapes_read(self):
        """When shapes are read and epic-title-shared breach is gone for a target repo,
        the tracking issue's list is rewritten to 'none' on that pass."""
        # Set up two epics with the same title in TARGET_REPO (breach)
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: shared title"),
            dict(epic(41), title="Epic: shared  title"),  # extra space, same normalized title
            epic(42)
        ]
        self.run_at(T0)
        filed = self.gh.filed()
        self.assertEqual(len(filed), 1)
        self.assertEqual(filed[0]["title"], "Invariant: epic-title-shared in %s" % TARGET_REPO)
        self.assertIn("Open epics with one title", filed[0]["body"])
        self.assertIn("issue 40", filed[0]["body"])
        self.assertIn("issue 41", filed[0]["body"])
        issue_number = filed[0]["number"]

        # Fix: make titles distinct in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: first batch"),
            dict(epic(41), title="Epic: second batch"),
            epic(42)
        ]
        # Run again with shapes read - the tracking issue should be rewritten to "none"
        def other_breaches(facts, say):
            self.assertIsNotNone(facts.shapes)
            shape = next(shape for shape in facts.shapes if shape.goal.key ==
                         "%s#%d" % (TARGET_REPO, GOAL))
            self.assertEqual([item.title for item in shape.epics],
                             ["Epic: first batch", "Epic: second batch", "Epic: part 42"])
            return [invariants.Breach("budget-probe", "budget-probe-%d" % n,
                                      "Budget probe %d" % n, (), T0)
                    for n in range(invariants.WRITES_MAX)]

        with mock.patch.object(invariants, "check", side_effect=other_breaches):
            self.run_at(T0 + invariants.CHECK_EVERY)
        # The issue should still exist but its body should now say "none"
        updated = self.gh.issues[issue_number]
        self.assertEqual(updated["state"], "open")
        self.assertIn(invariants.ROLL_NONE, updated["body"], "Tracking issue should list no violation now")

    def test_epic_title_shared_tracking_issue_closes_after_clear_wait(self):
        """Once the tracking issue has said 'none' for CLEAR_AFTER, it closes."""
        # Set up breach in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: shared title"),
            dict(epic(41), title="Epic: shared  title"),
        ]
        self.run_at(T0)
        filed = self.gh.filed()
        issue_number = filed[0]["number"]

        # Fix titles in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: first"),
            dict(epic(41), title="Epic: second"),
        ]
        # First clear pass: rewrites to "none"
        self.run_at(T0 + invariants.CHECK_EVERY)
        updated = self.gh.issues[issue_number]
        self.assertIn(invariants.ROLL_NONE, updated["body"])
        self.assertEqual(updated["state"], "open")

        # After CLEAR_AFTER: closes
        self.run_at(T0 + invariants.CHECK_EVERY + invariants.CLEAR_AFTER)
        self.assertEqual(self.gh.issues[issue_number]["state"], "closed", self.said)
        notes = self.gh.comments[(FACTORY_REPO, issue_number)]
        self.assertTrue(any(invariants.CLEARED_TEXT in note["body"] for note in notes), notes)

    def test_epic_title_shared_tracking_issue_stays_open_when_shapes_not_read(self):
        """When shapes are NOT read (facts.shapes is None), the tracking issue
        is left as-is and not rewritten to 'none'."""
        # Set up breach in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: shared title"),
            dict(epic(41), title="Epic: shared  title"),
        ]
        self.run_at(T0)
        filed = self.gh.filed()
        issue_number = filed[0]["number"]
        original_body = filed[0]["body"]

        # Fix titles in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: first"),
            dict(epic(41), title="Epic: second"),
        ]

        # Simulate shapes not being read by mocking _invariant_facts to return shapes=None
        with mock.patch.object(station.Station, "_invariant_facts") as mock_facts:
            facts = invariants.Facts(now=T0 + invariants.CHECK_EVERY)
            facts.shapes = None  # Not read
            facts.unread = {}  # No unread repos for epic-title-shared
            mock_facts.return_value = facts
            self.run_at(T0 + invariants.CHECK_EVERY)

        # The tracking issue should NOT be rewritten (shapes not read)
        updated = self.gh.issues[issue_number]
        self.assertEqual(updated["body"], original_body, "Tracking issue unchanged when shapes not read")
        self.assertEqual(updated["state"], "open", "Tracking issue stays open when shapes not read")
        self.assertNotIn(invariants.ROLL_NONE, updated["body"])


if __name__ == "__main__":
    unittest.main()
