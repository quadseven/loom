"""Operator-claimed issues settle when their pull requests merge (loom#1204).

Pull requests never use closing keywords, so an issue an operator session
claims (a claim:<login> label, no loom: label) stayed open after its pull
request merged: nothing in Loom settled it. The station's
settle_claimed_tickets step closes one whose boxes ask only for merged code
or tests, with one evidence comment, and labels one with a live box.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import claimed, station  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
MERGE_SHA = "a1" * 20
HEAD_SHA = "b2" * 20
CODE_BODY = ("## Why\n\nIt was slow.\n\n## Acceptance criteria\n\n"
             "- [ ] The parser reads the new field.\n"
             "- [ ] A test fails without the change and passes with it.\n\n"
             "Size: S\n\n## Out of scope\n\nNothing else.\n")
LIVE_BODY = CODE_BODY.replace("- [ ] The parser reads the new field.",
                              "- [ ] Live: the deployed service answers within 24 hours.")
TEST_PATCH = "@@ -1,2 +1,6 @@\n+def test_parser_reads_the_new_field():\n+    assert True\n"
TEST_SOURCE = "import os\n\n\ndef test_parser_reads_the_new_field():\n    assert True\n"


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class FakeGitHub:
    """Only the reads and writes the claimed settle step makes."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.closed_pulls = []
        self.open_pulls = []
        self.check_runs = {}
        self.files = {}
        self.contents = {"tests/test_parser.py": TEST_SOURCE}
        self.writes = []
        self.created = []

    def add_issue(self, number, labels=("claim:example-login",), body=CODE_BODY,
                  title="The parser reads the new field", **extra):
        self.issues[number] = dict({"number": number, "title": title, "state": "open",
                                    "body": body, "user": {"login": "example-login"},
                                    "labels": [{"name": x} for x in labels]}, **extra)
        self.comments[number] = []

    def add_merged(self, pr, refs, elder="success", others=("success",), base="main",
                   head=HEAD_SHA, merge=MERGE_SHA, body=None):
        self.closed_pulls.append({
            "number": pr, "state": "closed", "title": "Read the new field",
            "merged_at": "2030-01-02T00:00:00Z", "merge_commit_sha": merge,
            "head": {"ref": "a-branch", "sha": head}, "base": {"ref": base},
            "body": body if body is not None else "## Why\n\nBecause.\n\nRefs #%d\n" % refs})
        runs = [{"name": "Grug - Elder", "status": "completed", "conclusion": elder,
                 "completed_at": "2030-01-01T23:59:00Z"}]
        runs += [{"name": "unittest-%d" % i, "status": "completed", "conclusion": c,
                  "completed_at": "2030-01-01T23:58:00Z"} for i, c in enumerate(others)]
        self.check_runs[head] = runs
        self.files[pr] = [{"filename": "tests/test_parser.py", "patch": TEST_PATCH},
                          {"filename": "bus/parser.py", "patch": "+x = 1\n"}]

    def set_review(self, head, conclusion, at, summary=""):
        """The review check's newest run on `head`, as a re-review leaves it."""
        for run in self.check_runs[head]:
            if run["name"] == "Grug - Elder":
                run.update(conclusion=conclusion, completed_at=at,
                           output={"title": "Elder review", "summary": summary})

    def names(self, number):
        return [label["name"] for label in self.issues[number]["labels"]]

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        data = json.loads(body) if body else None
        if method != "GET":
            self.writes.append((method, parts.path, data))
        if parts.path == "/repos/%s" % REPO:
            return reply(200, {"full_name": REPO, "default_branch": "main", "private": False})
        rest = parts.path[len("/repos/%s/" % REPO):]
        issue = re.fullmatch(r"issues/(\d+)(?:/(\w+))?", rest)
        found = (self._issue(method, int(issue.group(1)), issue.group(2), data) if issue
                 else self._read(rest, parse_qs(parts.query)) if method == "GET"
                 else self._create(data) if (method, rest) == ("POST", "issues") else None)
        if found is None:
            raise AssertionError("unexpected call %s %s" % (method, url))
        return found

    def _read(self, rest, query):
        runs = re.fullmatch(r"commits/([0-9a-f]{40})/check-runs", rest)
        files = re.fullmatch(r"pulls/(\d+)/files", rest)
        if rest == "labels":
            names = sorted({x["name"] for i in self.issues.values() for x in i["labels"]})
            return reply(200, [{"name": name} for name in names])
        if rest == "issues":
            wanted = set(query.get("labels", [""])[0].split(","))
            return reply(200, [i for i in self.issues.values() if i["state"] == "open"
                               and wanted <= set(x["name"] for x in i["labels"])])
        if rest == "pulls":
            return reply(200, self.open_pulls if query.get("state") == ["open"]
                         else self.closed_pulls)
        if runs:
            return reply(200, {"check_runs": self.check_runs.get(runs.group(1), [])})
        if files:
            return reply(200, self.files.get(int(files.group(1)), []))
        if rest.startswith("contents/"):
            return self._contents(unquote(rest[len("contents/"):]))
        return None

    def _contents(self, path):
        source = self.contents.get(path)
        if source is None:
            return reply(404, {"message": "Not Found"})
        return reply(200, {"encoding": "base64",
                           "content": base64.b64encode(source.encode()).decode()})

    def _create(self, data):
        number = 900 + len(self.created)
        self.created.append(dict(data, number=number))
        return reply(201, {"number": number})

    def _issue(self, method, n, sub, data):
        self.comments.setdefault(n, [])
        if sub is None:
            if method == "PATCH":
                self.issues[n].update(data)
            return reply(200, self.issues[n])
        if sub == "comments" and method == "POST":
            self.comments[n].append({"body": data["body"],
                                     "user": {"login": "example-login"}})
            return reply(201, self.comments[n][-1])
        if sub == "comments":
            return reply(200, self.comments[n])
        if sub == "labels" and method == "POST":
            have = self.names(n)
            self.issues[n]["labels"] += [{"name": x} for x in data["labels"] if x not in have]
            return reply(200, self.issues[n]["labels"])
        return None


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.join(tmp.name, "loom")
        self.fake = FakeGitHub()
        self.settings = station.Settings(self.home, [REPO], ["example-login"], ["example-owner"],
                                         settle_claimed="on")
        self.slept = []
        clock = [1000.0]

        def sleep(seconds):
            self.slept.append(seconds)
            clock[0] += seconds

        patches = [mock.patch.object(claimed, "_sleep", sleep),
                   mock.patch.object(claimed, "_clock", lambda: clock[0])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.said = []
        said = mock.patch.object(station, "_say", self.said.append)
        said.start()
        self.addCleanup(said.stop)

    def settle(self, now=None):
        st = station.Station(GitHub("token", transport=self.fake), self.settings, now=now)
        st.settle_claimed_tickets()
        return st


class SettleClaimedTest(Base):
    def test_merged_pull_request_with_code_boxes_closes_with_one_evidence_comment(self):
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7)
        self.settle()
        issue = self.fake.issues[7]
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue["state_reason"], "completed")
        self.assertNotIn("- [ ]", issue["body"])
        self.assertIn("- [x] The parser reads the new field.", issue["body"])
        self.assertEqual(len(self.fake.comments[7]), 1)
        comment = self.fake.comments[7][0]["body"]
        self.assertTrue(comment.startswith(claimed.MARKER))
        self.assertIn("PR #41", comment)
        self.assertIn(MERGE_SHA, comment)
        self.assertIn("Grug - Elder success", comment)
        self.assertIn("unittest-0 success", comment)
        self.assertIn("test_parser_reads_the_new_field", comment)
        self.assertNotIn(claimed.LIVE_LABEL, self.fake.names(7))

    def test_writes_are_at_least_three_seconds_apart(self):
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7)
        self.settle()
        writes = [w for w in self.fake.writes if w[0] in ("POST", "PATCH")]
        self.assertEqual(len(writes), 3)  # comment, tick, close
        self.assertEqual(len(self.slept), 2)
        self.assertTrue(all(s >= claimed.WRITE_GAP for s in self.slept))

    def test_a_live_box_labels_the_issue_once_and_leaves_it_open(self):
        self.fake.add_issue(7, body=LIVE_BODY)
        self.fake.add_merged(41, refs=7)
        self.settle()
        self.assertEqual(self.fake.issues[7]["state"], "open")
        self.assertIn(claimed.LIVE_LABEL, self.fake.names(7))
        self.assertEqual(self.fake.comments[7], [])
        self.assertIn("- [ ] Live:", self.fake.issues[7]["body"])
        self.fake.writes.clear()
        self.settle(now=10 ** 10)  # the next hourly run
        self.assertEqual([w for w in self.fake.writes if w[0] != "GET"], [])

    def test_issues_with_a_loom_label_or_a_hold_are_never_touched(self):
        self.fake.add_issue(7, labels=("claim:example-login", "loom:merged"))
        self.fake.add_issue(8, labels=("claim:example-login", "needs:example-login"))
        self.fake.add_issue(9, labels=("claim:example-login", "ready-for-human"))
        self.fake.add_issue(10, labels=("claim:example-login", "epic"))
        self.fake.add_issue(11, title="Epic: a big thing")
        self.fake.add_issue(12, sub_issues_summary={"total": 2, "completed": 1})
        self.fake.add_issue(13, labels=())
        self.fake.add_issue(14, labels=("claim:example-login", "needs-operator"))
        self.fake.add_issue(15, labels=("claim:example-login", "wontfix"))
        for number in range(7, 16):
            self.fake.add_merged(40 + number, refs=number, head="%040x" % number)
        self.settle()
        self.assertEqual(self.fake.writes, [])
        self.assertTrue(all(i["state"] == "open" for i in self.fake.issues.values()))

    def test_a_red_check_is_skipped(self):
        self.fake.add_issue(8)
        self.fake.add_merged(42, refs=8, others=("failure",), head="c3" * 20)
        self.fake.add_issue(9)
        self.fake.add_merged(43, refs=9, others=("success", "cancelled"), head="d4" * 20)
        self.fake.add_issue(10)
        self.fake.add_merged(44, refs=10, elder="neutral", others=("failure",), head="e5" * 20)
        self.settle()
        self.assertEqual(self.fake.writes, [])

    def test_a_neutral_check_other_than_the_review_check_does_not_block(self):
        # The review bot's other checks conclude neutral when they have
        # nothing to say (a skipped pass), on nearly every head; the merge
        # gate takes them, so the settle does too, and says so by name.
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7, others=("success", "neutral"))
        self.settle()
        self.assertEqual(self.fake.issues[7]["state"], "closed")
        comment = self.fake.comments[7][0]["body"]
        self.assertIn("unittest-1 neutral", comment)
        self.assertIn("Grug - Elder success", comment)

    def test_an_unmerged_or_missing_pull_request_is_skipped(self):
        self.fake.add_issue(7)  # its only pull request is still open
        self.fake.open_pulls.append({"number": 50, "state": "open",
                                     "body": "Refs #7\n", "head": {"ref": "x", "sha": "e" * 40}})
        self.fake.add_issue(8)  # merged, but a second one is still open
        self.fake.add_merged(42, refs=8)
        self.fake.open_pulls.append({"number": 51, "state": "open",
                                     "body": "Refs #8\n", "head": {"ref": "y", "sha": "f" * 40}})
        self.fake.add_issue(9)  # no pull request at all
        self.fake.add_issue(10)  # merged into another branch
        self.fake.add_merged(44, refs=10, base="release", head="c3" * 20)
        self.settle()
        self.assertEqual(self.fake.writes, [])

    def test_dry_run_is_the_default_and_writes_nothing(self):
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7)
        self.fake.add_issue(8, body=LIVE_BODY)
        self.fake.add_merged(42, refs=8, head="c3" * 20)
        self.settings.settle_claimed = station.Settings.from_env({}).settle_claimed
        self.assertEqual(self.settings.settle_claimed, "dry")
        self.settle()
        self.assertEqual(self.fake.writes, [])
        self.assertTrue(any("would close %s#7" % REPO in line for line in self.said))
        self.assertTrue(any("would label %s#8" % REPO in line for line in self.said))

    def test_at_most_the_per_run_cap_is_settled(self):
        for number in (7, 8, 9):
            self.fake.add_issue(number)
            self.fake.add_merged(40 + number, refs=number, head="%040x" % number)
        self.settings.settle_claimed_max = 2
        self.settle()
        closed = [n for n, i in self.fake.issues.items() if i["state"] == "closed"]
        self.assertEqual(closed, [7, 8])

    def test_the_run_is_hourly(self):
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7)
        self.settings.settle_claimed = "dry"
        st = self.settle(now=5000.0)
        reads = len(self.fake.writes)
        self.settle(now=5000.0 + 60)
        self.assertEqual(len(self.fake.writes), reads)
        self.assertTrue(os.path.exists(os.path.join(st.state, claimed.STATE_FILE)))

    def test_an_existing_evidence_comment_is_not_posted_twice(self):
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7)
        self.fake.comments[7].append({"body": claimed.MARKER + "\nSettled.",
                                      "user": {"login": "station"}})
        self.settle()
        self.assertEqual(self.fake.issues[7]["state"], "closed")
        self.assertEqual(len(self.fake.comments[7]), 1)

    def test_a_refused_issue_does_not_stop_the_next(self):
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7)
        self.fake.add_issue(8)
        self.fake.add_merged(42, refs=8, head="c3" * 20)
        real = self.fake.__call__

        def refuse_files_of_41(method, url, headers, body):
            if "/pulls/41/files" in url:
                return reply(403, {"message": "Resource not accessible"})
            return real(method, url, headers, body)

        st = station.Station(GitHub("token", transport=refuse_files_of_41), self.settings)
        st.settle_claimed_tickets()
        self.assertEqual(self.fake.issues[7]["state"], "open")
        self.assertEqual(self.fake.issues[8]["state"], "closed")
        self.assertTrue(any("could not settle %s#7" % REPO in line for line in self.said))

    def test_an_issue_with_no_acceptance_boxes_is_left_for_a_person(self):
        self.fake.add_issue(7, body="## Why\n\nBecause.\n")
        self.fake.add_merged(41, refs=7)
        self.settle()
        self.assertEqual(self.fake.writes, [])

    def test_off_reads_nothing(self):
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7)
        self.settings.settle_claimed = "off"
        calls = []
        with mock.patch.object(FakeGitHub, "__call__",
                               lambda s, *a: calls.append(a) or reply(500)):
            self.settle()
        self.assertEqual(calls, [])


