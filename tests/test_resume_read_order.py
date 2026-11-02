"""bus.brain resume reads its repos lightest first, and trims the reads that
repeat what it already holds (loom#2483).

After the hard deadline (loom#1578) a Muse resume over 19 repos at
--deadline 1.0 still ended partial on about a third of ticks. Measured
read-only against live GitHub, the 19 repos took 441 requests and 7.5 MB on
the wire: the goal journals (the open issue listings and every goal's whole
comment history, read page after page) were 4.3 MB, and the finished epics
part 2.3 MB, most of it the open loom:ticket listing (a subset of the open
listing already read), every epic open and closed with its body, and each
goal's sub-issues with their bodies. These tests pin the order the repos are
read in, the request count of a loom-sized repo, and that each trimmed read
answers the same as the read it replaces."""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests import test_resume_read_budget as budget  # noqa: E402
from tests.test_handoff import (ALLOWED, LOCK_REPO, NOW, Routes, at, comment, defer,  # noqa: E402
                                journal, lock_routes)

OWNER = "example-owner"
# Given heaviest first, as LOOM_REPOS lists quadseven/loom first.
LIGHT = "%s/light" % OWNER
WEIGHTS = {"%s/heavy" % OWNER: 90, "%s/middle" % OWNER: 40, LIGHT: 3}
GIVEN = list(WEIGHTS)


def _goal_routes(repos):
    """One goal per repo with a live merge defer on its own pull request,
    and one finished epic (every ticket closed) under it."""
    routes = lock_routes()
    for n, repo in enumerate(repos):
        goal = {"number": 1, "title": "Goal %d" % n, "state": "open", "comments": 1,
                "labels": [{"name": "loom:goal"}]}
        epic = {"number": 20, "title": "Epic: work %d" % n, "state": "open",
                "labels": [{"name": "epic"}], "body": "Part of #1\n"}
        closed = {"number": 30, "state": "closed", "labels": [{"name": "loom:ticket"}]}
        routes.update({
            ("GET", "/repos/%s/issues" % repo): (200, [goal, epic]),
            ("GET", "/repos/%s/issues/1/comments" % repo): (
                200, [comment(journal(5, "muse", defer((repo, 7), "pr-abcdef123456")))]),
            ("GET", "/repos/%s/issues/7" % repo): (200, {"state": "open", "pull_request": {}}),
            ("GET", "/repos/%s/pulls/7" % repo): (200, {"state": "open",
                                                         "head": {"sha": "abcdef1234567890"}}),
            ("GET", "/repos/%s/issues/1/sub_issues" % repo): (200, [epic]),
            ("GET", "/repos/%s/issues/20/sub_issues" % repo): (200, [closed]),
        })
    return routes


class Weighed:
    """Routes, plus the repo weights query (brain.repo_weights) answered
    from WEIGHTS; it keeps the order each repo's listing was read in."""

    def __init__(self, routes):
        self.routes = Routes(routes)
        self.listed = []
        self.weighed = 0

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1].split("?")[0]
        if method == "POST" and "pullRequests(states:OPEN)" in json.loads(body)["query"]:
            self.weighed += 1
            query = json.loads(body)["query"]
            found = re.findall(r'(r\d+): repository\(owner:"([^"]+)", name:"([^"]+)"\)', query)
            data = {alias: {"issues": {"totalCount": WEIGHTS["%s/%s" % (o, n)]},
                            "pullRequests": {"totalCount": 0}} for alias, o, n in found}
            return 200, {}, json.dumps({"data": data}).encode()
        listing = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues", path)
        if listing and "labels=" not in url:
            self.listed.append(listing.group(1))
        return self.routes(method, url, headers, body)


def _resume(transport, repos, **kwargs):
    with redirect_stderr(io.StringIO()):
        return brain.resume(GitHub("t", transport=transport), repos, "lane-machine",
                            LOCK_REPO, NOW, ALLOWED, **kwargs)


