"""The goal tree identifies an epic or ticket by repository and number.

A goal in one repository can hold epics that live in another, and two issues
in different repositories can share a number. One fixture builds that goal:

  goal #1 in HOME. Its direct children: a local epic #2, a local loose ticket
  #7, a foreign epic #7 and a foreign epic #8. A local epic #8 names the goal
  in its body only. Open local tickets: #50 and #52 name epic 2 (#52 is a
  sub-issue of the foreign issue #2, so its Part of line is stale), #51 names
  the loose ticket #7, and #53 names the local epic #8.

Every reader of the goal tree is a row of READERS below, and each row must
return the same tree. A new reader of the goal tree is added to that table
(#2972).
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402

HOME = "example-owner/goal-home"
OTHER = "example-owner/other-home"
API = "https://api.github.com/repos/"


def issue(repo, number, title, *labels, body="", parent=None):
    out = {"number": number, "state": "open", "title": title, "body": body,
           "labels": [{"name": name} for name in labels],
           "repository_url": API + repo}
    if parent:
        out["parent_issue_url"] = API + "%s/issues/%d" % parent
    return out


def node(item):
    """The GraphQL spelling of a REST issue."""
    return {"number": item["number"], "title": item["title"], "state": "OPEN",
            "createdAt": None, "closedAt": None, "updatedAt": None,
            "labels": {"nodes": item["labels"]},
            "repository": {"nameWithOwner": item["repository_url"].split("/repos/")[1]}}


LOCAL_EPIC = issue(HOME, 2, "Local epic 2", "epic", body="Part of #1\n")
LOOSE = issue(HOME, 7, "Loose ticket 7", "loom:ticket")
FOREIGN_7 = issue(OTHER, 7, "Foreign epic 7", "epic")
FOREIGN_8 = issue(OTHER, 8, "Foreign epic 8", "epic")
LISTED_EPIC = issue(HOME, 8, "Local epic 8", "epic", body="Part of #1\n")
OPEN_TICKETS = [
    issue(HOME, 50, "Ticket 50", "loom:ticket", body="Part of #2\n"),
    issue(HOME, 51, "Ticket 51", "loom:ticket", body="Part of #7\n"),
    issue(HOME, 52, "Ticket 52", "loom:ticket", body="Part of #2\n", parent=(OTHER, 2)),
    issue(HOME, 53, "Ticket 53", "loom:ticket", body="Part of #8\n"),
]
SUBS = {
    (HOME, 1): [LOCAL_EPIC, LOOSE, FOREIGN_7, FOREIGN_8],
    (HOME, 2): [issue(HOME, 21, "Ticket 21", "loom:ticket")],
    (OTHER, 7): [issue(OTHER, 71, "Ticket 71", "loom:ticket")],
    (OTHER, 8): [issue(OTHER, 81, "Ticket 81", "loom:ticket")],
    (HOME, 8): [issue(HOME, 82, "Ticket 82", "loom:ticket")],
}

# What every reader must find: the goal's loose ticket, then each epic with
# its tickets, in the order the walk meets them.
LOOSE_NUMBERS = [7]
TREE = [("Local epic 2", [21, 50]),
        ("Foreign epic 7", [71]),
        ("Foreign epic 8", [81]),
        ("Local epic 8", [82, 53])]


def reply(body):
    return 200, {}, json.dumps(body).encode()


class World:
    """GitHub for the fixture: REST and GraphQL reads, nothing else."""

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[1]
        if method == "POST":
            return self._graphql(json.loads(body))
        found = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/sub_issues", path)
        if found:
            return reply(SUBS[(found.group(1), int(found.group(2)))])
        if path.startswith("/repos/%s/issues?labels=epic&state=all" % HOME):
            return reply([LISTED_EPIC])
        if path.startswith("/repos/%s/issues?labels=loom:ticket&state=open" % HOME):
            return reply(OPEN_TICKETS)
        if path.startswith("/repos/%s/issues?state=open" % HOME):
            return reply([LOOSE, LISTED_EPIC] + OPEN_TICKETS)
        raise AssertionError("unexpected GET %s" % path)

    @staticmethod
    def _graphql(payload):
        query = payload["query"]
        owner, name = re.search(r'repository\(owner:"([^"]+)",name:"([^"]+)"\)', query).groups()
        found = {}
        for alias, number in re.findall(r"(e\d+): issue\(number:(\d+)\)", query):
            nodes = [node(i) for i in SUBS.get(("%s/%s" % (owner, name), int(number)), [])]
            found[alias] = {"subIssues": {"totalCount": len(nodes), "nodes": nodes}}
        return reply({"data": {"repository": found}})


def _summary(loose, tree):
    return ([c["number"] for c in loose],
            [(epic["title"], [t["number"] for t in tickets]) for epic, tickets in tree])


def _goal_tree_full(gh):
    return _summary(*brain.goal_tree(gh, HOME, 1, light=False))


def _goal_tree_status(gh):
    return _summary(*brain.goal_tree(gh, HOME, 1, light=True))


def _finishable(gh):
    """The finishing read has no loose tickets and keeps every epic's tickets."""
    return [], [(epic["title"], [t["number"] for t in tickets])
                for epic, tickets in brain._finishable_tree(gh, HOME, 1)]


READERS = (("goal_tree, full read", _goal_tree_full),
           ("goal_tree, status read", _goal_tree_status),
           ("_finishable_tree", _finishable))


class SharedNumbersTest(unittest.TestCase):
    def read(self, reader):
        return reader(GitHub("placeholder", transport=World()))

    def test_every_reader_finds_the_same_tree(self):
        for name, reader in READERS:
            with self.subTest(reader=name):
                loose, tree = self.read(reader)
                self.assertEqual(TREE, tree)
                if name != "_finishable_tree":
                    self.assertEqual(LOOSE_NUMBERS, loose)

    def test_a_loose_goal_ticket_is_kept_when_a_foreign_epic_shares_its_number(self):
        loose, _ = self.read(_goal_tree_full)
        self.assertEqual([7], loose)

    def test_a_local_epic_naming_the_goal_is_found_when_a_foreign_epic_shares_its_number(self):
        for name, reader in READERS:
            with self.subTest(reader=name):
                _, tree = self.read(reader)
                self.assertIn("Local epic 8", [title for title, _ in tree])

    def test_a_part_of_line_never_names_an_epic_in_another_repository(self):
        for name, reader in READERS:
            with self.subTest(reader=name):
                _, tree = self.read(reader)
                foreign = dict(tree)["Foreign epic 7"] + dict(tree)["Foreign epic 8"]
                self.assertNotIn(51, foreign)
                self.assertNotIn(53, foreign)

    def test_a_sub_issue_of_a_foreign_issue_with_the_epics_number_is_not_its_ticket(self):
        for name, reader in READERS:
            with self.subTest(reader=name):
                _, tree = self.read(reader)
                self.assertNotIn(52, dict(tree)["Local epic 2"])


if __name__ == "__main__":
    unittest.main()
