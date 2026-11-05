"""A journal on a public goal home names another repo's issue in words, never
as a [owner/]name#N reference (loom#1750). The App-boundary scrub rewrites
only owner/name#N refs to private repos, so the bare name#N form reached the
goal home."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import journal  # noqa: E402
from bus.factory import SCAN_REF_RE  # noqa: E402
from bus.github import GitHub  # noqa: E402

OWNER, NAME = "example-owner", "example-repo"
REPO = "%s/%s" % (OWNER, NAME)
OTHER = "other-repo"
M = journal.JOURNAL_MARKER


def ref(repo, number):
    return "%s#%d" % (repo, number)


def tick(at, *lines):
    return "\n".join([M, "Tick %s, lane-machine" % at] + list(lines))


class Fake:
    """Answers GET /user, the day's comments read, PATCH and POST."""

    def __init__(self, comments, login="me"):
        self.comments = comments
        self.login = login
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1]
        self.calls.append((method, path, json.loads(body) if body else None))
        if method == "GET" and path == "/user":
            return 200, {}, json.dumps({"login": self.login}).encode()
        if method == "GET" and "/comments" in path:
            return 200, {}, json.dumps(self.comments).encode()
        if method in ("PATCH", "POST"):
            return 200 if method == "PATCH" else 201, {}, b"{}"
        raise AssertionError("unexpected %s %s" % (method, path))

    def written(self):
        [(method, _, body)] = [c for c in self.calls if c[0] in ("PATCH", "POST")]
        return method, body["body"]


def foreign(text):
    """The refs in `text` to a repo other than the goal home."""
    return [m.group(0) for m in SCAN_REF_RE.finditer(text)
            if m.group(2).lower() != NAME or (m.group(1) or OWNER).lower() != OWNER]


class JournalRefTests(unittest.TestCase):
    def test_another_repos_issue_is_posted_in_words(self):
        fake = Fake([])
        text = tick("2030-01-01T09:00:00Z", "- Shipped: %s and %s"
                    % (ref(OTHER, 12), ref("someone/" + OTHER, 13)))
        journal.post(GitHub("t", transport=fake), REPO, 7, text)
        method, body = fake.written()
        self.assertEqual(method, "POST")
        self.assertEqual(foreign(body), [])
        self.assertIn("issue 12 of another repo", body)
        self.assertIn("issue 13 of another repo", body)

    def test_the_goal_homes_own_refs_are_kept(self):
        fake = Fake([])
        own = "#5, %s and %s" % (ref(NAME, 6), ref(REPO, 7))
        journal.post(GitHub("t", transport=fake), REPO, 7,
                     tick("2030-01-01T09:00:00Z", "- Shipped: " + own))
        _, body = fake.written()
        self.assertIn(own, body)
        self.assertNotIn("another repo", body)

    def test_an_edit_of_todays_journal_is_rewritten_too(self):
        # Today's journal predates the rewrite and still names another repo.
        first = tick("2030-01-01T09:00:00Z", "- Shipped: " + ref(OTHER, 21))
        fake = Fake([{"id": 41, "body": first, "created_at": "2030-01-01T09:00:05Z",
                      "user": {"login": "me"}}])
        second = tick("2030-01-01T10:00:00Z", "- Shipped: " + ref(OTHER, 22))
        journal.post(GitHub("t", transport=fake), REPO, 7, second)
        method, body = fake.written()
        self.assertEqual(method, "PATCH")
        self.assertEqual(foreign(body), [])
        self.assertIn("issue 21 of another repo", body)
        self.assertIn("issue 22 of another repo", body)


if __name__ == "__main__":
    unittest.main()
