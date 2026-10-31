"""A person wait on a held pull request is withdrawn by the station when
that pull request merges or closes: the wait labels come off, one note is
posted, the ticket follows the merge or closed-unmerged state rule, and
the Needs-you list (built from those labels) no longer shows the ask."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import answer, operator_hold, station  # noqa: E402
from bus.github import GitHubError  # noqa: E402
from dashboard import github as dash_github, needs_you, operators  # noqa: E402

REPO = "acme/widgets"
OPERATOR = "operator-one"
MERGED_AT = "2030-01-02T03:04:05Z"
CLOSED_AT = "2030-01-03T04:05:06Z"


class Resp:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status


class FakeGitHub:
    """The GitHub client surface the station step uses: rest and paginate."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.pulls = {}
        self.posts = []
        self.label_writes = []
        self.requests = 0

    def add_issue(self, number, labels, ask_body, body=""):
        self.issues[number] = {
            "number": number, "title": "Ticket %d" % number, "state": "open",
            "body": body, "user": {"login": OPERATOR},
            "created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-01T00:00:00Z",
            "html_url": "https://example.com/%s/issues/%d" % (REPO, number),
            "labels": [{"name": name} for name in labels]}
        self.comments[number] = [
            {"id": 1, "body": ask_body, "user": {"login": OPERATOR},
             "created_at": "2030-01-01T01:00:00Z"}] if ask_body else []

    def names(self, number):
        return [label["name"] for label in self.issues[number]["labels"]]

    def rest(self, method, path, body=None, ok=()):
        self.requests += 1
        pull = re.fullmatch(r"/repos/.+/pulls/(\d+)", path)
        if method == "GET" and pull:
            found = self.pulls.get(int(pull.group(1)))
            if found is None:
                raise GitHubError("not found", 404)
            return Resp(dict(found))
        comment = re.fullmatch(r"/repos/.+/issues/(\d+)/comments", path)
        if method == "POST" and comment:
            number = int(comment.group(1))
            self.posts.append((number, body["body"]))
            self.comments[number].append(
                {"id": 100 + len(self.posts), "body": body["body"],
                 "user": {"login": OPERATOR}, "created_at": "2030-01-04T00:00:00Z"})
            return Resp({"id": 100 + len(self.posts)}, 201)
        labels = re.fullmatch(r"/repos/.+/issues/(\d+)/labels", path)
        if method == "PUT" and labels:
            number = int(labels.group(1))
            self.label_writes.append((number, list(body["labels"])))
            self.issues[number]["labels"] = [{"name": name} for name in body["labels"]]
            return Resp([])
        raise AssertionError("unexpected call %s %s" % (method, path))

    def paginate(self, path):
        if path.startswith("/repos/%s/issues?" % REPO):
            return [issue for issue in self.issues.values() if issue["state"] == "open"]
        comments = re.fullmatch(r"/repos/.+/issues/(\d+)/comments\?per_page=100", path)
        if comments:
            return list(self.comments[int(comments.group(1))])
        raise AssertionError("unexpected paginate %s" % path)


def held_ask(pr_number):
    """The loom:human ask a held lane pull request leaves on its ticket."""
    return "%s\n%s\nKind: decision" % (
        "<!-- loom:human v1 -->", operator_hold.ask(pr_number, ["operator steps are declared"]))


class HeldPrWaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gh = FakeGitHub()
        settings = station.Settings(os.path.join(self.tmp.name, "home"), [REPO],
                                    [OPERATOR], ["acme"])
        self.station = station.Station(self.gh, settings, now=1893456000.0)

    def run_step(self):
        self.station.withdraw_held_pr_waits()

    def test_merged_held_pull_request_withdraws_the_wait_once(self):
        self.gh.add_issue(7, ["loom:ticket", "loom:pr", "loom:human", "needs:operator-one"],
                          held_ask(99))
        self.gh.pulls[99] = {"number": 99, "state": "closed", "merged": True,
                             "merged_at": MERGED_AT, "closed_at": MERGED_AT, "body": ""}
        self.run_step()
        names = self.gh.names(7)
        self.assertNotIn("loom:human", names)
        self.assertNotIn("needs:operator-one", names)
        self.assertNotIn("needs-operator", names)
        self.assertIn("loom:merged", names)
        self.assertNotIn("loom:pr", names)
        self.assertEqual(self.gh.posts, [(7, "Pull request #99 merged at %s, so this no "
                                             "longer waits on a person." % MERGED_AT)])
        self.run_step()
        self.assertEqual(len(self.gh.posts), 1)
        self.assertEqual(self.gh.names(7), names)

    def test_closed_unmerged_held_pull_request_withdraws_and_requeues_by_the_closed_rule(self):
        self.gh.add_issue(7, ["loom:ticket", "loom:pr", "loom:human"], held_ask(99))
        self.gh.pulls[99] = {"number": 99, "state": "closed", "merged": False,
                             "merged_at": None, "closed_at": CLOSED_AT, "body": ""}
        self.run_step()
        names = self.gh.names(7)
        self.assertNotIn("loom:human", names)
        self.assertIn("loom:closed-unmerged", names)
        self.assertNotIn("loom:pr", names)
        self.assertEqual(len(self.gh.posts), 1)
        self.assertIn("Pull request #99 closed without merging", self.gh.posts[0][1])
        self.assertIn("no longer waits on a person", self.gh.posts[0][1])
        self.run_step()
        self.assertEqual(len(self.gh.posts), 1)

    def test_open_held_pull_request_leaves_the_issue_unchanged(self):
        self.gh.add_issue(7, ["loom:ticket", "loom:pr", "loom:human", "needs:operator-one"],
                          held_ask(99))
        self.gh.pulls[99] = {"number": 99, "state": "open", "merged": False,
                             "merged_at": None, "closed_at": None, "body": ""}
        before = self.gh.names(7)
        self.run_step()
        self.assertEqual(self.gh.names(7), before)
        self.assertEqual(self.gh.posts, [])
        self.assertEqual(self.gh.label_writes, [])

    def test_a_newer_ask_that_is_not_a_held_pull_request_keeps_the_wait(self):
        self.gh.add_issue(7, ["loom:ticket", "loom:pr", "loom:human"], held_ask(99))
        self.gh.comments[7].append(
            {"id": 2, "body": "<!-- loom:human v1 -->\nBuy the domain.\nKind: money",
             "user": {"login": OPERATOR}, "created_at": "2030-01-02T00:00:00Z"})
        self.gh.pulls[99] = {"number": 99, "state": "closed", "merged": True,
                             "merged_at": MERGED_AT, "closed_at": MERGED_AT, "body": ""}
        self.run_step()
        self.assertIn("loom:human", self.gh.names(7))
        self.assertEqual(self.gh.posts, [])

    def test_a_merge_or_hold_operator_ask_is_withdrawn_when_its_pull_request_merges(self):
        ask = ("<!-- loom:human v1 -->\nKind: decision\n"
               "Merge #99 now or hold it until the window.")
        self.gh.add_issue(8, ["loom:ticket", "loom:pr", "needs:operator-one"], ask)
        self.gh.pulls[99] = {"number": 99, "state": "closed", "merged": True,
                             "merged_at": MERGED_AT, "closed_at": MERGED_AT, "body": ""}
        self.run_step()
        self.assertNotIn("needs:operator-one", self.gh.names(8))
        self.assertIn("loom:merged", self.gh.names(8))
        self.assertEqual(len(self.gh.posts), 1)

    def needs_you_entries(self):
        """The Needs-you list, from the label-based sources the dashboard
        reads: loom:human and needs-operator items as operator asks, and
        needs:<login> items as needs records."""
        issues = [issue for issue in self.gh.issues.values() if issue["state"] == "open"]
        operator_asks = [dash_github._operator_item(REPO, issue, "loom_human")
                         for issue in issues if "loom:human" in self.gh.names(issue["number"])]
        operator_asks += [dash_github._operator_item(REPO, issue, "needs_operator")
                          for issue in issues
                          if "needs-operator" in self.gh.names(issue["number"])
                          and issue["number"] not in {item["number"] for item in operator_asks}]
        needs = [operators.needs_record(REPO, issue) for issue in issues
                 if operators.needs_of(self.gh.names(issue["number"]))]
        return needs_you.entries(operator_asks, needs, [REPO])

    def test_the_needs_you_list_no_longer_shows_a_withdrawn_ask(self):
        self.gh.add_issue(7, ["loom:ticket", "loom:pr", "loom:human", "needs:operator-one"],
                          held_ask(99))
        self.gh.pulls[99] = {"number": 99, "state": "closed", "merged": True,
                             "merged_at": MERGED_AT, "closed_at": MERGED_AT, "body": ""}
        self.assertEqual([entry["number"] for entry in self.needs_you_entries()], [7])
        self.run_step()
        self.assertEqual(self.needs_you_entries(), [])


class HeldPrParseTest(unittest.TestCase):
    def test_held_pr_reads_both_ask_shapes(self):
        self.assertEqual(answer.held_pr(operator_hold.ask(42, ["steps"])), 42)
        self.assertEqual(answer.held_pr("Merge #12 now or hold it until the window."), 12)
        self.assertEqual(answer.held_pr("Please merge pull request #34, or keep holding it."), 34)
        self.assertIsNone(answer.held_pr("Buy the domain."))
        self.assertIsNone(answer.held_pr("Merge the branch when ready."))


if __name__ == "__main__":
    unittest.main()
