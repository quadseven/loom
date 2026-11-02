"""bus.brain status reads a goal's sub-issues in a few GraphQL queries that
ask only for what it prints, not one REST list per epic with every body
(loom#2461).

Measured on 2026-11-01: a status of three heavy repos read 103 REST
sub-issue lists, 2.2 MB gzipped and 15.7 MB unpacked, the largest part of
its read; one repo's 57 lists held 605 sub-issues at about 12 KB each. The
same sub-issues with only number, title, state, label names and three times
came to 192 KB in 3 queries. These tests run the goal lines over a fake
GitHub at the transport, which serves the same issues both ways."""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
import sub_issue_graphql  # noqa: E402

REPO = "example-owner/example-repo"
NOW = datetime(2030, 1, 10, 12, 0, tzinfo=timezone.utc)
GOAL = 1
# More epics than one sub-issue query asks for (brain.EPIC_STATES).
EPICS = list(range(100, 100 + brain.EPIC_STATES + 5))
# What each sub-issue query asks of a sub-issue, spelled out here so that a
# change to the field set shows as a failing test.
PINNED_FIELDS = ("number title state createdAt closedAt updatedAt"
                 " labels(first:100) { nodes { name } }")


def _issue(number: int, labels: tuple, state: str = "open", **extra) -> dict:
    """An issue as the REST sub-issue list returns it, body and all."""
    out = {"number": number, "id": 9000 + number, "title": "Issue %d" % number,
           "state": state, "body": "x" * 4000, "labels": [{"name": n} for n in labels],
           "created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-02T00:00:00Z",
           "closed_at": "2030-01-03T00:00:00Z" if state == "closed" else None,
           "user": {"login": "example-lane"}}
    out.update(extra)
    return out


def _subs() -> dict:
    """Goal #1: a loose shipped ticket, the epics, and two tickets under each
    epic in states the goal lines count apart: shipped, awaiting evidence,
    held for a live event, closed and merged, and one waiting on a person."""
    states = [("loom:ticket", "loom:shipped"),
              ("loom:ticket", "loom:shipped", brain.AWAITING_EVIDENCE),
              ("loom:ticket", "loom:shipped", brain.WAIT_LIVE_EVENT),
              ("loom:ticket", "loom:merged"),
              ("loom:ticket", "loom:pr", "needs:human")]
    subs = {GOAL: [_issue(5, ("loom:ticket", "loom:shipped"))]}
    for n, epic in enumerate(EPICS):
        closed = n % 7 == 3
        subs[GOAL].append(_issue(epic, ("epic",), state="closed" if closed else "open",
                                 title="Epic: %d" % epic, body="Part of #1\n"))
        subs[epic] = [_issue(epic * 10 + k, states[(n + k) % len(states)],
                             state="closed" if (n + k) % 5 == 3 else "open",
                             body="Part of #%d\n" % epic) for k in range(2)]
    return subs


class Fake:
    """GitHub at the transport: each sub-issue list both as REST and as the
    GraphQL query, from the same issues; every request is recorded."""

    def __init__(self, subs: dict) -> None:
        self.subs = subs
        self.calls: list = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        query = json.loads(body or b"{}").get("query", "") if path == "/graphql" else ""
        self.calls.append((method, path, query))
        found = re.match(r"/repos/%s/issues/(\d+)/sub_issues" % re.escape(REPO), path)
        if method == "GET" and found:
            return self.reply(self.subs.get(int(found.group(1)), []))
        if method == "POST" and sub_issue_graphql.asks(query):
            return self.reply(sub_issue_graphql.answer(query, lambda n: self.subs.get(n, [])))
        if method == "GET":
            return self.reply([])
        raise AssertionError("unexpected %s %s" % (method, path))

    @staticmethod
    def reply(body):
        return 200, {}, json.dumps(body).encode()

    def rest_lists(self) -> int:
        return sum(1 for m, p, _ in self.calls if m == "GET" and "/sub_issues" in p)

    def queries(self) -> list:
        return [q for _, _, q in self.calls if sub_issue_graphql.asks(q)]


def _goal() -> dict:
    return {"number": GOAL, "title": "Goal", "state": "open", "body": "",
            "created_at": "2029-12-01T00:00:00Z", "labels": [{"name": "loom:goal"}]}


def _lines(fake: Fake, light_off: bool = False) -> list:
    gh = GitHub("t", transport=fake)
    issues = [i for subs in fake.subs.values() for i in subs if "needs:human" in
              [x["name"] for x in i["labels"]] and i["state"] == "open"]
    shares: dict = {}
    if light_off:
        full = brain.goal_tree
        with mock.patch.object(brain, "goal_tree",
                               lambda g, r, n, light=False: full(g, r, n)):
            lines = brain._goal_lines([_goal()], {"goals": {}}, NOW, None, gh, REPO,
                                      issues, shares)
    else:
        lines = brain._goal_lines([_goal()], {"goals": {}}, NOW, None, gh, REPO,
                                  issues, shares)
    return lines + ["shares %r" % shares]


