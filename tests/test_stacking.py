"""Pull request stack safety helpers."""

import re
import unittest

from bus import stacking
from bus.github import Response


REPO = "example-owner/example-repo"


class FakeGitHub:
    def __init__(self):
        self.pulls = {}
        self.files = {}

    def paginate(self, path):
        if path == "/repos/%s/pulls?state=open&per_page=100" % REPO:
            return [pull for pull in self.pulls.values() if pull["state"] == "open"]
        raise AssertionError("unexpected paginate %s" % path)

    def rest(self, method, path, body=None):
        if method != "GET":
            raise AssertionError("unexpected %s %s" % (method, path))
        if path.startswith("/repos/%s/pulls/" % REPO) and "/files?" not in path:
            number = int(path.rsplit("/", 1)[1])
            return Response(200, self.pulls[number])
        match = re.search(r"/pulls/(\d+)/files\?per_page=100&page=(\d+)$", path)
        if match:
            number = int(match.group(1))
            page = int(match.group(2))
            return Response(200, self.files.get(number, [])[(page - 1) * 100:page * 100])
        raise AssertionError("unexpected GET %s" % path)


def pull(number, created, patch, base="main"):
    return ({"number": number, "state": "open", "created_at": created,
             "base": {"ref": base}},
            [{"filename": "bus/example.py", "patch": patch}])


class OverlapTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.pulls[10], self.gh.files[10] = pull(10, "2026-01-01T00:00:00Z",
                                                    "@@ -2,2 +2,2 @@\n-a\n+b")
        self.gh.pulls[20], self.gh.files[20] = pull(20, "2026-01-03T00:00:00Z",
                                                    "@@ -3 +3 @@\n-a\n+b")

    def test_returns_older_overlapping_pull_request(self):
        self.assertEqual(stacking.overlapping_open_pull_request(self.gh, REPO, 20), 10)

    def test_allows_disjoint_hunks_in_the_same_file(self):
        self.gh.files[20] = [{"filename": "bus/example.py",
                              "patch": "@@ -8 +8 @@\n-a\n+b"}]
        self.assertIsNone(stacking.overlapping_open_pull_request(self.gh, REPO, 20))

    def test_ignores_pull_requests_on_other_bases(self):
        self.gh.pulls[10]["base"] = {"ref": "release"}
        self.assertIsNone(stacking.overlapping_open_pull_request(self.gh, REPO, 20))
