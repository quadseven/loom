"""IssueReads refreshes an issue after a write in the same pass."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus.github import GitHub, Response
from bus.issue_reads import once
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
ISSUE_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)$")
LABELS_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)/labels$")


class IssueWriteFake:
    """Stateful GitHub transport that counts requests by method and path."""

    def __init__(self):
        self.calls = Counter()
        self.issues = {
            1: {"number": 1, "title": "Before", "labels": []},
            2: {"number": 2, "title": "Other", "labels": []},
        }

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        self.calls[(method, path)] += 1
        data = json.loads(body) if body else {}
        found = ISSUE_PATH.fullmatch(path)
        if method == "GET" and found:
            return self._reply(self.issues[int(found.group(1))])
        found = LABELS_PATH.fullmatch(path)
        if method == "POST" and found:
            issue = self.issues[int(found.group(1))]
            issue["labels"] = [{"name": name} for name in data["labels"]]
            return self._reply(issue["labels"])
        found = ISSUE_PATH.fullmatch(path)
        if method == "PATCH" and found:
            issue = self.issues[int(found.group(1))]
            issue.update(data)
            return self._reply(issue)
        raise AssertionError("unexpected request: %s %s" % (method, path))

    @staticmethod
    def _reply(body):
        return 200, {}, json.dumps(body).encode()


class PassReadWriteFake:
    """IssueReads client fake for cached list endpoints."""

    def __init__(self):
        self.calls = Counter()

    def rest(self, method, path, body=None, etag=None, ok=()):
        self.calls[(method, path)] += 1
        return Response(200, {})

    def paginate(self, path):
        self.calls[("paginate", path)] += 1
        return [{"path": path}]


class BlockingListFake:
    """A list read that can finish after an issue write invalidates it."""

    def __init__(self):
        self.list_calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def rest(self, method, path, body=None, etag=None, ok=()):
        return Response(200, {})

    def paginate(self, path):
        self.list_calls += 1
        if self.list_calls == 1:
            self.started.set()
            if not self.release.wait(timeout=10):
                raise AssertionError("test did not release first list read")
            return [{"version": 1}]
        return [{"version": 2}]


class IssueReadsAfterWrites(unittest.TestCase):
    def test_issue_write_refreshes_that_issue_but_keeps_another_issue(self):
        cases = (
            ("PATCH", "/repos/%s/issues/1" % REPO, {"title": "After"},
             lambda issue: issue["title"], "After"),
            ("POST", "/repos/%s/issues/1/labels" % REPO,
             {"labels": ["refreshed"]},
             lambda issue: issue["labels"][0]["name"], "refreshed"),
        )
        for method, path, body, changed_value, expected in cases:
            with self.subTest(method=method):
                fake = IssueWriteFake()
                gh = once(GitHub("example-token", transport=fake))
                issue_path = "/repos/%s/issues/1" % REPO
                other_path = "/repos/%s/issues/2" % REPO

                before = gh.rest("GET", issue_path).data
                other_before = gh.rest("GET", other_path).data
                gh.rest(method, path, body)
                after = gh.rest("GET", issue_path).data
                other_after = gh.rest("GET", other_path).data

                self.assertEqual(before["title"], "Before")
                self.assertEqual(changed_value(after), expected)
                self.assertEqual(other_after, other_before)
                self.assertEqual(fake.calls[("GET", issue_path)], 2)
                self.assertEqual(fake.calls[("GET", other_path)], 1)

    def test_issue_write_drops_related_lists_and_the_repo_listing(self):
        fake = PassReadWriteFake()
        gh = once(fake)
        paths = (
            "/repos/%s/issues" % REPO,
            "/repos/%s/issues/1/comments" % REPO,
            "/repos/%s/issues/1/events" % REPO,
            "/repos/%s/issues/1/sub_issues" % REPO,
        )
        for path in paths:
            gh.paginate(path)
        gh.rest("PATCH", "/repos/%s/issues/1" % REPO, {"title": "After"})
        for path in paths:
            gh.paginate(path)

        for path in paths:
            self.assertEqual(fake.calls[("paginate", path)], 2, path)

    def test_a_list_read_started_before_a_write_is_not_kept_after_it(self):
        fake = BlockingListFake()
        gh = once(fake)
        path = "/repos/%s/issues" % REPO
        result = []
        reader = threading.Thread(target=lambda: result.extend(gh.paginate(path)))

        reader.start()
        self.assertTrue(fake.started.wait(timeout=10))
        gh.rest("PATCH", "/repos/%s/issues/1" % REPO, {"title": "After"})
        fake.release.set()
        reader.join(timeout=10)

        self.assertFalse(reader.is_alive())
        self.assertEqual(gh.paginate(path), [{"version": 2}])
        self.assertEqual(fake.list_calls, 2)

    def test_a_comment_write_by_id_drops_every_comments_list_of_that_repo(self):
        other = "example-owner/other-repo"
        for method in ("PATCH", "DELETE", "POST"):
            with self.subTest(method=method):
                fake = PassReadWriteFake()
                gh = once(fake)
                dropped = ("/repos/%s/issues/1/comments" % REPO,
                           "/repos/%s/issues/2/comments" % REPO)
                kept = ("/repos/%s/issues/1/events" % REPO,
                        "/repos/%s/issues/1/sub_issues" % REPO,
                        "/repos/%s/issues" % REPO,
                        "/repos/%s/issues/1/comments" % other)
                for path in dropped + kept:
                    gh.paginate(path)
                gh.rest(method, "/repos/%s/issues/comments/77" % REPO, {"body": "x"})
                for path in dropped + kept:
                    gh.paginate(path)

                for path in dropped:
                    self.assertEqual(fake.calls[("paginate", path)], 2, path)
                for path in kept:
                    self.assertEqual(fake.calls[("paginate", path)], 1, path)

    def test_a_comment_write_leaves_kept_single_issue_answers_alone(self):
        fake = PassReadWriteFake()
        gh = once(fake)
        issue_path = "/repos/%s/issues/1" % REPO
        gh.rest("GET", issue_path)
        gh.rest("PATCH", "/repos/%s/issues/comments/77" % REPO, {"body": "x"})
        gh.rest("POST", "/repos/%s/issues" % REPO, {"title": "New"})
        gh.rest("GET", issue_path)

        self.assertEqual(fake.calls[("GET", issue_path)], 1)

    def test_a_comments_read_started_before_a_comment_write_is_not_kept(self):
        fake = BlockingListFake()
        gh = once(fake)
        path = "/repos/%s/issues/1/comments" % REPO
        result = []
        reader = threading.Thread(target=lambda: result.extend(gh.paginate(path)))

        reader.start()
        self.assertTrue(fake.started.wait(timeout=10))
        gh.rest("PATCH", "/repos/%s/issues/comments/77" % REPO, {"body": "x"})
        fake.release.set()
        reader.join(timeout=10)

        self.assertFalse(reader.is_alive())
        self.assertEqual(gh.paginate(path), [{"version": 2}])
        self.assertEqual(fake.list_calls, 2)

    def test_creating_an_issue_drops_only_that_repos_listing(self):
        other = "example-owner/other-repo"
        fake = PassReadWriteFake()
        gh = once(fake)
        dropped = ("/repos/%s/issues" % REPO,
                   "/repos/%s/issues?state=open&per_page=100" % REPO)
        kept = ("/repos/%s/issues/1/comments" % REPO,
                "/repos/%s/issues/1/events" % REPO,
                "/repos/%s/issues/1/sub_issues" % REPO,
                "/repos/%s/issues" % other)
        for path in dropped + kept:
            gh.paginate(path)
        gh.rest("POST", "/repos/%s/issues" % REPO, {"title": "New"})
        for path in dropped + kept:
            gh.paginate(path)

        for path in dropped:
            self.assertEqual(fake.calls[("paginate", path)], 2, path)
        for path in kept:
            self.assertEqual(fake.calls[("paginate", path)], 1, path)

    def test_a_listing_read_started_before_an_issue_is_created_is_not_kept(self):
        fake = BlockingListFake()
        gh = once(fake)
        path = "/repos/%s/issues" % REPO
        result = []
        reader = threading.Thread(target=lambda: result.extend(gh.paginate(path)))

        reader.start()
        self.assertTrue(fake.started.wait(timeout=10))
        gh.rest("POST", "/repos/%s/issues" % REPO, {"title": "New"})
        fake.release.set()
        reader.join(timeout=10)

        self.assertFalse(reader.is_alive())
        self.assertEqual(gh.paginate(path), [{"version": 2}])
        self.assertEqual(fake.list_calls, 2)

    def test_reading_a_comment_by_id_keeps_every_list(self):
        fake = PassReadWriteFake()
        gh = once(fake)
        path = "/repos/%s/issues/1/comments" % REPO
        gh.paginate(path)
        gh.rest("GET", "/repos/%s/issues/comments/77" % REPO)
        gh.paginate(path)

        self.assertEqual(fake.calls[("paginate", path)], 1)


if __name__ == "__main__":
    unittest.main()
