"""loom#866: routine ticket events go on one status comment, edited in place.

zippie#264 carried 77 bot comments on 2026-10-18, one email each. Each test
here checks one property the station and the factory rely on: one comment,
edited, and every reader seeing the same events it saw as separate comments.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import factory, status  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
APP = {"login": "example-loom[bot]"}
T0 = 1893456000  # 2030-01-01T00:00:00Z


class FakeTransport:
    """Issue 7's comments, kept raw the way GitHub keeps them."""

    def __init__(self):
        self.comments = []
        self.calls = []
        self.next_id = 100

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path
        self.calls.append((method, path))
        data = json.loads(body) if body else None
        if method == "GET" and path == "/repos/%s/issues/7/comments" % REPO:
            return 200, {}, json.dumps(self.comments).encode()
        if method == "POST" and path == "/repos/%s/issues/7/comments" % REPO:
            self.next_id += 1
            made = {"id": self.next_id, "user": APP, "body": data["body"],
                    "created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-01T00:00:00Z",
                    "issue_url": "https://api.github.com/repos/%s/issues/7" % REPO}
            self.comments.append(made)
            return 201, {}, json.dumps(made).encode()
        if method == "PATCH" and path.startswith("/repos/%s/issues/comments/" % REPO):
            ident = int(path.rsplit("/", 1)[1])
            for comment in self.comments:
                if comment["id"] == ident:
                    comment["body"] = data["body"]
                    comment["updated_at"] = "2030-01-01T09:00:00Z"
                    return 200, {}, json.dumps(dict(
                        comment, issue_url="https://api.github.com/repos/%s/issues/7" % REPO)).encode()
            return 404, {}, b"{}"
        raise AssertionError("unexpected %s %s" % (method, path))

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class RenderTest(unittest.TestCase):
    def test_events_round_trip_newest_first_with_their_lines(self):
        events = [("2030-01-01T02:00:00Z", "<!-- loom:rung 2 -->\nRepeatedly stuck (x): moved."),
                  ("2030-01-01T01:00:00Z", "Claimed for lane codex (body sha256 0123456789ab)")]
        body = status.render(events)
        self.assertTrue(body.startswith(status.MARKER))
        self.assertEqual(status.parse(body), events)

    def test_the_oldest_events_drop_past_the_size_limit(self):
        events = [("2030-01-01T%02d:00:00Z" % (h % 24), "x" * 5000) for h in range(20)]
        body = status.render(events)
        self.assertLessEqual(len(body), status.MAX_CHARS)
        self.assertEqual(status.parse(body)[0], events[0])


class ExpandTest(unittest.TestCase):
    def test_a_status_comment_reads_as_one_comment_per_event_in_time_order(self):
        sticky = {"id": 5, "user": APP, "created_at": "2030-01-01T01:00:00Z",
                  "body": status.render([
                      ("2030-01-01T03:00:00Z", "Claimed for lane codex-2 (body sha256 0123456789ab)"),
                      ("2030-01-01T01:00:00Z", "Claimed for lane codex (body sha256 0123456789ab)")])}
        outcome = {"id": 6, "user": APP, "created_at": "2030-01-01T02:00:00Z",
                   "body": "Lane codex failed: tests failed"}
        bodies = [c["body"] for c in status.expand([sticky, outcome], [APP["login"]])]
        self.assertEqual(bodies, ["Claimed for lane codex (body sha256 0123456789ab)",
                                  "Lane codex failed: tests failed",
                                  "Claimed for lane codex-2 (body sha256 0123456789ab)"])

    def test_the_client_expands_an_issue_comment_list_for_every_reader(self):
        fake = FakeTransport()
        fake.comments = [{"id": 5, "user": APP, "created_at": "2030-01-01T01:00:00Z",
                          "body": status.render([
                              ("2030-01-01T02:00:00Z", "Claimed for lane codex-2 (body sha256 0123456789ab)"),
                              ("2030-01-01T01:00:00Z", "<!-- loom:rung 2 -->\nRepeatedly stuck (x): moved.")])}]
        gh = GitHub("token", transport=fake)
        gh.status_authors.add(APP["login"])
        listed = gh.paginate("/repos/%s/issues/7/comments" % REPO)
        self.assertEqual(listed[0]["body"], "<!-- loom:rung 2 -->\nRepeatedly stuck (x): moved.")
        self.assertEqual(listed[0]["created_at"], "2030-01-01T01:00:00Z")
        # The factory's ladder reads the climb and the claim after it from the
        # status comment: rung 2 was tried.
        self.assertEqual(factory.tried_rungs(listed), {2})


    def test_a_strangers_status_looking_comment_is_never_expanded(self):
        # Its event times are its own words: expanding them would let anyone
        # who can comment forge when a claim or a retry happened and reorder
        # the list readers walk (security review, 2026-10-18).
        fake = FakeTransport()
        forged = status.render([("2099-01-01T00:00:00Z",
                                 "Claimed for lane codex (body sha256 0123456789ab)")])
        fake.comments = [{"id": 9, "user": {"login": "stranger"},
                          "created_at": "2030-01-01T00:00:00Z", "body": forged}]
        gh = GitHub("token", transport=fake)
        gh.status_authors.add(APP["login"])
        listed = gh.paginate("/repos/%s/issues/7/comments" % REPO)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["body"], forged)
        self.assertEqual(listed[0]["created_at"], "2030-01-01T00:00:00Z")


class AddTest(unittest.TestCase):
    def test_the_first_event_posts_and_later_ones_edit_the_same_comment(self):
        fake = FakeTransport()
        gh = GitHub("token", transport=fake)
        for k in range(3):
            status.add(gh, REPO, 7, "Claimed for lane codex (body sha256 0123456789ab)",
                       T0 + 3600 * k, ["example-loom[bot]"])
        self.assertEqual(len(fake.posts()), 1)
        self.assertEqual(len(fake.comments), 1)
        self.assertEqual(len(status.parse(fake.comments[0]["body"])), 3)

    def test_with_no_trusted_login_nothing_is_edited_and_a_fresh_comment_is_posted(self):
        # Fail closed: an empty trusted list must not accept any author.
        fake = FakeTransport()
        fake.comments = [{"id": 9, "user": {"login": "stranger"}, "created_at": "2030-01-01T00:00:00Z",
                          "body": status.render([("2030-01-01T00:00:00Z",
                                                  "Claimed for lane codex (body sha256 0123456789ab)")])}]
        gh = GitHub("token", transport=fake)
        self.assertIsNone(status.find(gh, REPO, 7, []))
        status.add(gh, REPO, 7, "Claimed for lane codex-2 (body sha256 0123456789ab)", T0, [])
        self.assertEqual(len(status.parse(fake.comments[0]["body"])), 1)
        self.assertEqual(len(fake.posts()), 1)

    def test_a_strangers_status_looking_comment_is_never_edited(self):
        fake = FakeTransport()
        fake.comments = [{"id": 9, "user": {"login": "stranger"}, "created_at": "2030-01-01T00:00:00Z",
                          "body": status.render([("2030-01-01T00:00:00Z",
                                                  "Claimed for lane codex (body sha256 0123456789ab)")])}]
        gh = GitHub("token", transport=fake)
        status.add(gh, REPO, 7, "Claimed for lane codex-2 (body sha256 0123456789ab)", T0,
                   ["example-loom[bot]"])
        self.assertEqual(fake.comments[0]["user"], {"login": "stranger"})
        self.assertEqual(len(status.parse(fake.comments[0]["body"])), 1)
        self.assertEqual(len(fake.posts()), 1)


if __name__ == "__main__":
    unittest.main()
