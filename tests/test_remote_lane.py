import base64
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import lane


class FakeGitHub:
    def __init__(self, private=False):
        self.private = private
        self.issues = {4: {"number": 4, "title": "Add a feature", "body":
                           "Difficulty: medium\n\n## What\nAdd the feature.\n",
                           "created_at": "2026-01-01T00:00:00Z", "labels": [
                               {"name": "loom:ticket"}, {"name": "loom:queued"},
                               {"name": "lane:muse"}], "state": "open"}}
        self.comments = {4: []}
        self.calls = []

    def rest(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if path == "/repos/example/project":
            return type("Response", (), {"data": {"private": self.private,
                                                     "default_branch": "main"}})()
        if path.endswith("/issues/4") and method == "GET":
            return type("Response", (), {"data": self.issues[4]})()
        if path.endswith("/issues/4/labels") and method == "PUT":
            self.issues[4]["labels"] = [{"name": x} for x in body["labels"]]
        if path.endswith("/issues/4/comments") and method == "POST":
            self.comments[4].append({"body": body["body"], "created_at": "now"})
        if path.endswith("/contents/.loom/sensitive-paths"):
            return type("Response", (), {"data": {"content": base64.b64encode(b"secret/*").decode()}})()
        if path.endswith("/pulls") and method == "POST":
            return type("Response", (), {"data": {"number": 9, "html_url": "https://example.com/pr/9"}})()
        return type("Response", (), {"data": {}})()

    def paginate(self, path):
        if "issues?state=open&labels=loom:queued" in path:
            return [self.issues[4]] if "loom:queued" in [x["name"] for x in self.issues[4]["labels"]] else []
        if "issues?state=open&labels=loom:running" in path:
            return [self.issues[4]] if "loom:running" in [x["name"] for x in self.issues[4]["labels"]] else []
        if path.endswith("/issues/4/comments"):
            return self.comments[4]
        if "/pulls?state=all" in path:
            return []
        return []


class RemoteLaneTests(unittest.TestCase):
    def test_claim_returns_public_queued_ticket_once_without_local_brief(self):
        gh = FakeGitHub()
        got = lane.claim(gh, ["example/project"], now=1000)
        self.assertEqual(got["number"], 4)
        self.assertIn("Repo: example/project", got["brief"])
        self.assertEqual(len(gh.comments[4]), 1)
        self.assertEqual(gh.issues[4]["labels"][-1]["name"], "loom:running")
        self.assertIsNone(lane.claim(gh, ["example/project"], now=1001))

    def test_private_repo_is_never_claimed_and_expired_claim_returns_to_queue(self):
        private = FakeGitHub(private=True)
        self.assertIsNone(lane.claim(private, ["example/project"], now=1000))
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        lane._recover_and_count(gh, ["example/project"], "muse", 1000 + lane.CLAIM_AGE + 1)
        labels = [x["name"] for x in gh.issues[4]["labels"]]
        self.assertIn("loom:queued", labels)
        self.assertTrue(any(x["body"].startswith("[transient]") for x in gh.comments[4]))

    def test_pr_has_intent_class_and_refs_and_moves_ticket_to_pr(self):
        gh = FakeGitHub()
        with mock.patch.object(lane, "_git", side_effect=["loom/4-add-a-feature", "bus/lane.py\ntests/test_lane.py"]):
            with mock.patch.object(lane.change_class, "classify", return_value="logic"):
                with mock.patch.object(lane.change_class, "count_by_class", return_value={
                        "logic": 1, "tests": 1, "docs": 0, "sensitive": 0}):
                    made = lane.open_pr(gh, "example/project", 4, "main")
        self.assertEqual(made["number"], 9)
        pull = next(c for c in gh.calls if c[0] == "POST" and c[1].endswith("/pulls"))[2]
        self.assertIn("## Intent", pull["body"])
        self.assertIn("Class: logic", pull["body"])
        self.assertIn("Refs #4", pull["body"])
        self.assertIn("loom:pr", [x["name"] for x in gh.issues[4]["labels"]])


if __name__ == "__main__":
    unittest.main()