class RereviewTest(Base):
    """A pull request that merged with the review check neutral gets one
    re-review request, and its issue settles on the result (loom#1221)."""

    def merged_neutral(self, issue=7, pr=41, head=HEAD_SHA, **extra):
        self.fake.add_issue(issue, **extra)
        self.fake.add_merged(pr, refs=issue, elder="neutral", head=head)

    def saved(self, st):
        with open(os.path.join(st.state, claimed.STATE_FILE)) as handle:
            return json.load(handle)

    def asks(self, pr=41):
        return [c for c in self.fake.comments.get(pr, [])
                if c["body"].startswith(claimed.REREVIEW_MARKER)]

    def test_a_neutral_review_gets_one_request_recorded_so_it_is_not_asked_twice(self):
        self.merged_neutral()
        st = self.settle(now=5000.0)
        self.assertEqual(len(self.asks()), 1)
        self.assertEqual(self.asks()[0]["body"], claimed.REREVIEW_MARKER + "\n/grug improve")
        self.assertEqual(self.fake.issues[7]["state"], "open")
        self.assertEqual(self.fake.comments[7], [])
        saved = self.saved(st)
        self.assertIn("%s#41" % REPO, saved["rereview"])
        self.settle(now=5000.0 + claimed.EVERY + 1)
        self.settle(now=5000.0 + 5 * claimed.EVERY)
        self.assertEqual(len(self.asks()), 1)

    def test_two_passes_a_minute_apart_ask_once(self):
        self.merged_neutral()
        self.settle(now=5000.0)
        writes = list(self.fake.writes)
        self.settle(now=5060.0)
        self.assertEqual(self.fake.writes, writes)
        self.assertEqual(len(writes), 1)

    def test_a_marker_on_the_pull_request_stops_a_second_ask_without_state(self):
        self.merged_neutral()
        st = self.settle(now=5000.0)
        os.remove(os.path.join(st.state, claimed.STATE_FILE))
        self.settle(now=9000.0)
        self.assertEqual(len(self.asks()), 1)

    def test_a_marker_from_an_untrusted_author_does_not_suppress_the_request(self):
        self.merged_neutral()
        self.fake.comments[41] = [{"body": claimed.REREVIEW_MARKER + "\n/grug improve",
                                   "user": {"login": "a-stranger"}}]
        self.settle(now=5000.0)
        ours = [c for c in self.asks() if c["user"]["login"] == "example-login"]
        self.assertEqual(len(ours), 1)

    def test_a_successful_re_review_lets_the_issue_settle(self):
        self.merged_neutral()
        self.settle(now=5000.0)
        self.fake.set_review(HEAD_SHA, "success", "2030-01-03T00:00:00Z")
        self.settle(now=5000.0 + claimed.EVERY + 1)
        self.assertEqual(self.fake.issues[7]["state"], "closed")
        self.assertEqual(len(self.asks()), 1)
        self.assertEqual(self.fake.created, [])

    def test_a_failed_re_review_files_its_findings_once_and_leaves_the_issue(self):
        self.merged_neutral()
        self.settle(now=5000.0)
        self.fake.set_review(HEAD_SHA, "failure", "2030-01-03T00:00:00Z",
                             "high: bus/parser.py reads past the end\nlow: a name is unclear")
        self.settle(now=5000.0 + claimed.EVERY + 1)
        self.assertEqual(len(self.fake.created), 1)
        made = self.fake.created[0]
        self.assertEqual(made["labels"], ["loom:triage"])
        self.assertIn("#41", made["title"])
        self.assertIn("reads past the end", made["body"])
        self.assertIn("Refs #7", made["body"])
        self.assertEqual(self.fake.issues[7]["state"], "open")
        self.assertIn("#900", self.fake.comments[7][0]["body"])
        self.settle(now=5000.0 + 3 * claimed.EVERY)
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(len(self.fake.comments[7]), 1)

    def test_a_second_neutral_result_asks_nothing_more_and_files_nothing(self):
        self.merged_neutral()
        self.settle(now=5000.0)
        self.fake.set_review(HEAD_SHA, "neutral", "2030-01-03T00:00:00Z")
        self.settle(now=5000.0 + claimed.EVERY + 1)
        self.assertEqual(len(self.asks()), 1)
        self.assertEqual(self.fake.created, [])
        self.assertEqual(self.fake.issues[7]["state"], "open")

    def test_a_failure_nobody_asked_about_is_left_alone(self):
        self.fake.add_issue(7)
        self.fake.add_merged(41, refs=7, elder="failure")
        self.settle()
        self.assertEqual(self.fake.writes, [])

    def test_a_red_check_or_open_pull_request_earns_no_request(self):
        self.merged_neutral(8, 42, head="c3" * 20)
        self.fake.check_runs["c3" * 20][1]["conclusion"] = "failure"
        self.merged_neutral(9, 43, head="d4" * 20)
        self.fake.open_pulls.append({"number": 52, "state": "open",
                                     "body": "Refs #9\n", "head": {"ref": "y", "sha": "f" * 40}})
        self.settle()
        self.assertEqual(self.fake.writes, [])

    def test_a_dry_run_writes_and_records_nothing(self):
        self.merged_neutral()
        self.settings.settle_claimed = "dry"
        st = self.settle(now=5000.0)
        self.assertEqual(self.fake.writes, [])
        self.assertTrue(any("would ask" in line for line in self.said))
        saved = self.saved(st)
        self.assertNotIn("%s#41" % REPO, saved.get("rereview", {}))

    def test_at_most_the_cap_of_requests_is_made_a_run(self):
        for n in range(4):
            self.merged_neutral(7 + n, 41 + n, head="%040x" % (n + 1))
        self.settle(now=5000.0)
        self.assertEqual(sum(len(self.asks(41 + n)) for n in range(4)), claimed.REREVIEW_PER_RUN)
        self.settle(now=5000.0 + claimed.EVERY + 1)
        self.assertEqual(sum(len(self.asks(41 + n)) for n in range(4)), 4)


