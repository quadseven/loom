"""Status reads goal comments through gzipped REST, not GraphQL (loom#2464).

GitHub's GraphQL endpoint never compresses, and the goal comments were about
80% of the status query's bytes (738 of 854 KB for the heaviest repo). REST
answers arrive gzipped. These tests pin the status query's field set, the
REST comment reads (count, pages and parameters), and that the goal lines
read the same comments the GraphQL query gave them."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import brain, handoff, status as status_events  # noqa: E402
from bus.github import GitHub, _comment_pages  # noqa: E402
import sub_issue_graphql  # noqa: E402

REPO = "example-owner/example-repo"
ME = "example-brain"
APP = "example-loom"
NOW = datetime(2030, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
# Goal number -> its comment count in the issue listing.
COUNTS = {1: 0, 2: 40, 3: 100, 4: 192, 5: 250}
# The status query for one lane pull request and the goals, field for field.
PINNED_QUERY = (
    'query { repository(owner:"example-owner", name:"example-repo") {'
    " p7: pullRequest(number:7) {"
    " commits(last:1) { nodes { commit { committedDate statusCheckRollup { state } } } }"
    " reviews(last:100) { nodes { author { __typename login } state submittedAt } }"
    " reviewThreads(first:100) { nodes { isResolved"
    " root: comments(first:1) { nodes { author { __typename login } createdAt } } } }"
    " comments(last:100) { totalCount nodes { databaseId url createdAt body"
    " author { __typename login } } } }"
    " defaultBranchRef { name }"
    " merged: pullRequests(states:MERGED, first:10,"
    " orderBy:{field:UPDATED_AT, direction:DESC}) { nodes { baseRefName } } } }")


def reply(code=200, body=None, link=""):
    headers = {"link": link} if link else {}
    return code, headers, b"" if body is None else json.dumps(body).encode()


def comment(n, login=ME, bot=False, body=None, edited=None):
    """The n-th REST comment, a minute apart from 2030-01-01T00:00:00Z."""
    at = "2030-01-01T%02d:%02d:00Z" % (n // 60 % 24, n % 60)
    return {"id": n, "body": body if body is not None else "comment %d" % n,
            "created_at": at, "updated_at": edited or at,
            "user": {"login": login + ("[bot]" if bot else ""), "type": "Bot" if bot else "User"}}


def graphql_node(c):
    """What the GraphQL `comments(last:100)` node gave for a REST comment."""
    user = c["user"]
    bot = user["type"] == "Bot"
    login = user["login"][:-len("[bot]")] if bot else user["login"]
    return {"body": c["body"], "createdAt": c["created_at"], "updatedAt": c["updated_at"],
            "author": {"__typename": user["type"], "login": login}}


class Repo:
    """A fake GitHub for one repo's status: goals with comments, one lane PR."""

    def __init__(self, comments, counts=None):
        self.comments = comments  # goal number -> its REST comments
        self.counts = counts or {n: len(c) for n, c in comments.items()}
        self.queries, self.comment_reads = [], []

    def goals(self):
        return [{"number": n, "title": "Goal %d" % n, "state": "open", "body": "",
                 "created_at": "2029-12-01T00:00:00Z", "labels": [{"name": "loom:goal"}],
                 "comments": count} for n, count in sorted(self.counts.items())]

    def _page(self, number, query):
        per = int(query["per_page"][0])
        page = int(query.get("page", ["1"])[0])
        found = self.comments.get(number, [])
        link = ""
        if page * per < len(found):
            link = '<https://api.github.com/repos/%s/issues/%d/comments?per_page=%d&page=%d>; rel="next"' % (
                REPO, number, per, page + 1)
        return reply(200, found[(page - 1) * per:page * per], link)

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        if method == "POST" and path == "/graphql":
            asked = json.loads(body)["query"]
            if sub_issue_graphql.asks(asked):
                return reply(200, sub_issue_graphql.answer(asked, lambda n: []))
            self.queries.append(asked)
            return reply(200, {"data": {"repository": {"defaultBranchRef": {"name": "main"}}}})
        if method != "GET":
            raise AssertionError("status wrote: %s %s" % (method, url))
        for number in self.counts:
            if path == "/repos/%s/issues/%d/comments" % (REPO, number):
                self.comment_reads.append((number, parts.query))
                return self._page(number, query)
        if path == "/repos/%s/issues" % REPO:
            return reply(200, [] if "labels" in query else self.goals())
        if path == "/repos/%s/pulls" % REPO:
            return reply(200, [{"number": 7, "title": "Lane work", "head": {"ref": "loom/9-work"},
                                "user": {"login": ME}, "labels": [], "body": "Refs #9"}])
        if path == "/repos/%s" % REPO:
            return reply(200, {"private": False})
        return reply(200, [])


def run_status(fake):
    gh = GitHub("t", transport=fake)
    return brain.status(gh, [REPO], REPO, env={"LOOM_BRIEF_AUTHORS": ME}, now=NOW, authors=[ME])


