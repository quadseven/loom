"""goal_tree's full (REST) walk reads a cross-repo epic in its own repo.

A goal's sub-issues can include an epic that lives in another repo. The
REST list that names it carries that repo in the epic's repository_url
(or, in the search spelling, its repository full_name); reading the
epic's sub-issues in the goal's own repo answers 404, which is how the
standup for such a goal failed.
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


def issue(number, labels=(), **extra):
    out = {"number": number, "id": 1000 + number, "state": "open",
           "title": "Issue %d" % number, "body": "",
           "labels": [{"name": name} for name in labels]}
    out.update(extra)
    return out


def ticket(number):
    return issue(number, labels=("loom:ticket",))


def epic(number, **extra):
    return issue(number, labels=("epic",), body="Part of #1\n", **extra)


def reply(status, body):
    return status, {}, json.dumps(body).encode()


class CrossRepoWalk:
    """A goal (#1) in HOME whose direct children the test sets, with each
    epic's sub-issues served only from the repo that owns the epic: a
    sub-issue read in any other repo answers 404, as GitHub's does."""

    def __init__(self, direct, subs):
        self.subs = {(HOME, 1): direct}
        self.subs.update(subs)
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[1]
        self.calls.append(path)
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/sub_issues", path)
        if method == "GET" and m:
            key = (m.group(1), int(m.group(2)))
            if key in self.subs:
                return reply(200, self.subs[key])
            return reply(404, {"message": "Not Found"})
        if method == "GET" and path.startswith(
                "/repos/%s/issues?labels=loom:ticket&state=open" % HOME):
            return reply(200, [])
        if method == "GET" and path.startswith(
                "/repos/%s/issues?labels=epic&state=all" % HOME):
            return reply(200, [])
        raise AssertionError("unexpected %s %s" % (method, path))

    def sub_issue_paths(self):
        return [p for p in self.calls if "/sub_issues" in p]


class GoalTreeCrossRepoEpicTest(unittest.TestCase):
    def test_an_epic_named_by_repository_url_is_read_in_its_own_repo(self):
        walk = CrossRepoWalk(
            [ticket(5), epic(10, repository_url="https://api.github.com/repos/" + OTHER)],
            {(OTHER, 10): [ticket(11), ticket(12)]})
        gh = GitHub("t", transport=walk)
        loose, tree = brain.goal_tree(gh, HOME, 1, light=False)
        self.assertEqual([5], [c["number"] for c in loose])
        self.assertEqual([10], [e["number"] for e, _ in tree])
        self.assertEqual([[11, 12]], [[t["number"] for t in tickets] for _, tickets in tree])
        self.assertTrue(any(p.startswith("/repos/%s/issues/10/sub_issues" % OTHER)
                            for p in walk.sub_issue_paths()))
        self.assertFalse(any(p.startswith("/repos/%s/issues/10/sub_issues" % HOME)
                             for p in walk.sub_issue_paths()))

    def test_an_epic_named_by_full_name_and_one_with_no_repo_are_each_read_at_home(self):
        walk = CrossRepoWalk(
            [ticket(5),
             epic(10, repository={"full_name": OTHER}),
             epic(20),
             epic(30, repository={"nameWithOwner": OTHER})],
            {(OTHER, 10): [ticket(11)],
             (HOME, 20): [ticket(21), ticket(22)],
             (OTHER, 30): [ticket(31)]})
        gh = GitHub("t", transport=walk)
        loose, tree = brain.goal_tree(gh, HOME, 1, light=False)
        self.assertEqual([5], [c["number"] for c in loose])
        self.assertEqual([[11], [21, 22], [31]],
                         [[t["number"] for t in tickets] for _, tickets in tree])
        self.assertTrue(any(p.startswith("/repos/%s/issues/10/sub_issues" % OTHER)
                            for p in walk.sub_issue_paths()))
        self.assertTrue(any(p.startswith("/repos/%s/issues/20/sub_issues" % HOME)
                            for p in walk.sub_issue_paths()))
        self.assertTrue(any(p.startswith("/repos/%s/issues/30/sub_issues" % OTHER)
                            for p in walk.sub_issue_paths()))


if __name__ == "__main__":
    unittest.main()
