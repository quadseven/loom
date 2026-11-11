"""The journal CLI reads cross-repository epic work before posting reports."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import __main__  # noqa: E402
from bus.github import GitHub  # noqa: E402

HOME = "example-owner/goal-home"
EPIC_HOME = "example-owner/epic-home"


class Fake:
    def __init__(self):
        self.gets = []
        self.posts = []

    def __call__(self, method, url, headers, body):
        if method == "GET":
            url = url.replace("https://api.github.com", "")
            self.gets.append(url)
            if url == "/repos/%s/issues/1/sub_issues?per_page=100" % HOME:
                value = [{"number": 516, "title": "Remote epic", "state": "open",
                          "labels": [{"name": "epic"}],
                          "repository_url": "https://api.github.com/repos/%s" % EPIC_HOME}]
            elif url == "/repos/%s/issues/516/sub_issues?per_page=100" % EPIC_HOME:
                value = [{"number": 517, "title": "Ticket", "state": "open",
                          "labels": [{"name": "loom:ticket"}]}]
            elif url.startswith("/repos/%s/issues?labels=epic" % HOME):
                value = []
            elif url.startswith("/repos/%s/issues?labels=loom:ticket" % HOME):
                value = []
            elif url.startswith("/repos/%s/issues/1/comments" % HOME):
                value = []
            elif url.startswith("/repos/%s/issues?labels=" % HOME):
                value = []
            elif url.startswith("/repos/%s/issues/1" % HOME):
                value = {"number": 1, "state": "open", "labels": []}
            else:
                raise AssertionError("unexpected GET %s" % url)
            return 200, {}, json.dumps(value).encode()
        self.posts.append((url.replace("https://api.github.com", ""),
                           json.loads(body or b"{}")))
        return 201, {}, b'{"id":1}'


class JournalCrossRepoSubIssueTest(unittest.TestCase):
    def test_cli_posts_report_and_reads_epic_in_its_own_repository(self):
        report = "\n".join([
            "<!-- loom:report v1 -->", "Report 2030-01-01T10:00:00Z, codex",
            "- Measure: steady", "- Shipped: none", "- In flight: ticket 517",
            "- Blocked: none", "- Next: continue", "Waiting on you: nothing",
        ])
        fake = Fake()
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
            stream.write(report)
            stream.flush()
            args = argparse.Namespace(command="journal", target=HOME + "#1", file=stream.name,
                                      holder=None, lock_repo=None)
            with mock.patch("bus.brain.trusted_authors", return_value=[]):
                self.assertEqual(0, __main__._run(args, GitHub("placeholder", transport=fake)))

        epic_reads = [url for url in fake.gets if "/issues/516/sub_issues?" in url]
        self.assertEqual(["/repos/%s/issues/516/sub_issues?per_page=100" % EPIC_HOME], epic_reads)
        self.assertEqual(1, len(fake.posts))
        self.assertEqual("/repos/%s/issues/1/comments" % HOME, fake.posts[0][0])
        self.assertTrue(fake.posts[0][1]["body"].startswith("<!-- loom:report v1 -->"))


if __name__ == "__main__":
    unittest.main()