class LiveBoxTest(unittest.TestCase):
    def test_live_deployed_window_and_observed_boxes(self):
        for text in ("Live: the endpoint answers 200.",
                     "The change is deployed to the cluster.",
                     "No error in production for a week.",
                     "The alert does not fire for 24 hours after the merge.",
                     "The nightly run posts the report.",
                     "The status line is seen in the logs.",
                     "Within 2 days the backlog count drops.",
                     "After the next deploy the page loads."):
            self.assertTrue(claimed.live_box(text), text)

    def test_code_and_test_boxes(self):
        for text in ("A test fails without the change and passes with it.",
                     "The parser reads the new field.",
                     "production/playbooks/site.yml sets the new variable.",
                     "The deploy.yml workflow pins every action by SHA.",
                     "The live_criterion helper returns the line.",
                     "docs/LANES.md names the setting."):
            self.assertFalse(claimed.live_box(text), text)


class SettingsTest(unittest.TestCase):
    def test_from_env(self):
        def mode(raw):
            return station.Settings.from_env({"LOOM_SETTLE_CLAIMED": raw}).settle_claimed
        self.assertEqual(station.Settings.from_env({}).settle_claimed, "dry")
        self.assertEqual(mode(""), "dry")
        self.assertEqual(mode("dry"), "dry")
        self.assertEqual(mode("on"), "on")
        self.assertEqual(mode("1"), "on")
        self.assertEqual(mode("off"), "off")
        self.assertEqual(station.Settings("h", [], [], []).settle_claimed, "off")
        cap = station.Settings.from_env({"LOOM_SETTLE_CLAIMED_MAX": "3"}).settle_claimed_max
        self.assertEqual(cap, 3)
        self.assertEqual(station.Settings.from_env({}).settle_claimed_max, claimed.PER_RUN)


if __name__ == "__main__":
    unittest.main()
