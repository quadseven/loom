"""Journal reads sub-issue epics in the repository that owns each issue."""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402

HOME = "example-owner/goal-home"
OTHER = "example-owner/ticket-home"


class Fake:
    def __init__(self):
        self.calls = []
        self.rest_calls = []

    def __call__(self, method, url, headers, body):
        if method == "GET":
            self.rest_calls.append(url)
            if "/sub_issues" in url:
                # Extract repo and issue number from URL
                match = re.search(r"/repos/(.+?)/issues/(\d+)/sub_issues", url)
                if match:
                    repo = match.group(1)
                    issue_num = int(match.group(2))
                    if issue_num == 2 and repo == HOME:
                        return 200, {}, json.dumps([{"number": 20, "title": "Local ticket", "state": "open"}]).encode()
                    elif issue_num == 3 and repo == OTHER:
                        return 200, {}, json.dumps([{"number": 30, "title": "Remote ticket", "state": "open"}]).encode()
                    elif issue_num == 3 and repo == HOME:
                        # This is the bug: asking for epic #3's sub-issues in HOME repo returns 404
                        return 404, {}, b'{"message": "Not Found"}'
            return 200, {}, b"[]"
        payload = json.loads(body or b"{}")
        query = payload.get("query", "")
        match = re.search(r'repository\(owner:"([^"]+)",name:"([^"]+)"\)', query)
        repo = "%s/%s" % (match.group(1), match.group(2)) if match else ""
        self.calls.append(repo)
        result = {"data": {"repository": {}}}
        for alias, number in re.findall(r"(e\d+): issue\(number:(\d+)\)", query):
            n = int(number)
            if n == 1 and repo == HOME:
                nodes = [{"number": 2, "title": "Local epic", "state": "OPEN",
                          "labels": {"nodes": [{"name": "epic"}]},
                          "repository": {"nameWithOwner": HOME}},
                         {"number": 3, "title": "Remote epic", "state": "OPEN",
                          "labels": {"nodes": [{"name": "epic"}]},
                          "repository": {"nameWithOwner": OTHER}}]
            elif n in (2, 3):
                nodes = [{"number": n * 10, "title": "Ticket", "state": "OPEN",
                          "createdAt": None, "closedAt": None, "updatedAt": None,
                          "labels": {"nodes": [{"name": "loom:ticket"}]}}]
            else:
                nodes = []
            result["data"]["repository"][alias] = {
                "subIssues": {"totalCount": len(nodes), "nodes": nodes}}
        return 200, {}, json.dumps(result).encode()


class JournalCrossRepoSubIssueTest(unittest.TestCase):
    def test_goal_quiet_reads_each_epic_from_its_repository(self):
        """goal_quiet (used by journal) should read each epic's sub-issues from its own repo."""
        fake = Fake()
        gh = GitHub("placeholder", transport=fake)
        # Exercise the same read used by goal_quiet: goal_children -> goal_tree(light=False)
        direct = brain._sub_issue_tickets(gh, HOME, [1])[1]
        with mock.patch.object(brain, "_direct_children", return_value=direct):
            loose, tree = brain.goal_tree(gh, HOME, 1, light=False)
        # Check that we got tickets from both epics
        epic_numbers = [int(e.get("number") or 0) for e, _ in tree]
        self.assertEqual([2, 3], epic_numbers)
        # Check that tickets were read from both repos
        all_tickets = []
        for _, items in tree:
            all_tickets.extend(items)
        ticket_numbers = [int(item.get("number") or 0) for item in all_tickets]
        self.assertEqual([20, 30], ticket_numbers)
        # Verify REST calls were made to the correct repos
        self.assertTrue(any("/repos/%s/issues/2/sub_issues" % HOME in call for call in fake.rest_calls))
        self.assertTrue(any("/repos/%s/issues/3/sub_issues" % OTHER in call for call in fake.rest_calls))
        # The bug: should NOT call HOME for epic #3
        self.assertFalse(any("/repos/%s/issues/3/sub_issues" % HOME in call for call in fake.rest_calls))

    def test_goal_children_reads_each_epic_from_its_repository(self):
        """goal_children should read each epic's sub-issues from its own repo."""
        fake = Fake()
        gh = GitHub("placeholder", transport=fake)
        direct = brain._sub_issue_tickets(gh, HOME, [1])[1]
        with mock.patch.object(brain, "_direct_children", return_value=direct):
            children = brain.goal_children(gh, HOME, 1)
        ticket_numbers = [int(c.get("number") or 0) for c in children]
        self.assertIn(20, ticket_numbers)
        self.assertIn(30, ticket_numbers)
        self.assertTrue(any("/repos/%s/issues/2/sub_issues" % HOME in call for call in fake.rest_calls))
        self.assertTrue(any("/repos/%s/issues/3/sub_issues" % OTHER in call for call in fake.rest_calls))
        self.assertFalse(any("/repos/%s/issues/3/sub_issues" % HOME in call for call in fake.rest_calls))


if __name__ == "__main__":
    unittest.main()