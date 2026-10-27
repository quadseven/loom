"""bus.brain status reads each issue once per repo (loom#2057)."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import unittest
import unittest.mock
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, issue_reads
from bus.github import GitHub, GitHubError
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
NOW = datetime(2030, 1, 1, 14, 0, tzinfo=timezone.utc)
GRUG = {"__typename": "Bot", "login": "grug-tribe"}
ISSUE = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)$")


def _issue(number, labels, body="", **extra):
    out = {"number": number, "id": 1000 + number, "state": "open", "title": "Issue %d" % number,
           "labels": [{"name": n} for n in labels], "body": body,
           "created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-01T00:00:00Z"}
    out.update(extra)
    return out


GOAL = _issue(1, ["loom:goal"], "Merge constraint: keep the docs current\n\n# Goal\n")
EPIC = _issue(2, ["epic"], "Part of #1\n")
TICKETS = {n: _issue(n, ["loom:ticket", "loom:pr"], "Part of #2\n") for n in (4, 5)}
PULLS = [{"number": 10 + n, "title": "PR %d" % n, "head": {"ref": "loom/%d-work" % n},
          "body": "Refs #%d" % n, "labels": [], "user": {"login": "example-lane"},
          "created_at": "2030-01-01T00:00:00Z"} for n in TICKETS]


def _node():
    return {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                              "statusCheckRollup": {"state": "SUCCESS"}}}]},
            "reviews": {"nodes": [{"author": GRUG, "state": "APPROVED",
                                   "submittedAt": "2030-01-01T13:00:00Z"}]},
            "reviewThreads": {"nodes": []}}


class PathFake:
    """Answers by path, whatever the order, and counts each request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = Counter()

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        with self.lock:
            self.calls[(method, path)] += 1
        if method == "POST" and path == "/graphql":
            nodes = {"p%d" % pr["number"]: _node() for pr in PULLS}
            return self._reply({"data": {"repository": nodes}})
        found = ISSUE.match(path)
        if found:
            number = int(found.group(1))
            issue = {1: GOAL, 2: EPIC}.get(number) or TICKETS.get(number)
            return self._reply(issue) if issue else (404, {}, b'{"message": "Not Found"}')
        if path.startswith("/repos/%s/issues?state=open" % REPO):
            return self._reply([GOAL, EPIC] + list(TICKETS.values()))
        if path.startswith("/repos/%s/pulls?state=open" % REPO):
            return self._reply(PULLS)
        if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+", path):
            return self._reply({"mergeable": True, "mergeable_state": "clean"})
        if path.endswith("/files"):
            return self._reply([{"filename": "bus/a.py", "patch": "+x"}])
        return self._reply([])

    @staticmethod
    def _reply(body):
        return 200, {}, json.dumps(body).encode()

    def issue_reads(self):
        return {int(ISSUE.match(p).group(1)): n for (m, p), n in self.calls.items()
                if m == "GET" and ISSUE.match(p)}


def _status(fake):
    return brain.status(GitHub("t", transport=fake), [REPO], REPO, env={}, authors=(), now=NOW)


class StatusReadsEachIssueOnce(unittest.TestCase):
    def test_each_ticket_and_its_goal_home_are_read_once_per_repo(self):
        fake = PathFake()
        report = _status(fake)
        # Both pull requests reach the READY checks and the goal-home rule.
        for pr in (14, 15):
            line = next(x for x in report.splitlines() if x.startswith("- #%d " % pr))
            self.assertIn("goal home %s#1 rule merge cannot check" % REPO, line)
        # Each is read at most once; all four are in the open listing, which
        # answers them, so none is read on its own (loom#2078).
        self.assertEqual(fake.issue_reads(), {})

    def test_the_status_text_is_the_same_as_reading_every_time(self):
        once = _status(PathFake())
        uncached = PathFake()
        with unittest.mock.patch.object(brain.issue_reads, "once", lambda gh: gh):
            every = _status(uncached)
        self.assertGreater(uncached.issue_reads()[4], 1)
        self.assertEqual(once.splitlines(), every.splitlines())


class IssueReadsClient(unittest.TestCase):
    def test_only_a_plain_single_issue_get_is_kept(self):
        fake = PathFake()
        gh = issue_reads.once(GitHub("t", transport=fake))
        self.assertIs(issue_reads.once(gh), gh)
        for _ in range(2):
            gh.rest("GET", "/repos/%s/issues/4" % REPO)
            gh.rest("GET", "/repos/%s/issues/4/comments" % REPO)
            gh.rest("GET", "/repos/%s/issues/5" % REPO, ok=(404,))
            gh.rest("PATCH", "/repos/%s/issues/4" % REPO, {"body": "x"})
        self.assertEqual(fake.calls[("GET", "/repos/%s/issues/4" % REPO)], 1)
        self.assertEqual(fake.calls[("GET", "/repos/%s/issues/4/comments" % REPO)], 2)
        self.assertEqual(fake.calls[("GET", "/repos/%s/issues/5" % REPO)], 2)
        self.assertEqual(fake.calls[("PATCH", "/repos/%s/issues/4" % REPO)], 2)
        # Every other attribute is the wrapped client's.
        self.assertEqual(gh.requests, 7)

    def test_a_failed_read_is_not_kept(self):
        fake = PathFake()
        gh = issue_reads.once(GitHub("t", transport=fake))
        for _ in range(2):
            with self.assertRaises(GitHubError):
                gh.rest("GET", "/repos/%s/issues/99" % REPO)
        self.assertEqual(fake.calls[("GET", "/repos/%s/issues/99" % REPO)], 2)

    def test_readers_on_several_threads_share_one_read(self):
        fake = PathFake()
        gh = issue_reads.once(GitHub("t", transport=fake))
        with ThreadPoolExecutor(8) as pool:
            answers = list(pool.map(lambda _: gh.rest("GET", "/repos/%s/issues/4" % REPO).data,
                                    range(16)))
        self.assertTrue(all(a["number"] == 4 for a in answers))
        self.assertEqual(fake.calls[("GET", "/repos/%s/issues/4" % REPO)], 1)


if __name__ == "__main__":
    unittest.main()