class ResumeReadsTheLightestRepoFirst(unittest.TestCase):
    def test_the_repos_are_read_lightest_first(self):
        # One worker reads the repos one after another, so the order of the
        # listings is the read order. On main it was the order given.
        fake = Weighed(_goal_routes(GIVEN))
        with mock.patch.object(brain, "READ_WORKERS", 1):
            _resume(fake, GIVEN)
        self.assertEqual(1, fake.weighed)
        self.assertEqual(sorted(GIVEN, key=WEIGHTS.get), fake.listed)

    def test_a_deadline_leaves_the_heaviest_repos_unread(self):
        # The heavier repos' reads hang past the deadline: the light repo,
        # read first, is printed and only the heavier ones are named. In the
        # order given, the heaviest repo held the one worker and no repo
        # was printed.
        release = threading.Event()
        self.addCleanup(release.set)
        weighed = Weighed(_goal_routes(GIVEN))

        def transport(method, url, headers, body):
            if "/light/" not in url and "/issues?" in url:
                release.wait(5)
            return weighed(method, url, headers, body)
        missed = []
        with mock.patch.object(brain, "READ_WORKERS", 1), \
                mock.patch.object(brain, "PAST_DEADLINE_SECONDS", 0.5):
            text = _resume(transport, GIVEN, deadline=0.5 / 60, missed=missed)
        self.assertIn("## %s#1 Goal 2" % LIGHT, text)
        for repo in ("%s/heavy" % OWNER, "%s/middle" % OWNER):
            self.assertIn(repo, missed)
            self.assertNotIn("## %s#1" % repo, text)

    def test_only_the_print_order_changes(self):
        # The sections across repos keep the order given: the finished epics
        # read lightest first are listed as before, heaviest first.
        weighed = _resume(Weighed(_goal_routes(GIVEN)), GIVEN)
        given = _resume(Routes(_goal_routes(GIVEN)), GIVEN)
        self.assertEqual(sorted(weighed.splitlines()), sorted(given.splitlines()))
        tail = weighed.split("## Finished epics (every ticket closed)")[1].split("\n## ")[0]
        self.assertEqual(["- resume close-epic %s#20" % repo for repo in GIVEN],
                         [" ".join(line.split()[:4]) for line in tail.strip().splitlines()])


class ALoomSizedRepoReadsLess(unittest.TestCase):
    """budget.LoomSizedResume: two goals, 26 epics, 19 shipped tickets."""

    def test_the_request_count_is_pinned(self):
        # On main: 32 (two REST goal sub-issue lists, the epic and the open
        # ticket listings, and the open pull requests).
        fake = budget.LoomSizedResume()
        budget._resume(fake)
        self.assertEqual(29, fake.reads())

    def test_no_listing_the_open_listing_holds_is_read_again(self):
        fake = budget.LoomSizedResume()
        report = budget._resume(fake)
        paths = [p for _, p in fake.calls]
        for left in ("labels=loom:ticket", "labels=epic", "/pulls", "/search/issues",
                     "/sub_issues"):
            self.assertFalse([p for p in paths if left in p], left)
        listing = [p for p in paths if p.startswith("/repos/%s/issues?" % budget.REPO)
                   and "labels=" not in p]
        self.assertEqual(1, len(listing))
        self.assertIn("- resume close-epic %s#%d" % (budget.REPO, budget.FINISHED), report)

    def test_the_text_is_the_same_as_the_full_reads(self):
        # The full walk (goal_tree: REST sub-issue lists, the epic listing,
        # the open ticket listing) and the pull request listing gives the
        # same resume text.
        def every_epic(gh, repo, goal):
            return brain.goal_tree(gh, repo, goal)[1]
        fast = budget._resume(budget.LoomSizedResume())
        with mock.patch.object(brain, "_finishable_tree", every_epic), \
                mock.patch.object(brain, "_sensitive_listed", lambda *a: True):
            slow = budget._resume(budget.LoomSizedResume())
        self.assertEqual(fast.splitlines(), slow.splitlines())

    def test_an_open_epic_named_only_by_its_part_of_line_is_finished(self):
        # Not a sub-issue of the goal: the open listing names it, and a
        # closed epic is never read at all.
        routes = _goal_routes(["%s/one" % OWNER])
        repo = "%s/one" % OWNER
        goal, epic = routes[("GET", "/repos/%s/issues" % repo)][1]
        closed_epic = dict(epic, number=21, state="closed")
        routes[("GET", "/repos/%s/issues/1/sub_issues" % repo)] = (200, [closed_epic])
        fake = Routes(routes)
        text = _resume(fake, [repo])
        self.assertIn("- resume close-epic %s#20" % repo, text)
        self.assertNotIn("issue(number:21)", " ".join(
            body["query"] for path, body in fake.posted if body))

    def test_a_handoff_item_named_by_two_goals_is_read_once(self):
        fake = TwoGoalsOneItem()
        report = budget._resume(fake)
        self.assertIn("- dropped failed %s#850" % budget.REPO, report)
        self.assertEqual(1, fake.calls[("GET", "/repos/%s/issues/850" % budget.REPO)])


