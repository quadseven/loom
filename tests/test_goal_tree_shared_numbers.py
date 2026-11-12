"""The goal tree identifies an epic or ticket by repository and number.

A goal in one repository can hold epics that live in another, and two issues
in different repositories can share a number. One fixture builds that goal:

  goal #1 in HOME. Its direct children: a local epic #2, local loose tickets
  #7 and #9, a foreign epic #7 and a foreign epic #8. A local epic #8 names the goal
  in its body only. Open local tickets: #50 and #52 name epic 2 (#52 is a
  sub-issue of the foreign issue #2, so its Part of line is stale), #51 names
  the loose ticket #7, and #53 names the local epic #8. The foreign epic #8
  holds tickets #81, #9 and #60, and #9 shares its number with the loose ticket #9
  of HOME; the foreign epic #7 holds #71 and #50, which shares its number with
  the local ticket #50. HOME has open needs:human issues #9 (a child of the
goal) and #60 (no child of the goal: the foreign ticket #60 only shares its
number).

Every reader of the goal tree is a row of READERS below, and each row must
return the same tree. Every flattening of it (the status read's unproven
count, human share and quiet check all read the flat list) is a row of
FLATTENERS, and each row must keep one entry per repository and number. A
new reader or flattening of the goal tree is added to the matching table
(#2972).
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone

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
LOOSE_9 = issue(HOME, 9, "Loose ticket 9", "loom:ticket")
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
    (HOME, 1): [LOCAL_EPIC, LOOSE, LOOSE_9, FOREIGN_7, FOREIGN_8],
    (HOME, 2): [issue(HOME, 21, "Ticket 21", "loom:ticket")],
    (OTHER, 7): [issue(OTHER, 71, "Ticket 71", "loom:ticket"),
                 issue(OTHER, 50, "Ticket 50 elsewhere", "loom:ticket")],
    (OTHER, 8): [issue(OTHER, 81, "Ticket 81", "loom:ticket"),
                 issue(OTHER, 9, "Ticket 9 elsewhere", "loom:ticket"),
                 issue(OTHER, 60, "Ticket 60 elsewhere", "loom:ticket")],
    (HOME, 8): [issue(HOME, 82, "Ticket 82", "loom:ticket")],
}

# What every reader must find: the goal's loose ticket, then each epic with
# its tickets, in the order the walk meets them.
LOOSE_NUMBERS = [7, 9]
TREE = [("Local epic 2", [21, 50]),
        ("Foreign epic 7", [71, 50]),
        ("Foreign epic 8", [81, 9, 60]),
        ("Local epic 8", [82, 53])]
# The flat list of the whole goal, as (repository, number): the loose tickets,
# then each epic's tickets in walk order, each issue once. #9 and #50 of OTHER
# keep their place beside #9 and #50 of HOME.
FLAT = [(HOME, 7), (HOME, 9),
        (HOME, 21), (HOME, 50),
        (OTHER, 71), (OTHER, 50),
        (OTHER, 81), (OTHER, 9), (OTHER, 60),
        (HOME, 82), (HOME, 53)]


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


def _flat_of_status(gh):
    """What the status read feeds on: _flatten over the light goal_tree."""
    return [_key(i) for i in brain._flatten(*brain.goal_tree(gh, HOME, 1, light=True),
                                           repo=HOME)]


def _flat_of_goal_children(gh):
    return [_key(i) for i in brain.goal_children(gh, HOME, 1)]


def _key(item):
    """(repository, number) as the fixture spells it: a REST issue names its
    repository_url, a light status read a repository node."""
    if "repository_url" in item:
        return (item["repository_url"].split("/repos/")[1], item["number"])
    return (item["repository"]["nameWithOwner"], item["number"])


FLATTENERS = (("_flatten over the status read", _flat_of_status),
              ("goal_children", _flat_of_goal_children))

READERS = (("goal_tree, full read", _goal_tree_full),
           ("goal_tree, status read", _goal_tree_status),
           ("_finishable_tree", _finishable))


HUMAN_ISSUES = [issue(HOME, 9, "Loose ticket 9", "needs:human"),
                issue(HOME, 60, "Not under the goal", "needs:human")]


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

    def test_every_flattening_keeps_one_entry_per_repository_and_number(self):
        for name, flatten in FLATTENERS:
            with self.subTest(flattening=name):
                self.assertEqual(FLAT, self.read(flatten))

    def test_a_foreign_ticket_sharing_a_number_with_a_loose_ticket_is_not_dropped(self):
        for name, flatten in FLATTENERS:
            with self.subTest(flattening=name):
                flat = self.read(flatten)
                self.assertIn((OTHER, 9), flat)
                self.assertIn((OTHER, 50), flat)

    def test_the_status_read_counts_only_the_goals_own_issues_as_waiting_on_a_person(self):
        # _goal_lines is the status consumer: it reads the light goal tree,
        # flattens it and counts how many of the home repository's needs:human
        # issues are the goal's. Foreign ticket #60 shares its number with a
        # needs:human issue of HOME that is no child of the goal, so it is not
        # one of them; the goal's share is #9 alone.
        shares = {}
        goal = {"number": 1, "title": "Goal", "state": "open", "labels": [],
                "created_at": "2026-11-01T00:00:00Z"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        brain._goal_lines([goal], {"goals": {1: {}}}, now, None,
                          GitHub("placeholder", transport=World()), HOME,
                          HUMAN_ISSUES, shares)
        self.assertEqual({1: 1}, shares)

    def test_a_loose_goal_ticket_is_kept_when_a_foreign_epic_shares_its_number(self):
        loose, _ = self.read(_goal_tree_full)
        self.assertEqual([7, 9], loose)

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
