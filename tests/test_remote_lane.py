import base64
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import hunt_plan, lane, station  # noqa: E402

BODY = ("Difficulty: medium\n\n## Why\nThe feature is missing for every visitor.\n\n"
        "## What\nAdd the feature.\n\n## Acceptance criteria\n- [ ] It works.\n"
        "- [ ] A test fails without it.\n- [ ] It is live.\n\n## Size\n\nS\n\n"
        "## Out of scope\n- Other features.\n")


class FakeGitHub:
    def __init__(self, private=False, body=BODY):
        self.private = private
        self.issues = {4: {"number": 4, "title": "Add a feature", "body": body,
                           "created_at": "2026-01-01T00:00:00Z", "labels": [
                               {"name": "loom:ticket"}, {"name": "loom:queued"},
                               {"name": "lane:muse"}], "state": "open"}}
        self.comments = {4: [], 9: []}
        self.pulls = []
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
        m = re.search(r"/issues/(\d+)/comments$", path)
        if m and method == "POST":
            n = int(m.group(1))
            self.comments.setdefault(n, []).append(
                {"id": 100 + len(self.comments[n]), "body": body["body"], "created_at": "now"})
        m = re.search(r"/issues/comments/(\d+)$", path)
        if m and method == "PATCH":
            for found in self.comments.values():
                for comment in found:
                    if comment["id"] == int(m.group(1)):
                        comment["body"] = body["body"]
        if path.endswith("/contents/.loom/sensitive-paths"):
            return type("Response", (), {"data": {"content": base64.b64encode(b"secret/*").decode()}})()
        if path.endswith("/pulls") and method == "POST":
            return type("Response", (), {"data": {"number": 9, "html_url": "https://example.com/pr/9"}})()
        return type("Response", (), {"data": {}})()

    def paginate(self, path):
        names = [x["name"] for x in self.issues[4]["labels"]]
        if "issues?state=open&labels=loom:queued" in path:
            return [self.issues[4]] if "loom:queued" in names else []
        if "issues?state=open&labels=loom:running" in path:
            return [self.issues[4]] if "loom:running" in names else []
        if "issues?state=open&labels=loom:lanes" in path:
            return [{"number": 9}]
        m = re.search(r"/issues/(\d+)/comments$", path)
        if m:
            return self.comments.setdefault(int(m.group(1)), [])
        if "/pulls?state=all&head=" in path:
            return list(self.pulls)
        return []


def labels(gh):
    return [x["name"] for x in gh.issues[4]["labels"]]


