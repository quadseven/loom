"""A person's token leaves each thread Loom writes on (2026-10-20: the
operator's token commented on 101 tickets in a day, each comment subscribed
the operator, and every later bot comment there was an email)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"


class Fake:
    def __init__(self, graphql_status=200):
        self.calls = []
        self.graphql_status = graphql_status

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[1]
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        if path == "/graphql":
            return self.graphql_status, {}, b'{"data": {"updateSubscription": {}}}'
        if method == "POST" and path == "/repos/%s/issues" % REPO:
            return 201, {}, json.dumps({"number": 9, "node_id": "I_nine"}).encode()
        if method == "POST":
            return 201, {}, b'{"id": 1}'
        if path == "/repos/%s/issues/7" % REPO:
            return 200, {}, b'{"number": 7, "node_id": "I_seven"}'
        return 200, {}, b"{}"

    def unsubscribed(self):
        return [c[2]["variables"]["id"] for c in self.calls
                if c[1] == "/graphql" and "UNSUBSCRIBED" in c[2]["query"]]


class UnsubscribeTests(unittest.TestCase):
    def test_a_comment_and_a_new_issue_leave_their_threads(self):
        fake = Fake()
        gh = GitHub("token", transport=fake, unsubscribe=True)
        gh.rest("POST", "/repos/%s/issues/7/comments" % REPO, {"body": "x"})
        gh.rest("POST", "/repos/%s/issues/7/comments" % REPO, {"body": "y"})
        gh.rest("POST", "/repos/%s/issues" % REPO, {"title": "t", "body": "b"})
        self.assertEqual(fake.unsubscribed(), ["I_seven", "I_seven", "I_nine"])
        # The issue's node id is read once.
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "GET"), 1)

    def test_other_writes_and_a_client_without_the_setting_leave_nothing(self):
        fake = Fake()
        gh = GitHub("token", transport=fake, unsubscribe=True)
        gh.rest("POST", "/repos/%s/issues/7/labels" % REPO, {"labels": ["x"]})
        gh.rest("PATCH", "/repos/%s/issues/comments/3" % REPO, {"body": "x"})
        plain = GitHub("token", transport=fake)
        plain.rest("POST", "/repos/%s/issues/7/comments" % REPO, {"body": "x"})
        self.assertEqual(fake.unsubscribed(), [])

    def test_a_refused_unsubscribe_is_kept_and_the_write_stands(self):
        fake = Fake(graphql_status=403)
        gh = GitHub("token", transport=fake, unsubscribe=True)
        resp = gh.rest("POST", "/repos/%s/issues/7/comments" % REPO, {"body": "x"})
        self.assertEqual(resp.data, {"id": 1})
        self.assertEqual(len(gh.unsubscribe_failures), 1)

    def test_the_brain_and_bus_commands_turn_it_on(self):
        root = os.path.join(os.path.dirname(__file__), "..", "bus")
        for name in ("brain.py", "__main__.py"):
            with open(os.path.join(root, name)) as fh:
                self.assertIn("GitHub(auth_from_env(), unsubscribe=True)", fh.read(), name)


if __name__ == "__main__":
    unittest.main()
