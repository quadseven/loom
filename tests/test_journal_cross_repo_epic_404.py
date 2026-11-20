"""A remote epic's sub-issues are never asked in the goal home's repo."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402

HOME = "example-owner/goal-home"
OTHER = "example-owner/ticket-home"


class Fake:
    def __init__(self):
        self.urls = []

    def __call__(self, method, url, headers, body):
        self.urls.append(url)
        if "/repos/%s/issues/3/sub_issues" % HOME in url:
            return 404, {}, b'{"message":"Not Found"}'
        if "/issues/3/sub_issues" in url:
            nodes = [{"number": 30, "title": "Ticket", "state": "open",
                      "labels": [{"name": "loom:ticket"}]}]
            return 200, {}, json.dumps(nodes).encode()
        return 200, {}, b"[]"


class RemoteEpicTest(unittest.TestCase):
    def test_remote_epic_sub_issues_are_read_in_its_own_repo(self):
        fake = Fake()
        gh = GitHub("placeholder", transport=fake)
        epic = {"number": 3, "title": "Remote epic", "state": "open",
                "labels": [{"name": "epic"}],
                "repository_url": "https://api.github.com/repos/" + OTHER}
        count = brain.sub_issue_count(gh, HOME, epic)
        bad = [u for u in fake.urls if "/repos/%s/issues/3/" % HOME in u]
        self.assertEqual([], bad)
        self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
