"""A brief naming a full repairs epic files under the batch with room (loom#3206)."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
BODY = ("## Why\nA repair is needed for one small thing in the factory.\n\n## What\nChange one thing.\n\n"
        "## Acceptance criteria\n- [ ] A test shows the change.\n\n## Size\nXS\n\n## Out of scope\n- Anything else.\n")


def epic(title, total):
    return {"number": 10, "state": "open", "title": title,
            "sub_issues_summary": {"total": total}}


class BriefRepairsRolloverTest(unittest.TestCase):
    def file(self, issue):
        filed = []
        gh = GitHub("placeholder", transport=lambda *a: (200, {}, b"{}"))
        err = io.StringIO()

        def fake_issue(gh_, repo, parent, title, body, *rest):
            filed.append((parent, body))
            return {"html_url": "https://example.test/1"}

        with unittest.mock.patch.object(brain, "_open_epic_for",
                                        return_value=(issue, 1, {"number": 1, "body": ""})), \
                unittest.mock.patch.object(brain, "repairs_epic", return_value=20) as rolled, \
                unittest.mock.patch.object(brain, "_rewrite_source", return_value=(None, None)), \
                unittest.mock.patch.object(brain, "_check_no_foreign_refs"), \
                unittest.mock.patch.object(brain, "_brief_base", return_value="main"), \
                unittest.mock.patch.object(brain, "_goal_children", return_value=[]), \
                unittest.mock.patch.object(brain, "_check_goal_capacity"), \
                unittest.mock.patch.object(brain, "_check_goal_unproven"), \
                unittest.mock.patch.object(brain, "_brief_done_due", return_value=None), \
                unittest.mock.patch.object(brain, "_brief_issue", side_effect=fake_issue), \
                contextlib.redirect_stderr(err):
            brain.brief(gh, REPO, 10, "Repair one thing", BODY)
        return filed, rolled, err.getvalue()

    def test_a_full_standing_epic_rolls_to_the_batch_with_room(self):
        filed, rolled, err = self.file(epic(brain.REPAIRS_TITLE, 100))
        rolled.assert_called_once()
        self.assertEqual(rolled.call_args.kwargs["full"], (10,))
        self.assertEqual(filed[0][0], 20)
        self.assertIn("Part of #20", filed[0][1])

    def test_a_full_batch_epic_goes_to_the_next_batch(self):
        filed, rolled, _ = self.file(epic(brain.REPAIRS_PREFIX + "batch 3", 100))
        rolled.assert_called_once()
        self.assertEqual(filed[0][0], 20)

    def test_the_reroute_prints_both_epics(self):
        _, _, err = self.file(epic(brain.REPAIRS_TITLE, 100))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("#10", err)
        self.assertIn("#20", err)

    def test_an_epic_with_room_is_kept(self):
        filed, rolled, err = self.file(epic(brain.REPAIRS_TITLE, 99))
        rolled.assert_not_called()
        self.assertEqual(filed[0][0], 10)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
