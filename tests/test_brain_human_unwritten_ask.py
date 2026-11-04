from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub


REPO = "example-owner/example-repo"


def response(status, body):
    return status, {}, json.dumps(body).encode()


class Transport:
    def __init__(self, issue, comments):
        self.issue = issue
        self.comments = comments
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        self.calls.append((method, path, json.loads(body) if body else None))
        if method == "GET" and path == "/repos/%s/issues/6" % REPO:
            return response(200, self.issue)
        if method == "GET" and path.startswith("/repos/%s/issues/6/comments" % REPO):
            return response(200, self.comments)
        return response(201, {})

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def issue(label):
    return {"number": 6, "state": "open", "labels": [{"name": label}]}


class HumanUnwrittenAskTests(unittest.TestCase):
    def test_old_ask_label_without_comment_gets_written_ask_and_current_label(self):
        fake = Transport(issue("needs-operator"), [])

        brain.human(GitHub("t", transport=fake), REPO, 6,
                    "Kind: decision\nChoose the release date.")

        posts = [call for call in fake.writes() if call[1].endswith("/issues/6/comments")]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][2]["body"].startswith(brain.HUMAN_MARKER))
        self.assertIn(("POST", "/repos/%s/issues/6/labels" % REPO,
                       {"labels": ["needs:human"]}), fake.writes())

    def test_current_ask_label_with_written_ask_is_refused(self):
        fake = Transport(issue("needs:human"), [
            {"body": brain.HUMAN_MARKER + "\nChoose the release date."}])

        with self.assertRaisesRegex(ValueError, "already needs:human"):
            brain.human(GitHub("t", transport=fake), REPO, 6,
                        "Kind: decision\nChoose the release date.")

        self.assertEqual(fake.writes(), [])


if __name__ == "__main__":
    unittest.main()
