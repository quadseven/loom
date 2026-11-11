"""Journal reads sub-issue epics in the repository that owns each issue."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, journal  # noqa: E402
from bus.github import GitHub  # noqa: E402

HOME = "example-owner/goal-home"
OTHER = "example-owner/ticket-home"


class Fake:
    def __init__(self):
        self.get_urls = []

    def __call__(self, method, url, headers, body):
        if method == "GET":
            self.get_urls.append(url)
            if "/issues/3/sub_issues" in url and url.startswith("/repos/" + HOME):
                return 404, {}, b'{"message":"Not Found"}'
            if "/comments" in url or "/issues?" in url:
                return 200, {}, b"[]"
            if "/sub_issues" in url:
                if "/issues/2/sub_issues" in url:
                    nodes = [{"number": 20, "title": "Ticket", "state": "open",
                              "labels": [{"name": "loom:ticket"}]}]
                elif "/issues/3/sub_issues" in url and url.startswith("/repos/" + OTHER):
                    nodes = [{"number": 30, "title": "Ticket", "state": "open",
                              "labels": [{"name": "loom:ticket"}]}]
                else:
                    nodes = []
                return 200, {}, json.dumps(nodes).encode()
            return 200, {}, b"[]"
        if method == "POST" and url.endswith("/comments"):
            return 201, {}, json.dumps({"id": 1}).encode()
        return 200, {}, b"{}"


class JournalCrossRepoSubIssueTest(unittest.TestCase):
    def test_report_posts_and_reads_remote_epic_in_its_repo(self):
        fake = Fake()
        gh = GitHub("placeholder", transport=fake)
        report = "\n".join([
            journal.REPORT_MARKER,
            "Report 2026-11-11T00:00:00Z, tester",
            "- Measure: none",
            "- Shipped: none",
            "- In flight: none",
            "- Blocked: none",
            "- Next: none",
        ])
        direct = [{"number": 2, "title": "Local epic", "state": "open",
                   "labels": [{"name": "epic"}],
                   "repository_url": "https://api.github.com/repos/" + HOME},
                  {"number": 3, "title": "Remote epic", "state": "open",
                   "labels": [{"name": "epic"}],
                   "repository_url": "https://api.github.com/repos/" + OTHER}]
        with mock.patch.object(brain, "_direct_children", return_value=direct):
            journal.post(gh, HOME, 1, report, "tester", ["tester"],
                         now=datetime(2026, 11, 11, tzinfo=timezone.utc),
                         quiet=lambda: brain.goal_quiet(gh, HOME, 1, ["tester"],
                                                        now=datetime(2026, 11, 11, tzinfo=timezone.utc)))
        bad = [u for u in fake.get_urls if "/repos/%s/issues/3/sub_issues" % HOME in u]
        self.assertEqual([], bad, "cross-repo epic sub-issues read from goal home: %s" % bad)
        good = [u for u in fake.get_urls if "/repos/%s/issues/3/sub_issues" % OTHER in u]
        self.assertTrue(good, "remote epic sub-issues never read from its repo: %s" % fake.get_urls)


if __name__ == "__main__":
    unittest.main()