class RemoteLaneTests(unittest.TestCase):
    def test_claim_returns_public_queued_ticket_once_without_local_brief(self):
        gh = FakeGitHub()
        got = lane.claim(gh, ["example/project"], now=1000)
        self.assertEqual(got["number"], 4)
        self.assertIn("Repo: example/project", got["brief"])
        self.assertEqual(len(gh.comments[4]), 1)
        self.assertEqual(labels(gh)[-1], "loom:running")
        self.assertIsNone(lane.claim(gh, ["example/project"], now=1001))

    def test_private_repo_is_never_claimed_and_expired_claim_returns_to_queue(self):
        private = FakeGitHub(private=True)
        self.assertIsNone(lane.claim(private, ["example/project"], now=1000))
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        lane._recover_and_count(gh, ["example/project"], "muse", 1000 + lane.CLAIM_AGE + 1)
        self.assertIn("loom:queued", labels(gh))
        self.assertTrue(any(x["body"].startswith("[transient]") for x in gh.comments[4]))

    def test_a_claim_whose_branch_has_a_pull_request_is_never_handed_back(self):
        # Past CLAIM_AGE, but the branch has a pull request: the ticket moves
        # to loom:pr instead of going back to the queue.
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        gh.pulls = [{"number": 9, "head": {"ref": "loom/4-add-a-feature"}}]
        active = lane._recover_and_count(gh, ["example/project"], "muse",
                                          1000 + lane.PR_AGE + 1)
        self.assertEqual(active, 0)
        self.assertIn("loom:pr", labels(gh))
        self.assertNotIn("loom:queued", labels(gh))
        self.assertFalse(any(x["body"].startswith("[transient]") for x in gh.comments[4]))
        pulls = [c for c in gh.calls if "/pulls?" in c[1]]
        self.assertFalse(pulls)  # paginate is used, never a full pull listing
        # The lookup names the branch instead of listing every pull request.
        self.assertTrue(lane._pull_for(gh, "example/project", "loom/4-add-a-feature"))

    def test_a_heartbeat_keeps_a_claim_and_a_claim_with_no_pr_expires_after_pr_age(self):
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        lane.heartbeat(gh, "example/project", 4, now=1000 + lane.CLAIM_AGE - 10)
        self.assertEqual(lane._recover_and_count(
            gh, ["example/project"], "muse", 1000 + lane.CLAIM_AGE + 10), 1)
        lane._recover_and_count(gh, ["example/project"], "muse", 1000 + lane.PR_AGE + 1)
        self.assertIn("loom:queued", labels(gh))

    def test_the_lane_heartbeat_edits_one_comment(self):
        gh = FakeGitHub()
        lane.lane_heartbeat(gh, "example/project", now=1000)
        lane.lane_heartbeat(gh, "example/project", now=2000)
        self.assertEqual(len(gh.comments[9]), 1)
        self.assertIn(" 2000.000 ", gh.comments[9][0]["body"])

    def open_pr(self, gh):
        with mock.patch.object(lane, "_git", side_effect=["loom/4-add-a-feature",
                                                          "bus/lane.py\ntests/test_lane.py"]):
            with mock.patch.object(lane.change_class, "classify", return_value="logic"):
                with mock.patch.object(lane.change_class, "count_by_class", return_value={
                        "logic": 1, "tests": 1, "docs": 0, "sensitive": 0}):
                    return lane.open_pr(gh, "example/project", 4, "main")

    def test_the_pr_body_is_the_lane_body_the_review_bot_accepts(self):
        gh = FakeGitHub()
        made = self.open_pr(gh)
        self.assertEqual(made["number"], 9)
        body = next(c for c in gh.calls if c[0] == "POST" and c[1].endswith("/pulls"))[2]["body"]
        self.assertIn("## Intent", body)
        self.assertIn("Class: logic", body)
        self.assertEqual(hunt_plan.problems(body), [])
        self.assertTrue(body.rstrip().endswith("Refs example/project#4"))
        self.assertIsNone(re.search(r"\b(close[sd]?|fix(es|ed)?|resolve[sd]?)\s+#\d", body, re.I))
        self.assertIn("loom:pr", labels(gh))

    def test_a_ticket_whose_pr_would_fail_the_plan_checks_opens_nothing(self):
        gh = FakeGitHub(body="Difficulty: medium\n\n## What\nAdd the feature.\n")
        with self.assertRaisesRegex(ValueError, "plan checks"):
            self.open_pr(gh)
        self.assertFalse([c for c in gh.calls if c[0] == "POST" and c[1].endswith("/pulls")])


class TakesTest(unittest.TestCase):
    def test_a_queued_ticket_with_no_or_empty_body_is_skipped_not_a_crash(self):
        # tickets.parse returns (headers, rest) for any body, so [0] is a
        # dict ({} here) and the ticket is skipped for having no Difficulty.
        for body in (None, "", "no headers here"):
            with self.subTest(body=body):
                item = {"number": 5, "body": body,
                        "labels": [{"name": "loom:queued"}, {"name": "lane:muse"}]}
                self.assertFalse(lane._takes(item, "muse"))
        item["body"] = "Difficulty: easy\n\n## What\nx"
        self.assertTrue(lane._takes(item, "muse"))


class RemoteBeatTests(unittest.TestCase):
    def test_a_remote_lane_counts_as_up_for_its_own_window(self):
        # Muse ticks about every 30 minutes; a 40-minute-old beat is up for
        # it, while a local lane's window stays HEARTBEAT_MAX_AGE.
        self.assertTrue(station.remote_beat_fresh("muse", 1000.0, 1000.0 + 2400))
        self.assertFalse(station.remote_beat_fresh("muse", 1000.0, 1000.0 + 2 * 3600 + 1))
        self.assertFalse(station.remote_beat_fresh("codex", 1000.0, 1000.0 + 2400))
        self.assertFalse(station.remote_beat_fresh("muse", None, 1000.0))


if __name__ == "__main__":
    unittest.main()
