"""A Refs line links a pull request regardless of its branch prefix."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import brain, factory
from bus.github import GitHub
from test_bus_station import AUTHORS, Base, REPO


def reply(body):
    return 200, {}, json.dumps(body).encode()


class StatusTransport:
    def __init__(self):
        self.ticket = {"number": 5, "title": "Ticket five", "body": "",
                       "state": "open", "created_at": "2030-01-01T00:00:00Z",
                       "labels": [{"name": "loom:ticket"}, {"name": "loom:failed"}]}
        self.pull = {"number": 6, "title": "Build fix", "state": "open", "draft": False,
                     "user": {"login": AUTHORS[0]}, "body": "Refs #5",
                     "head": {"ref": "muse/5-x", "sha": "head5"},
                     "base": {"repo": {"full_name": REPO}}}

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path
        if method == "GET" and path == "/repos/%s/issues" % REPO:
            return reply([self.ticket])
        if method == "GET" and path == "/repos/%s/pulls" % REPO:
            return reply([self.pull])
        if method == "GET" and path == "/repos/%s/issues/5" % REPO:
            return reply(self.ticket)
        if method == "POST" and path == "/graphql":
            return reply({"data": {"repository": {
                "p6": {"commits": {"nodes": [{"commit": {
                    "committedDate": "2030-01-01T00:00:00Z",
                    "statusCheckRollup": {"state": "SUCCESS"}}}]},
                    "reviews": {"nodes": []}, "reviewThreads": {"nodes": []}}}}})
        return reply([] if method == "GET" else {})


class StatusRefsPullTest(unittest.TestCase):
    def test_status_lists_a_refs_pull_from_a_non_loom_branch_under_pull_requests(self):
        report = brain.status(
            GitHub("token", transport=StatusTransport()), [REPO], REPO,
            env={"LOOM_BRIEF_AUTHORS": AUTHORS[0]}, authors=AUTHORS[:1])
        section = report.split("### Pull requests", 1)[1].split("\n###", 1)[0]
        self.assertIn("- #6 Build fix - blocked: ticket at loom:failed, not loom:pr", section)


class StationRefsPullTest(Base):
    def test_queued_ticket_named_by_a_trusted_refs_pull_moves_to_pr(self):
        self.settings.factory_on = True
        self.fake.add(5, state="queued", body="Difficulty: medium\n\n## Why\nWork.\n",
                      author=AUTHORS[0])
        self.fake.pulls = [{"number": 6, "title": "Build fix", "state": "open",
                            "user": {"login": AUTHORS[0]}, "body": "Refs #5",
                            "head": {"ref": "muse/5-x", "repo": {"full_name": REPO}},
                            "base": {"repo": {"full_name": REPO}}}]

        runner = factory.Factory(GitHub("token", transport=self.fake), [REPO],
                                 time.time(), trusted=AUTHORS, available=("codex",))
        runner.assign_queued()

        self.assertIn("loom:pr", self.fake.names(5))
        self.assertNotIn("loom:queued", self.fake.names(5))


if __name__ == "__main__":
    unittest.main()
