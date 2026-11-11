"""Status reads sub-issue epics in the repository that owns each issue."""
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

    def __call__(self, method, url, headers, body):
        if method == "GET":
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


class CrossRepoSubIssueTest(unittest.TestCase):
    def test_goal_tree_reads_each_epic_from_its_repository(self):
        fake = Fake()
        gh = GitHub("placeholder", transport=fake)
        # Exercise the same two-stage read used by status: goal children,
        # then ticket lists under each epic.
        direct = brain._sub_issue_tickets(gh, HOME, [1])[1]
        tree = brain.goal_tree
        with mock.patch.object(brain, "_direct_children", return_value=direct):
            _, epics = tree(gh, HOME, 1, light=True)
        self.assertEqual([20, 30], [items[0]["number"] for _, items in epics])
        self.assertIn(OTHER, fake.calls)

    def test_not_found_names_unresolved_sub_issue_and_keeps_read_available(self):
        class Missing(Fake):
            def __call__(self, method, url, headers, body):
                if method == "POST":
                    return 200, {}, json.dumps({"errors": [{"message":
                        "Could not resolve to an Issue with the number of 88",
                        "extensions": {"code": "NOT_FOUND"}}]}).encode()
                return super().__call__(method, url, headers, body)

        warning = []
        found = brain._sub_issue_tickets(GitHub("placeholder", transport=Missing()), HOME,
                                         [88], warnings=warning)
        self.assertEqual([], found[88])
        self.assertEqual(["#88"], warning)


class SharedNumberTest(unittest.TestCase):
    """A local epic and a foreign epic can share an issue number."""

    EPICS = [{"number": 2, "title": "Local epic", "state": "open",
              "labels": [{"name": "epic"}], "repository": {"nameWithOwner": HOME}},
             {"number": 2, "title": "Remote epic", "state": "open",
              "labels": [{"name": "epic"}], "repository": {"nameWithOwner": OTHER}}]

    @staticmethod
    def _read(gh, repo, numbers, **kwargs):
        return {n: [{"number": 20 if repo == HOME else 30, "state": "open"}] for n in numbers}

    def test_goal_tree_keeps_same_numbered_epics_apart(self):
        gh = GitHub("placeholder", transport=Fake())
        with mock.patch.object(brain, "_direct_children", return_value=[]), \
                mock.patch.object(brain, "_goal_epics", return_value=self.EPICS), \
                mock.patch.object(brain, "_sub_issue_tickets", side_effect=self._read):
            _, tree = brain.goal_tree(gh, HOME, 1, light=True)
        self.assertEqual([[20], [30]], [[t["number"] for t in items] for _, items in tree])

    def test_finishable_tree_keeps_same_numbered_epics_apart(self):
        gh = GitHub("placeholder", transport=Fake())
        with mock.patch.object(brain, "_issues", return_value=[]), \
                mock.patch.object(brain, "_sub_issue_tickets", return_value={1: []}), \
                mock.patch.object(brain, "_open_goal_epics", return_value=self.EPICS), \
                mock.patch.object(brain, "_sub_issue_states", side_effect=self._read):
            tree = brain._finishable_tree(gh, HOME, 1)
        self.assertEqual([[20], [30]], [[t["number"] for t in items] for _, items in tree])


if __name__ == "__main__":
    unittest.main()
