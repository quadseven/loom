"""loom#636: every goal count walks goal, epics and tickets (decision 0003),
and still counts the goal's direct tickets while tickets move under epics.
The in-flight cap, the unproven limit, status, standups and invariants all
read one walk, so no count drops when a ticket moves."""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock
from datetime import datetime, timezone
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub

REPO = "example-owner/example-repo"
BODY = ("## Why\nwhy: the page query is slow for every visitor.\n## What\nwhat\n"
        "## Acceptance criteria\n- [ ] done\n- [ ] tested\n- [ ] live\n## Size\nS\n## Out of scope\n- none")
PARENT = "https://api.github.com/repos/%s/issues/" % REPO


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


def goal():
    return {"number": 1, "id": 101, "state": "open", "title": "Goal",
            "labels": [{"name": "loom:goal"}]}


def epic(number, body="Part of #1\n", state="open"):
    return {"number": number, "id": 1000 + number, "state": state, "title": "Epic %d" % number,
            "body": "## Done when\nshipped\n\n" + body, "labels": [{"name": "epic"}]}


def ticket(number, state, body="## Why\nx\n", parent=None):
    out = {"number": number, "id": 1000 + number, "state": "open", "title": "Ticket %d" % number,
           "body": body, "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state}]}
    if parent:
        out["parent_issue_url"] = PARENT + str(parent)
    return out


class Repo:
    """Goal #1 with two epics of three tickets each and one direct ticket.

    #20 is an epic linked as the goal's sub-issue; #21 is an epic only its
    Part of #1 line ties to the goal (the goal refused the link). #20 holds
    three sub-issues; #21 holds two, and a third ticket whose link was refused
    names it with Part of #21. #9 is still a direct child of the goal."""

    def __init__(self, state="running"):
        self.sub_issues = {
            1: [epic(20), ticket(9, state, parent=1)],
            20: [ticket(n, state, parent=20) for n in (31, 32, 33)],
            21: [ticket(n, state, parent=21) for n in (41, 42)],
        }
        self.epics = [epic(20), epic(21),
                      epic(22, "Part of #5\n"),          # another goal's epic
                      epic(23, "No parent line\n"),
                      dict(epic(24), pull_request={})]   # a pull request, never an epic
        self.search = [
            ticket(43, state, "## Why\nx\n\nPart of #21\n"),
            ticket(44, state, "Part of #99\n"),           # the search matched loose words
            ticket(45, state, "see Part of #21 inline"),  # names the epic inside a sentence
            dict(ticket(46, state, "Part of #1\n"), labels=[{"name": "bug"}]),  # anyone's issue
        ]
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[1]
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("unexpected write %s %s" % (method, path))
        if path == "/repos/%s/issues/1" % REPO:
            return reply(200, goal())
        if path.startswith("/repos/%s/issues?labels=epic&state=all&" % REPO):
            return reply(200, self.epics)
        if path.startswith("/search/issues?"):
            return reply(200, {"items": self.search})
        for number, found in self.sub_issues.items():
            if path.startswith("/repos/%s/issues/%d/sub_issues?" % (REPO, number)):
                return reply(200, found)
        if "/sub_issues?" in path:
            return reply(200, [])
        raise AssertionError("unexpected %s %s" % (method, path))

    def searches(self):
        return [unquote(p) for m, p in self.calls if p.startswith("/search/issues?")]


class GoalWalkCountTests(unittest.TestCase):
    def gh(self, repo):
        return GitHub("t", transport=repo)

    def test_two_epics_of_three_and_one_direct_child_count_seven_for_the_cap(self):
        repo = Repo("running")
        self.assertEqual(brain._goal_in_flight(self.gh(repo), REPO, 1), 7)
        with unittest.mock.patch.dict(os.environ, {"LOOM_GOAL_CAP": "7"}):
            with self.assertRaisesRegex(ValueError, "7 tickets in flight"):
                brain.brief(self.gh(Repo("running")), REPO, 1, "Ticket", BODY, "codex")

    def test_two_epics_of_three_and_one_direct_child_count_seven_for_the_unproven_limit(self):
        self.assertEqual(brain.goal_unproven(self.gh(Repo("shipped")), REPO, 1), 7)
        with unittest.mock.patch.dict(os.environ, {"LOOM_UNVERIFIED_LIMIT": "6"}):
            with self.assertRaisesRegex(ValueError, "7 tickets shipped without a proof"):
                brain.brief(self.gh(Repo("shipped")), REPO, 1, "Ticket", BODY, "codex")

    def test_the_walk_lists_each_ticket_once_and_no_epic_as_a_ticket(self):
        walk = brain.goal_walk(self.gh(Repo()), REPO, 1)
        self.assertEqual(sorted(brain._number(t) for t in walk.tickets),
                         [9, 31, 32, 33, 41, 42, 43])
        self.assertEqual([brain._number(e) for e, _ in walk.epics], [20, 21])
        self.assertEqual([brain._number(t) for t in walk.loose], [45])

    def test_a_ticket_moving_under_an_epic_counts_once_before_during_and_after(self):
        repo = Repo("running")
        gh = self.gh(repo)
        before = brain._goal_in_flight(gh, REPO, 1)
        # During: #9 is under epic #20 and still a sub-issue of the goal.
        repo.sub_issues[20] = repo.sub_issues[20] + [ticket(9, "running", parent=20)]
        during = brain._goal_in_flight(gh, REPO, 1)
        # After: the goal's link is gone.
        repo.sub_issues[1] = [epic(20)]
        after = brain._goal_in_flight(gh, REPO, 1)
        self.assertEqual((before, during, after), (7, 7, 7))

    def test_direct_part_of_tickets_still_count_once_the_goal_drops_below_the_limit(self):
        # Tickets that named the goal by Part of line (filed past GitHub's
        # 100 sub-issue limit) keep counting while tickets move away, so the
        # goal's sub-issues fall below the limit with epics in place.
        repo = Repo("running")
        repo.search.append(ticket(47, "running", "Part of #1\n"))
        self.assertEqual(brain._goal_in_flight(self.gh(repo), REPO, 1), 8)

    def test_one_search_names_the_goal_and_its_open_epics(self):
        repo = Repo()
        repo.epics.append(epic(25, state="closed"))
        repo.sub_issues[25] = [ticket(51, "running", parent=25)]
        walk = brain.goal_walk(self.gh(repo), REPO, 1)
        searches = repo.searches()
        self.assertEqual(len(searches), 1)
        self.assertIn('"Part of #1" OR "Part of #20" OR "Part of #21"', searches[0])
        self.assertNotIn("#25", searches[0])
        # A closed epic stays under the goal, and its open ticket still counts.
        self.assertIn(51, [brain._number(t) for t in walk.tickets])
        self.assertEqual([brain._number(e) for e in walk.open_epics()], [20, 21])

    def test_each_epics_sub_issues_are_read_once_per_walk(self):
        repo = Repo()
        brain.goal_walk(self.gh(repo), REPO, 1)
        reads = [p for _, p in repo.calls if "/sub_issues?" in p]
        self.assertEqual(sorted(reads), sorted(set(reads)))
        self.assertEqual(len(reads), 3)   # the goal, #20 and #21

    def test_more_than_six_numbers_share_queries_of_at_most_six(self):
        repo = Repo()
        for n in range(60, 66):
            repo.epics.append(epic(n))
        brain.goal_walk(self.gh(repo), REPO, 1)
        searches = repo.searches()
        self.assertEqual(len(searches), 2)
        self.assertTrue(all(s.count("Part of #") <= brain.SEARCH_TERMS for s in searches))

    def test_a_goal_without_epics_below_the_limit_makes_no_search(self):
        repo = Repo()
        repo.sub_issues[1] = [ticket(9, "running", parent=1)]
        repo.epics = []
        self.assertEqual(brain._goal_in_flight(self.gh(repo), REPO, 1), 1)
        self.assertEqual(repo.searches(), [])

    def test_unassociated_tickets_include_loose_mentions_of_an_epic(self):
        loose = brain.unassociated_tickets(self.gh(Repo()), REPO, 1)
        self.assertEqual([brain._number(t) for t in loose], [45])


class GoalWalkStatusTests(unittest.TestCase):
    def test_status_prints_each_epics_tickets_and_the_goals_open_epics(self):
        repo = Repo("shipped")
        repo.epics.append(epic(25, state="closed"))
        now = datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc)
        lines = brain._goal_lines([goal()], {"goals": {1: {}}}, now, None,
                                  GitHub("t", transport=repo), REPO)
        text = "\n".join(lines)
        self.assertIn("unproven work 7/30", text)
        self.assertIn("open epics 2/6", text)
        self.assertIn("epic #20 Epic 20 - open, tickets 3/15", text)
        self.assertIn("epic #21 Epic 21 - open, tickets 3/15", text)
        self.assertIn("epic #25 Epic 25 - closed, tickets 0/15", text)
        self.assertEqual(len(repo.searches()), 1)


if __name__ == "__main__":
    unittest.main()
