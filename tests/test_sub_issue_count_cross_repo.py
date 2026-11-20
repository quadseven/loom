"""sub_issue_count reads an epic's sub-issues in the epic's own repo."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402

HOME = "example-owner/goal-home"
OTHER = "example-owner/ticket-home"


def _count(epic):
    gh = mock.Mock()
    gh.paginate.return_value = [{"number": 1}, {"number": 2}]
    return brain.sub_issue_count(gh, HOME, epic), gh


class SubIssueCountCrossRepoTest(unittest.TestCase):
    def test_a_cross_repo_epic_is_read_in_its_own_repo(self):
        epic = {"number": 7, "repository_url": "https://api.example.invalid/repos/" + OTHER}
        count, gh = _count(epic)
        self.assertEqual(count, 2)
        gh.paginate.assert_called_once_with(
            "/repos/%s/issues/7/sub_issues?per_page=100" % OTHER)

    def test_an_epic_in_the_goal_home_is_read_in_the_goal_home(self):
        for epic in ({"number": 7},
                     {"number": 7, "repository_url": "https://api.example.invalid/repos/" + HOME}):
            count, gh = _count(epic)
            self.assertEqual(count, 2)
            gh.paginate.assert_called_once_with(
                "/repos/%s/issues/7/sub_issues?per_page=100" % HOME)


if __name__ == "__main__":
    unittest.main()