class StatusQueryTests(unittest.TestCase):
    def test_the_status_query_asks_no_goal_comments_and_pins_its_field_set(self):
        fake = Repo({n: [comment(i) for i in range(c)] for n, c in COUNTS.items()})
        run_status(fake)
        self.assertEqual([PINNED_QUERY], fake.queries)
        self.assertNotIn("issue(number:", fake.queries[0])

    def test_each_goal_reads_only_the_pages_its_last_100_comments_are_on(self):
        fake = Repo({n: [comment(i) for i in range(c)] for n, c in COUNTS.items()})
        run_status(fake)
        # No comments: no read. Up to 100: page 1. 192: two pages of 92
        # (comments 92 to 191). 250: two pages of 75 (comments 150 to 249).
        self.assertEqual([(2, "per_page=100&page=1"), (3, "per_page=100&page=1"),
                          (4, "per_page=92&page=2"), (4, "per_page=92&page=3"),
                          (5, "per_page=75&page=3"), (5, "per_page=75&page=4")],
                         sorted(fake.comment_reads))


class LastCommentsTests(unittest.TestCase):
    def read(self, actual, count):
        fake = Repo({1: [comment(i) for i in range(actual)]}, counts={1: count})
        found = GitHub("t", transport=fake).last_comments(REPO, 1, count, 100)
        return [c["id"] for c in found], [q for _, q in fake.comment_reads]

    def test_the_last_100_of_250_come_from_two_pages_of_75(self):
        self.assertEqual((list(range(150, 250)), ["per_page=75&page=3", "per_page=75&page=4"]),
                         self.read(250, 250))

    def test_a_comment_added_since_the_listing_is_followed_to_the_next_page(self):
        self.assertEqual((list(range(101, 201)), ["per_page=100&page=2", "per_page=100&page=3"]),
                         self.read(201, 200))

    def test_comments_deleted_since_the_listing_are_made_up_from_the_page_before(self):
        self.assertEqual((list(range(80, 180)), ["per_page=75&page=3", "per_page=75&page=4",
                                                 "per_page=75&page=2"]), self.read(180, 250))

    def test_the_page_size_reads_the_last_100_in_the_fewest_requests_and_comments(self):
        # (page size, first page, last page)
        self.assertEqual((100, 1, 1), _comment_pages(40, 100))
        self.assertEqual((100, 1, 1), _comment_pages(100, 100))
        self.assertEqual((100, 1, 2), _comment_pages(101, 100))
        self.assertEqual((92, 2, 3), _comment_pages(192, 100))
        self.assertEqual((100, 2, 2), _comment_pages(200, 100))
        self.assertEqual((75, 3, 4), _comment_pages(250, 100))
        for count in range(1, 1200):
            size, first, last = _comment_pages(count, 100)
            self.assertLessEqual(last - first + 1, 2, count)
            self.assertLessEqual((first - 1) * size, max(0, count - 100), count)
            self.assertGreaterEqual(last * size, count, count)

    def test_no_comments_reads_nothing(self):
        self.assertEqual(([], []), self.read(0, 0))


class SameCommentsTests(unittest.TestCase):
    """The goal lines read what the GraphQL node gave them, Bot authors,
    edited reports and status-looking comments included."""

    def comments(self):
        report = handoff.REPORT_MARKER + "\nIntake is empty."
        edited = report + "\n" + handoff.REPORTED_STAMP % "2030-01-08T11:00:00Z"
        status_like = status_events.render([("2030-01-07T10:00:00Z", "claimed by lane x")])
        return [comment(1, body=report), comment(2, login=APP, bot=True,
                                                 body=handoff.JOURNAL_MARKER + "\nTick 1, muse"),
                comment(3, body=status_like),
                comment(4, login=APP, bot=True, body=edited, edited="2030-01-08T11:00:00Z")]

    def test_the_rest_read_gives_the_graphql_nodes(self):
        rest = self.comments()
        gh = GitHub("t", transport=Repo({1: rest}))
        # A status writer's comment would read as events through rest();
        # GraphQL gave it as one comment, and so does this read.
        gh.status_authors |= {ME}
        gh.status_in_body = True
        node = brain._goal_comment_node(gh, REPO, {"number": 1, "comments": len(rest)})
        self.assertEqual({"comments": {"nodes": [graphql_node(c) for c in rest]}}, node)

    def test_the_goal_lines_are_the_same_as_from_the_graphql_node(self):
        rest = self.comments()
        goal = {"number": 1, "title": "Goal", "created_at": "2029-12-01T00:00:00Z", "comments": 4}
        authors = [ME, APP + "[bot]"]
        before = brain._goal_lines([goal], {"goals": {1: {"comments": {"nodes": [
            graphql_node(c) for c in rest]}}}}, NOW, authors)
        node = brain._goal_comment_node(GitHub("t", transport=Repo({1: rest})), REPO, goal)
        self.assertEqual(before, brain._goal_lines([goal], {"goals": {1: node}}, NOW, authors))
        # The App's report edited in place counts at its edit (bus/quiet.py).
        self.assertTrue(any("report 2030-01-08T11:00:00Z" in line for line in before), before)


if __name__ == "__main__":
    unittest.main()
