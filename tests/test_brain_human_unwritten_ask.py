import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub


REPO = "example-owner/example-repo"


class HumanIssueTransport:
    def __init__(self, labels):
        self.labels = labels
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/repos/%s/issues/9" % REPO:
            data = {"number": 9, "state": "open",
                    "labels": [{"name": label} for label in self.labels]}
        elif method == "GET" and path.startswith("/repos/%s/issues/9/comments" % REPO):
            data = []
        else:
            data = {}
        return 200, {}, json.dumps(data).encode()


class HumanUnwrittenAskTests(unittest.TestCase):
    def run_human(self, labels):
        transport = HumanIssueTransport(labels)
        brain.human(GitHub("token", transport=transport), REPO, 9,
                    "Kind: decision\nChoose the deployment window.")
        return transport

    def test_old_ask_label_without_comment_gets_one_written_ask_and_canonical_label(self):
        transport = self.run_human(["needs-operator"])
        comments = [call for call in transport.calls
                    if call[0] == "POST" and call[1].endswith("/issues/9/comments")]
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0][2]["body"].startswith(brain.HUMAN_MARKER))
        labels = [call for call in transport.calls
                  if call[0] == "POST" and call[1].endswith("/issues/9/labels")]
        self.assertEqual(labels, [("POST", "/repos/%s/issues/9/labels" % REPO,
                                  {"labels": ["needs:human"]})])

    def test_old_ask_label_without_comment_removes_triage_and_adds_canonical_label(self):
        transport = self.run_human(["needs-operator", "loom:triage"])
        self.assertTrue(any(call[0] == "DELETE" and
                            call[1].endswith("/labels/loom%3Atriage")
                            for call in transport.calls))
        self.assertTrue(any(call[0] == "POST" and call[1].endswith("/labels") and
                            call[2] == {"labels": ["needs:human"]}
                            for call in transport.calls))


if __name__ == "__main__":
    unittest.main()