class StatusSubIssueReadTest(unittest.TestCase):
    def test_status_reads_no_rest_sub_issue_list_and_pins_its_queries(self) -> None:
        fake = Fake(_subs())
        _lines(fake)
        self.assertEqual(0, fake.rest_lists())
        queries = fake.queries()
        # One query for the goal's own sub-issues, then the epics
        # EPIC_STATES to a query: 3 here, where REST sent 1 + 30 lists.
        self.assertEqual(1 + -(-len(EPICS) // brain.EPIC_STATES), len(queries))
        asked = [n for q in queries for n in sub_issue_graphql.numbers(q)]
        self.assertEqual([GOAL] + EPICS, asked)
        for query in queries:
            fields = re.findall(r"nodes \{ (.*?) \} \} \}(?= e\d+:| \} \}$)", query)
            self.assertEqual([PINNED_FIELDS] * len(sub_issue_graphql.numbers(query)), fields)
            self.assertNotIn("body", query)

    def test_the_goal_lines_are_the_same_as_the_full_rest_walk(self) -> None:
        light = Fake(_subs())
        full = Fake(_subs())
        self.assertEqual(_lines(full, light_off=True), _lines(light))
        self.assertEqual(1 + len(EPICS), full.rest_lists())
        self.assertEqual(0, light.rest_lists())

    def test_a_quiet_goal_reads_the_same_ticket_times_as_the_full_walk(self) -> None:
        # bus/quiet.py compares each ticket's created, closed and updated
        # times with the newest report: a ticket closed after the report
        # keeps the goal from being quiet either way.
        subs = _subs()
        report = {"body": brain.REPORT_MARKER + "\n- Measure: none\n",
                  "createdAt": "2030-01-02T12:00:00Z", "updatedAt": "2030-01-02T12:00:00Z",
                  "author": {"__typename": "User", "login": "allowed"}}
        data = {"goals": {GOAL: {"comments": {"nodes": [report]}}}}
        out = []
        walk = brain.goal_tree
        for light_off in (True, False):
            gh = GitHub("t", transport=Fake(subs))
            patch = mock.patch.object(brain, "goal_tree",
                                      lambda g, r, n, light=False, off=light_off, full=walk:
                                      full(g, r, n, light=light and not off))
            with patch:
                out.append(brain._goal_lines([_goal()], data, NOW, ["allowed"], gh, REPO))
        self.assertEqual(out[0], out[1])
        self.assertFalse(any("quiet since" in line for line in out[1]), out[1])

    def test_an_issue_past_one_page_of_sub_issues_is_read_whole_through_rest(self) -> None:
        subs = _subs()
        subs[EPICS[0]] = [_issue(5000 + k, ("loom:ticket",)) for k in range(brain.SUB_ISSUE_LIMIT)]

        class Truncated(Fake):
            def __call__(self, method, url, headers, body):
                status, headers_, raw = super().__call__(method, url, headers, body)
                answer = json.loads(raw)
                if isinstance(answer, dict) and "e%d" % EPICS[0] in answer["data"]["repository"]:
                    answer["data"]["repository"]["e%d" % EPICS[0]]["subIssues"]["totalCount"] += 1
                return status, headers_, json.dumps(answer).encode()

        fake = Truncated(subs)
        found = brain._sub_issue_tickets(GitHub("t", transport=fake), REPO, EPICS)
        self.assertEqual(1, fake.rest_lists())
        self.assertEqual("x" * 4000, found[EPICS[0]][0]["body"])
        self.assertNotIn("body", found[EPICS[1]][0])

    def test_a_node_reads_in_the_rest_spelling(self) -> None:
        node = {"number": 7, "title": "T", "state": "CLOSED", "createdAt": "2030-01-01T00:00:00Z",
                "closedAt": "2030-01-02T00:00:00Z", "updatedAt": "2030-01-03T00:00:00Z",
                "labels": {"nodes": [{"name": "loom:ticket"}, {"name": "epic"}]}}
        self.assertEqual({"number": 7, "title": "T", "state": "closed",
                          "created_at": "2030-01-01T00:00:00Z",
                          "closed_at": "2030-01-02T00:00:00Z",
                          "updated_at": "2030-01-03T00:00:00Z",
                          "labels": [{"name": "loom:ticket"}, {"name": "epic"}]},
                         brain._rest_issue(node))


if __name__ == "__main__":
    unittest.main()