class TwoGoalsOneItem(budget.LoomSizedResume):
    """Both goals' journals defer the same closed ticket."""

    @staticmethod
    def _comments(number):
        if number not in budget.GOALS:
            return []
        body = "\n".join([brain.handoff.JOURNAL_MARKER, "Tick %s, muse" % budget._at(0.1),
                          brain.handoff.defer_marker("failed", (budget.REPO, 850),
                                                     "failed-c1", "muse")])
        return [{"id": 8100 + number, "body": body, "user": budget.WRITER,
                 "created_at": budget._at(0.1), "updated_at": budget._at(0.1)}]


class SensitivePullRequests(unittest.TestCase):
    def routes(self, labels):
        repo = "%s/one" % OWNER
        routes = _goal_routes([repo])
        goal, epic = routes[("GET", "/repos/%s/issues" % repo)][1]
        pr = {"number": 40, "title": "a change", "state": "open", "pull_request": {},
              "labels": [{"name": n} for n in labels], "created_at": at(600)}
        routes[("GET", "/repos/%s/issues" % repo)] = (200, [goal, epic, pr])
        return repo, Routes(routes)

    def test_without_a_labelled_pull_request_the_pull_requests_are_not_listed(self):
        for labels in ((), ("needs-brain",), ("class:sensitive",)):
            with self.subTest(labels=labels):
                repo, fake = self.routes(labels)
                text = _resume(fake, [repo])
                self.assertIn("## Sensitive pull requests", text)
                self.assertNotIn(("GET", "/repos/%s/pulls" % repo), fake.calls)

    def test_a_pull_request_labelled_both_lists_them_once(self):
        repo, fake = self.routes(("needs-brain", "class:sensitive"))
        _resume(fake, [repo])
        self.assertEqual(1, fake.calls.count(("GET", "/repos/%s/pulls" % repo)))


class Comments:
    """A goal home's comments, answered page by page as GitHub answers
    them: a next link on every page but the last, and pages numbered from
    one. It keeps how many pages were in flight at once."""

    def __init__(self, total, pause=0.0):
        self.items = [{"id": k, "body": "c%d" % k, "user": {"login": "example-writer"},
                       "created_at": "2030-01-01T00:%02d:%02dZ" % (k // 60 % 60, k % 60)}
                      for k in range(total)]
        self.pause = pause
        self.lock = threading.Lock()
        self.paths = []
        self.flying = 0
        self.most = 0

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1]
        with self.lock:
            self.paths.append(path)
            self.flying += 1
            self.most = max(self.most, self.flying)
        try:
            time.sleep(self.pause)
            page = int((re.search(r"[?&]page=(\d+)", path) or [0, 1])[1])
            size = int(re.search(r"per_page=(\d+)", path)[1])
            found = self.items[(page - 1) * size:page * size]
            more = len(self.items) > page * size
            link = ('<https://api.github.com/repositories/1/issues/5/comments?per_page=%d'
                    '&page=%d>; rel="next"' % (size, page + 1)) if more else ""
            return 200, {"link": link}, json.dumps(found).encode()
        finally:
            with self.lock:
                self.flying -= 1


PATH = "/repos/%s/one/issues/5/comments" % OWNER


class GoalCommentPages(unittest.TestCase):
    def read(self, total, count, pause=0.0):
        fake = Comments(total, pause)
        got = brain._comment_pages_side_by_side(GitHub("t", transport=fake), PATH, count)
        return fake, got

    def test_the_pages_are_the_same_comments_as_paginate(self):
        for total, count in ((250, 250), (230, 150), (120, 250), (300, 300)):
            with self.subTest(total=total, count=count):
                fake, got = self.read(total, count)
                self.assertEqual(GitHub("t", transport=Comments(total)).paginate(PATH), got)
                self.assertEqual(PATH + "?per_page=100", fake.paths[0])

    def test_the_pages_after_the_first_are_read_side_by_side(self):
        # Read one after another, this goal home of 750 comments is eight
        # reads in a row (quadseven/loom's journal home on 2026-11-02).
        fake, got = self.read(750, 750, pause=0.05)
        self.assertEqual(750, len(got))
        self.assertEqual(8, len(fake.paths))
        self.assertGreaterEqual(fake.most, 2)

    def test_a_goal_of_one_page_reads_as_before(self):
        fake = Comments(40)
        gh = GitHub("t", transport=fake)
        got = brain._goal_home_comments(gh, "%s/one" % OWNER, 5, 40)
        self.assertEqual(40, len(got))
        self.assertEqual([PATH + "?per_page=100"], fake.paths)


if __name__ == "__main__":
    unittest.main()
