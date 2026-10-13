from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, journal, proof, text, tickets
from bus.github import GitHub

REPO = "example-owner/example-repo"
BODY = "## Why\nwhy\n## What\nwhat\n## Acceptance criteria\n- [ ] done\n## Size\nS\n## Out of scope\n- none"


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class Fake:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, json.loads(body) if body else None))
        return self.replies.pop(0)


def goal():
    return {"number": 1, "id": 101, "state": "open", "title": "Goal", "labels": [{"name": "loom:goal"}]}


class BrainTests(unittest.TestCase):
    def test_status_sections_and_request_ceiling(self):
        gh = GitHub("t", transport=Fake(
            reply(200, [{"number": 1, "title": "Goal", "labels": [{"name": "loom:goal"}], "created_at": "2030-01-01T00:00:00Z"},
                        {"number": 2, "title": "Ticket", "labels": [{"name": "loom:ticket"}, {"name": "loom:queued"}], "created_at": "2030-01-01T00:00:00Z"},
                        {"number": 3, "title": "Ask", "labels": [{"name": "needs-operator"}]}]),
            reply(200, [{"number": 4, "title": "PR", "head": {"ref": "loom/4-work"}}]),
            reply(200, {"data": {"repository": {"p4": {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}, "reviewThreads": {"nodes": [{"isResolved": False}]}}, "g1": {"comments": {"nodes": [{"body": "<!-- loom:journal v1 -->", "createdAt": "2030-01-01T01:00:00Z"}]}}}}}),
            reply(200, []), reply(200, [])))
        report = brain.status(gh, [REPO], REPO)
        for section in ("Goals", "Tickets", "Pull requests", "Needs operator", "Lane status", "SUCCESS", "1 unresolved threads"):
            self.assertIn(section, report)
        # Project repo: issues, pulls, one GraphQL request; lane repo: issues
        # (no loom:lanes issue here, so no comments read). The fake has no
        # replies left, so any extra call fails the test.
        self.assertEqual(gh.requests, 4)

    def test_status_reads_lane_comments_with_one_more_request(self):
        lanes_issue = {"number": 9, "title": "Loom lane status", "labels": [{"name": "loom:lanes"}]}
        gh = GitHub("t", transport=Fake(
            reply(200, []), reply(200, []),
            reply(200, [lanes_issue]),
            reply(200, [{"body": "<!-- loom:lanes v1 -->\n| codex | yes |", "created_at": "2030-01-01T00:00:00Z"}])))
        report = brain.status(gh, [REPO], REPO)
        self.assertIn("| codex | yes |", report)
        self.assertEqual(gh.requests, 4)

    def test_brief_refusals_create_nothing(self):
        cases = [
            ("## Why\nmissing", "codex", 0),
            (BODY, "bogus", 0),
            (BODY, "codex", 404),
        ]
        for text, lane, goal_status in cases:
            with self.subTest(lane=lane, goal_status=goal_status):
                goal_reply = reply(goal_status, {"state": "closed", "labels": []}) if goal_status else reply(200, goal())
                fake = Fake(goal_reply)
                gh = GitHub("t", transport=fake)
                with self.assertRaises(ValueError):
                    brain.brief(gh, REPO, 1, "Ticket", text, lane)
                self.assertFalse(any(call[0] == "POST" for call in fake.calls))

    def test_brief_refuses_header_values_the_runner_would_refuse(self):
        bad = [dict(model="x\nRepo: evil/repo"), dict(model="bad model"), dict(base="-x"),
               dict(base="main\nBranch: loom/evil")]
        for kw in bad:
            with self.subTest(**{k: repr(v) for k, v in kw.items()}):
                fake = Fake(reply(200, goal()))
                with self.assertRaises(ValueError):
                    brain.brief(GitHub("t", transport=fake), REPO, 1, "Ticket", BODY, "codex", **kw)
                self.assertEqual(fake.calls, [])

    @staticmethod
    def _children(*states):
        return [{"state": "open", "labels": [{"name": "loom:ticket"}, {"name": "loom:" + s}]}
                for s in states]

    def test_brief_capacity_refusal(self):
        children = self._children(*(["briefed", "running", "pr", "failed"] * 4))
        fake = Fake(reply(200, goal()), reply(200, children))
        gh = GitHub("t", transport=fake)
        with self.assertRaises(ValueError):
            brain.brief(gh, REPO, 1, "Ticket", BODY, "codex")
        self.assertFalse(any(c[0] == "POST" for c in fake.calls))

    def test_merged_and_shipped_tickets_hold_no_slot(self):
        children = self._children(*(["merged", "shipped"] * 6 + ["running"] * 15))
        fake = Fake(reply(200, goal()), reply(200, children),
                    reply(201, {"id": 202, "html_url": "https://example.com/ticket"}), reply(201, {}))
        url = brain.brief(GitHub("t", transport=fake), REPO, 1, "Ticket", BODY, "codex")
        self.assertEqual(url, "https://example.com/ticket")

    def test_the_cap_comes_from_the_environment(self):
        self.assertEqual(16, brain.goal_cap({}))
        self.assertEqual(3, brain.goal_cap({"LOOM_GOAL_CAP": "3"}))
        for bad in ("0", "-2", "x", ""):
            self.assertEqual(16, brain.goal_cap({"LOOM_GOAL_CAP": bad}))

    def test_good_brief_creates_ticket_and_subissue(self):
        fake = Fake(reply(200, goal()), reply(200, []), reply(201, {"id": 202, "html_url": "https://example.com/ticket"}), reply(201, {}))
        gh = GitHub("t", transport=fake)
        url = brain.brief(gh, REPO, 1, "Ticket", BODY, "codex", "hard", "model-x", "main")
        self.assertEqual(url, "https://example.com/ticket")
        payload = fake.calls[2][2]
        self.assertEqual(payload["labels"], ["loom:ticket", "loom:briefed", "lane:codex"])
        self.assertTrue(payload["body"].startswith("Difficulty: hard\nModel: model-x\nBase: main\n\n"))
        self.assertEqual(fake.calls[3][1].split("api.github.com")[1], "/repos/%s/issues/1/sub_issues" % REPO)
        self.assertEqual(fake.calls[3][2], {"sub_issue_id": 202})

    def test_move_keeps_labels_and_swaps_lane(self):
        fake = Fake(reply(200, {"state": "open", "labels": [{"name": "loom:ticket"}, {"name": "loom:queued"}, {"name": "lane:agy"}, {"name": "custom"}]}), reply(200, []))
        gh = GitHub("t", transport=fake)
        brain.move(gh, REPO, 8, "briefed", "codex")
        self.assertEqual(fake.calls[1][2], {"labels": ["loom:ticket", "custom", "loom:briefed", "lane:codex"]})

    def test_move_refusals(self):
        for labels in ([{"name": "loom:ticket"}, {"name": "loom:queued"}],):
            fake = Fake(reply(200, {"state": "open", "labels": labels}))
            with self.assertRaises(ValueError):
                brain.move(GitHub("t", transport=fake), REPO, 8, "briefed")
        fake = Fake(reply(200, {"state": "closed", "labels": []}))
        with self.assertRaises(ValueError):
            brain.move(GitHub("t", transport=fake), REPO, 8, "queued")
        fake = Fake(reply(200, {"state": "open", "labels": []}))
        with self.assertRaises(ValueError):
            brain.move(GitHub("t", transport=fake), REPO, 8, "queued", "codex")

    def _merge_replies(self, changes=None):
        pr = {"state": "open", "draft": False, "body": "Refs %s#9" % REPO, "head": {"ref": "loom/9-work", "sha": "checked", "repo": {"full_name": REPO}}, "base": {"repo": {"full_name": REPO}}, "mergeable": True}
        details = {"isDraft": False, "mergeable": "MERGEABLE", "headRefName": "loom/9-work", "headRefOid": "checked", "headRepository": {"nameWithOwner": REPO}, "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}, "reviewThreads": {"nodes": []}}
        if changes:
            pr.update(changes.get("pr", {}))
            details.update(changes.get("details", {}))
        return reply(200, pr), reply(200, {"data": {"repository": {"pullRequest": details}}})

    def test_merge_refusals_do_not_call_merge(self):
        cases = [
            {"pr": {"state": "closed"}}, {"pr": {"draft": True}},
            {"pr": {"head": {"ref": "feature/x", "sha": "x", "repo": {"full_name": REPO}}}},
            {"details": {"headRepository": {"nameWithOwner": "other/repo"}}},
            {"pr": {"mergeable": False}},
            {"details": {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]}}},
            {"details": {"reviewThreads": {"nodes": [{"isResolved": False}]}}},
        ]
        for case in cases:
            with self.subTest(case=case):
                fake = Fake(*self._merge_replies(case))
                with self.assertRaises(ValueError):
                    brain.merge(GitHub("t", transport=fake), REPO, 8)
                self.assertFalse(any("/merge" in call[1] for call in fake.calls))

    def test_good_merge_uses_checked_sha_then_deletes_and_moves_ticket(self):
        pr, query = self._merge_replies()
        issue = {"state": "open", "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}
        fake = Fake(pr, query, reply(200, issue), reply(200, {"merged": True}), reply(204),
                    reply(200, issue), reply(200, {}))
        result = brain.merge(GitHub("t", transport=fake), REPO, 8, (REPO, 9))
        self.assertEqual(fake.calls[3][2], {"merge_method": "squash", "sha": "checked"})
        self.assertIn("/git/refs/heads/loom/9-work", fake.calls[4][1])
        self.assertEqual(result, {"merged": True, "pull_request": (REPO, 8),
                                  "ticket": (REPO, 9), "head_sha": "checked"})
        self.assertTrue(fake.calls[-2][1].endswith("/repos/%s/issues/9" % REPO))

    def test_merge_refuses_an_unrelated_closed_or_non_ticket_issue_before_write(self):
        cases = [
            ((REPO, 999), {"state": "open", "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}),
            ((REPO, 9), {"state": "closed", "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}),
            ((REPO, 9), {"state": "open", "labels": [{"name": "loom:pr"}]}),
        ]
        for supplied, issue in cases:
            pr, query = self._merge_replies()
            fake = Fake(pr, query, reply(200, issue))
            with self.assertRaises(ValueError):
                brain.merge(GitHub("t", transport=fake), REPO, 8, supplied)
            self.assertFalse(any("/merge" in call[1] for call in fake.calls))
            self.assertFalse(any(call[0] in ("PUT", "DELETE") for call in fake.calls))

    def test_a_branch_already_deleted_still_counts_as_merged(self):
        pr, query = self._merge_replies()
        issue = {"state": "open", "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}
        fake = Fake(pr, query, reply(200, issue), reply(200, {"merged": True}),
                    reply(422, {"message": "Reference does not exist"}), reply(200, issue), reply(200, {}))
        brain.merge(GitHub("t", transport=fake), REPO, 8, (REPO, 9))
        self.assertEqual(fake.calls[-1][0], "PUT")
        # Merged is not deployed: the ticket says merged until the project's
        # ship owner deploys it.
        self.assertIn("loom:merged", fake.calls[-1][2]["labels"])
        self.assertNotIn("loom:shipped", fake.calls[-1][2]["labels"])

    def test_merge_refuses_when_the_head_repo_is_unknown(self):
        fake = Fake(*self._merge_replies({"pr": {"head": {"ref": "loom/8-work", "sha": "checked"}},
                                          "details": {"headRepository": None}}))
        with self.assertRaises(ValueError):
            brain.merge(GitHub("t", transport=fake), REPO, 8)
        self.assertFalse(any("/merge" in call[1] for call in fake.calls))

    def test_merge_does_not_delete_when_github_does_not_confirm(self):
        pr, query = self._merge_replies()
        fake = Fake(pr, query, reply(200, {"merged": False}))
        with self.assertRaises(ValueError):
            brain.merge(GitHub("t", transport=fake), REPO, 8)
        self.assertFalse(any("/git/refs/" in call[1] for call in fake.calls))

    def test_reply_and_resolve_right_thread(self):
        details = {"reviewThreads": {"nodes": [{"id": "THREAD", "comments": {"nodes": [{"databaseId": 42}]}}]}}
        fake = Fake(reply(200, {}), reply(200, {"data": {"repository": {"pullRequest": details}}}), reply(200, {"data": {}}))
        brain.reply_to_review(GitHub("t", transport=fake), REPO, 8, 42, "Thanks", True)
        self.assertTrue(fake.calls[0][1].endswith("/comments/42/replies"))
        self.assertIn("THREAD", fake.calls[2][2]["query"])
        # The thread id must be asked for, or a real reply can never resolve.
        self.assertIn("nodes { id ", fake.calls[1][2]["query"])

    def test_reply_without_resolve_only_posts(self):
        fake = Fake(reply(200, {}))
        brain.reply_to_review(GitHub("t", transport=fake), REPO, 8, 42, "Thanks")
        self.assertEqual(len(fake.calls), 1)

    def test_shared_body_checks_refuse_in_reply_brief_and_move(self):
        # Built at run time so this file never holds the handle itself.
        handle = "@" + text.REVIEW_BOT_SHORT_NAME.upper()
        bodies = [handle + " please look", "This does not fix #42",
                  "closes other/repo#3", "Resolved: https://github.com/a/b/issues/9"]
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    brain.reply_to_review(GitHub("t", transport=Fake()), REPO, 8, 42, body)
                with self.assertRaises(ValueError):
                    brain.brief(GitHub("t", transport=Fake()), REPO, 1, body, BODY)
                with self.assertRaises(ValueError):
                    brain.move(GitHub("t", transport=Fake()), REPO, 8, "queued", note=body)


def fix_pr(**changes):
    pr = {"state": "open", "body": "## Why\nx\n\nRefs %s#9\n" % REPO,
          "head": {"ref": "loom/9-work", "sha": "abc123", "repo": {"full_name": REPO}},
          "base": {"repo": {"full_name": REPO}}}
    pr.update(changes)
    return pr


def fix_ticket(*states):
    names = ["loom:ticket", "lane:codex", "custom"] + ["loom:" + s for s in states or ("pr",)]
    return {"state": "open", "labels": [{"name": n} for n in names]}


EVIDENCE = "The owed clock stopped again after the ship.\nMeasured: 0 of 12 clocks restored in the last hour.\n"


class GoalFake:
    """A goal with sub-issues: enough of the issues API for brief and regress."""

    def __init__(self, ordinary, cap_env="16"):
        self.issues = {1: dict(goal(), html_url="https://example.com/1")}
        self.children = []
        self.calls = []
        for n in range(ordinary):
            self.add(100 + n, "running")
        self.add(50, "shipped")
        self.add(51, "shipped")

    def add(self, number, state, extra=()):
        self.issues[number] = {
            "number": number, "id": 1000 + number, "state": "open",
            "html_url": "https://example.com/%d" % number,
            "labels": [{"name": n} for n in ("loom:ticket", "loom:" + state) + tuple(extra)]}
        self.children.append(number)

    def names(self, number):
        return [x["name"] for x in self.issues[number]["labels"]]

    def __call__(self, method, url, headers, body):
        data = json.loads(body) if body else None
        path = url.split("api.github.com")[1].split("?")[0]
        self.calls.append((method, path, data))
        rest = path[len("/repos/%s/" % REPO):]
        if method == "GET" and rest == "issues/1/sub_issues":
            return reply(200, [self.issues[n] for n in self.children])
        if method == "POST" and rest == "issues":
            number = max(self.issues) + 1
            self.issues[number] = {"number": number, "id": 1000 + number, "state": "open",
                                   "html_url": "https://example.com/%d" % number,
                                   "labels": [{"name": n} for n in data["labels"]]}
            return reply(201, self.issues[number])
        if method == "POST" and rest == "issues/1/sub_issues":
            self.children.append(data["sub_issue_id"] - 1000)
            return reply(201, {})
        number = int(rest.split("/")[1])
        if method == "GET":
            return reply(200, self.issues[number])
        if method == "PUT":
            self.issues[number]["labels"] = [{"name": n} for n in data["labels"]]
            return reply(200, [])
        if method == "PATCH":
            self.issues[number].update(data)
            return reply(200, {})
        return reply(201, {})

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class RegressionTests(unittest.TestCase):
    def setUp(self):
        old = os.environ.pop("LOOM_GOAL_CAP", None)
        self.addCleanup(lambda: old is None or os.environ.__setitem__("LOOM_GOAL_CAP", old))

    def test_a_full_goal_admits_one_regression_and_no_ordinary_brief(self):
        fake = GoalFake(16)
        gh = GitHub("t", transport=fake)
        out = brain.regress(gh, REPO, 1, 50, "codex", EVIDENCE)
        self.assertIn("17 in flight, cap 16", out)
        self.assertEqual(fake.names(50), ["loom:ticket", "loom:briefed", "lane:codex", "loom:regression"])
        comment = [c[2]["body"] for c in fake.writes() if c[0] == "POST"][0]
        self.assertTrue(comment.startswith(tickets.REGRESSION_MARKER + "\nGoal: %s#1\n" % REPO))
        self.assertIn("regression slot: 16 tickets in flight, cap 16", comment)
        self.assertIn("Measured: 0 of 12", comment)
        # No new issue: the shipped ticket itself went back to a lane.
        self.assertFalse(any(c[1].endswith("/issues") for c in fake.writes()))
        before = len(fake.writes())
        with self.assertRaises(ValueError):
            brain.brief(gh, REPO, 1, "Ticket", BODY, "codex")
        self.assertEqual(len(fake.writes()), before)

    def test_the_slot_holds_one_regression_and_admitting_it_again_changes_nothing(self):
        fake = GoalFake(16)
        gh = GitHub("t", transport=fake)
        brain.regress(gh, REPO, 1, 50, "codex", EVIDENCE)
        before = len(fake.writes())
        self.assertIn("already admitted", brain.regress(gh, REPO, 1, 50, "agy", EVIDENCE))
        with self.assertRaises(ValueError):
            brain.regress(gh, REPO, 1, 51, "codex", EVIDENCE)
        self.assertEqual(len(fake.writes()), before)
        self.assertIn("loom:shipped", fake.names(51))
        # Still taken while the regression is anywhere in flight, even below the cap.
        fake.issues[100]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:shipped"}]
        fake.issues[50]["labels"].append({"name": "loom:pr"})
        with self.assertRaises(ValueError):
            brain.regress(gh, REPO, 1, 51, "codex", EVIDENCE)

    def test_the_slot_frees_once_the_regression_ships_and_capacity_recovers(self):
        fake = GoalFake(16)
        gh = GitHub("t", transport=fake)
        brain.regress(gh, REPO, 1, 50, "codex", EVIDENCE)
        brain.move(gh, REPO, 50, "shipped")
        with self.assertRaises(ValueError):
            brain.brief(gh, REPO, 1, "Ticket", BODY, "codex")
        brain.regress(gh, REPO, 1, 51, "codex", EVIDENCE)
        brain.move(gh, REPO, 101, "merged")
        brain.move(gh, REPO, 102, "merged")
        self.assertIn("example.com", brain.brief(gh, REPO, 1, "Ticket", BODY, "codex"))

    def test_below_the_cap_a_regression_is_admitted_within_capacity(self):
        fake = GoalFake(3)
        brain.regress(GitHub("t", transport=fake), REPO, 1, 50, "codex", EVIDENCE)
        comment = [c[2]["body"] for c in fake.writes() if c[0] == "POST"][0]
        self.assertIn("Admitted within capacity: 3 tickets in flight, cap 16.", comment)

    def test_a_verified_and_closed_ticket_is_reopened(self):
        fake = GoalFake(16)
        fake.issues[50].update(state="closed", state_reason="completed",
                               labels=[{"name": "loom:ticket"}, {"name": "loom:verified"}])
        brain.regress(GitHub("t", transport=fake), REPO, 1, 50, "codex", EVIDENCE)
        self.assertEqual(fake.issues[50]["state"], "open")
        self.assertIn("loom:briefed", fake.names(50))

    def test_regress_refusals_write_nothing(self):
        cases = [
            (50, "codex", "No measurement.\n"),
            (50, "bogus", EVIDENCE),
            (100, "codex", EVIDENCE),
            (999, "codex", EVIDENCE),
        ]
        for number, lane, evidence in cases:
            with self.subTest(number=number, lane=lane):
                fake = GoalFake(4)
                fake.issues[999] = dict(fake.issues[50], number=999)
                with self.assertRaises(ValueError):
                    brain.regress(GitHub("t", transport=fake), REPO, 1, number, lane, evidence)
                self.assertEqual(fake.writes(), [])
        fake = GoalFake(4)
        fake.issues[50].update(state="closed", state_reason="not_planned")
        with self.assertRaises(ValueError):
            brain.regress(GitHub("t", transport=fake), REPO, 1, 50, "codex", EVIDENCE)
        self.assertEqual(fake.writes(), [])


class FixTests(unittest.TestCase):
    def refused(self, pr, ticket=None, ref=(REPO, 9)):
        fake = Fake(reply(200, pr), reply(200, ticket or fix_ticket()))
        with self.assertRaises(ValueError):
            brain.fix(GitHub("t", transport=fake), REPO, 8, ref, "notes")
        self.assertFalse(any(c[0] in ("POST", "PUT") for c in fake.calls))

    def test_fix_refuses_a_closed_pull_request(self):
        self.refused(fix_pr(state="closed"))

    def test_fix_refuses_a_foreign_or_non_loom_branch(self):
        self.refused(fix_pr(head={"ref": "loom/9-work", "sha": "x", "repo": {"full_name": "fork/repo"}}))
        self.refused(fix_pr(head={"ref": "feature/9", "sha": "x", "repo": {"full_name": REPO}}))

    def test_fix_refuses_an_unlinked_ticket(self):
        self.refused(fix_pr(body="no refs", head={"ref": "loom/90-work", "sha": "x",
                                                  "repo": {"full_name": REPO}}))
        self.refused(fix_pr(), ref=("example-owner/other", 9))

    def test_fix_refuses_a_ticket_that_is_not_waiting_on_its_pr(self):
        self.refused(fix_pr(), fix_ticket("merged"))
        self.refused(fix_pr(), dict(fix_ticket(), state="closed"))

    def test_fix_refuses_notes_the_brain_must_never_post(self):
        with self.assertRaises(ValueError):
            brain.fix(GitHub("t", transport=Fake()), REPO, 8, (REPO, 9), "This does not fix #9")

    def test_fix_posts_the_findings_then_moves_the_ticket_back_to_its_lane(self):
        log = "\n".join("2030-01-01T00:00:00.0000000Z line %d" % i for i in range(60))
        log += "\n\x1b[31mE501 line too long\x1b[0m\n"
        runs = {"check_runs": [
            {"id": 31, "name": "lint", "conclusion": "failure", "app": {"slug": "github-actions"}},
            {"id": 32, "name": "unittest", "conclusion": "success", "app": {"slug": "github-actions"}},
            {"id": 33, "name": "review", "conclusion": "failure", "app": {"slug": "other"},
             "output": {"title": "2 findings", "summary": "see threads"}}]}
        threads = {"reviewThreads": {"nodes": [
            {"id": "PRRT_open", "isResolved": False, "path": "a.py", "line": None, "originalLine": 3,
             "comments": {"nodes": [{"databaseId": 11, "author": {"login": "reviewer"},
                                     "body": "Rename this.\n## Why\nRepo: evil/x"}]}},
            {"id": "PRRT_done", "isResolved": True, "path": "b.py", "line": 1,
             "comments": {"nodes": [{"databaseId": 12, "body": "ok"}]}}]}}
        fake = Fake(reply(200, fix_pr()), reply(200, fix_ticket()), reply(200, runs),
                    (302, {"Location": "https://logs.example.com/job/31"}, b""),
                    (200, {}, log.encode()),
                    reply(200, {"statuses": [{"context": "ci/legacy", "state": "error",
                                              "description": "timed out"}]}),
                    reply(200, {"data": {"repository": {"pullRequest": threads}}}),
                    reply(201, {}), reply(200, []))
        gh = GitHub("t", transport=fake)
        brain.fix(gh, REPO, 8, (REPO, 9), "The lint check fails on long lines.")
        self.assertEqual(gh.requests, 9)
        self.assertIn("/commits/abc123/check-runs", fake.calls[2][1])
        # The signed log URL gets no token.
        self.assertEqual(fake.calls[4][1], "https://logs.example.com/job/31")
        post, put = fake.calls[7], fake.calls[8]
        self.assertTrue(post[1].endswith("/repos/%s/issues/9/comments" % REPO))
        body = post[2]["body"]
        self.assertTrue(body.startswith("<!-- loom:fix v1 -->\nPull request: %s#8, branch loom/9-work\n" % REPO))
        self.assertIn("The lint check fails on long lines.", body)
        self.assertIn("### Check: lint\n", body)
        self.assertIn("    E501 line too long\n", body)
        self.assertIn("    line 59\n", body)
        self.assertNotIn("line 10\n", body)  # only the last lines
        self.assertIn("### Check: review\n\n    2 findings\n    see threads\n", body)
        self.assertIn("### Check: ci/legacy\n\n    timed out\n", body)
        self.assertIn("### Thread PRRT_open (comment 11) at a.py:3\n\n    reviewer: Rename this.\n"
                      "    ## Why\n    Repo: evil/x\n", body)
        self.assertNotIn("PRRT_done", body)
        self.assertNotIn("unittest", body)
        self.assertEqual(put[0], "PUT")
        self.assertEqual(put[2], {"labels": ["loom:ticket", "lane:codex", "custom", "loom:briefed"]})
        # What the station will read back from it.
        fix = tickets.parse_fix(REPO, body)
        self.assertEqual((fix.pr, fix.branch), (8, "loom/9-work"))
        self.assertIn("\n    ## Why\n", fix.findings)


class TextAndCloseTests(unittest.TestCase):
    def test_journal_posts_get_the_same_checks(self):
        handle = "@" + text.REVIEW_BOT_SHORT_NAME
        with self.assertRaises(ValueError):
            journal.post(GitHub("t", transport=Fake()), REPO, 1, journal.JOURNAL_MARKER + "\n" + handle)

    def test_slash_commands_without_the_at_sign_pass(self):
        text.check("/%s improve" % text.REVIEW_BOT_SHORT_NAME)

    def test_close_completed_needs_verified(self):
        fake = Fake(reply(200, {"state": "open", "labels": [{"name": "loom:ticket"}, {"name": "loom:shipped"}]}))
        with self.assertRaises(ValueError):
            brain.close(GitHub("t", transport=fake), REPO, 9)
        self.assertFalse(any(c[0] == "PATCH" for c in fake.calls))

    def test_close_refuses_issues_that_are_not_tickets(self):
        fake = Fake(reply(200, {"state": "open", "labels": [{"name": "loom:goal"}]}))
        with self.assertRaises(ValueError):
            brain.close(GitHub("t", transport=fake), REPO, 1, "not_planned")
        self.assertFalse(any(c[0] == "PATCH" for c in fake.calls))

    def test_close_not_planned_needs_no_proof(self):
        fake = Fake(reply(200, ticket("failed", UNCHECKED)), reply(200, {}))
        brain.close(GitHub("t", transport=fake), REPO, 9, "not_planned", env=ENV)
        self.assertEqual(fake.calls[-1][2], {"state": "closed", "state_reason": "not_planned"})


TICKET_BODY = ("## Why\nwhy\n## What\nwhat\n## Acceptance criteria\n"
               "- [x] The probe logs one line per request.\n- [x] p95 latency drops below 200 ms.\n"
               "## Size\nS\n## Out of scope\n- [ ] not a criterion\n")
UNCHECKED = TICKET_BODY.replace("- [x] p95", "- [ ] p95")
ENV = {"LOOM_BRIEF_AUTHORS": "brain-bot, other"}


def ticket(state, body=TICKET_BODY):
    return {"state": "open", "body": body,
            "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state}, {"name": "lane:codex"}]}


def record(kind="runtime", **changes):
    out = {"kind": kind, "revision": "abc1234",
           "observed": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"},
           "sources": ["https://example.com/logs/1"],
           "criteria": [{"criterion": "The probe logs one line per request.", "result": "pass",
                         "evidence": "log line seen"},
                        {"criterion": "p95 latency drops below 200 ms.", "result": "pass",
                         "evidence": "dashboard"}]}
    if kind == "runtime":
        out.update(environment="production", artifact="image@sha256:0000",
                   measure={"name": "p95 ms", "baseline": "340", "target": "200", "after": "180"})
    out.update(changes)
    return {k: v for k, v in out.items() if v is not None}


def on_main(status="behind"):
    return reply(200, {"default_branch": "main"}), reply(200, {"status": status})


class ProofTests(unittest.TestCase):
    def test_queued_to_verified_without_proof_is_refused(self):
        fake = Fake(reply(200, ticket("queued")), reply(200, []))
        with self.assertRaises(ValueError):
            brain.move(GitHub("t", transport=fake), REPO, 9, "verified")
        self.assertFalse(any(c[0] in ("PUT", "POST") for c in fake.calls))

    def test_queued_to_verified_with_a_good_proof_is_still_refused(self):
        fake = Fake(reply(200, ticket("queued")), *on_main())
        with self.assertRaisesRegex(ValueError, "only from shipped"):
            brain.move(GitHub("t", transport=fake), REPO, 9, "verified", record=record())
        self.assertFalse(any(c[0] in ("PUT", "POST") for c in fake.calls))

    def test_only_verified_takes_a_proof(self):
        with self.assertRaises(ValueError):
            brain.move(GitHub("t", transport=Fake()), REPO, 9, "shipped", record=record())

    def test_incomplete_proofs_are_refused_before_anything_is_written(self):
        missing = record()
        del missing["measure"]
        failed = record()
        failed["criteria"][1]["result"] = "fail"
        cases = [
            (missing, TICKET_BODY, "missing measure"),
            (record(artifact=" "), TICKET_BODY, "artifact"),
            (record(revision="main"), TICKET_BODY, "commit SHA"),
            (record(sources=[]), TICKET_BODY, "sources"),
            (record(observed={"start": "2030-01-02T00:00:00Z", "end": "2030-01-01T00:00:00Z"}),
             TICKET_BODY, "ends before"),
            (record(criteria=record()["criteria"][:1]), TICKET_BODY, "acceptance criteria"),
            (failed, TICKET_BODY, "did not pass"),
            (record(), UNCHECKED, "unchecked acceptance criterion: p95"),
            (record(kind="other"), TICKET_BODY, "runtime or docs"),
        ]
        for proof_record, body, why in cases:
            with self.subTest(why=why):
                fake = Fake(reply(200, ticket("shipped", body)))
                with self.assertRaisesRegex(ValueError, why):
                    brain.move(GitHub("t", transport=fake), REPO, 9, "verified", record=proof_record)
                self.assertEqual(len(fake.calls), 1)

    def test_a_revision_not_on_the_default_branch_is_refused(self):
        for answer in (on_main("diverged"), (reply(200, {"default_branch": "main"}), reply(404, {}))):
            fake = Fake(reply(200, ticket("shipped")), *answer)
            with self.assertRaisesRegex(ValueError, "not on main"):
                brain.move(GitHub("t", transport=fake), REPO, 9, "verified", record=record())
            self.assertFalse(any(c[0] in ("PUT", "POST") for c in fake.calls))

    def test_runtime_proof_waits_for_shipped_but_docs_proof_takes_merged(self):
        fake = Fake(reply(200, ticket("merged")))
        with self.assertRaisesRegex(ValueError, "runtime ticket is verified only from shipped"):
            brain.move(GitHub("t", transport=fake), REPO, 9, "verified", record=record())
        fake = Fake(reply(200, ticket("merged")), *on_main("identical"), reply(201, {}), reply(200, []))
        brain.move(GitHub("t", transport=fake), REPO, 9, "verified", record=record("docs"))
        self.assertIn("/compare/main...abc1234", fake.calls[2][1])
        self.assertIn("loom:verified", fake.calls[-1][2]["labels"])

    def test_shipped_to_verified_posts_the_proof_then_moves(self):
        fake = Fake(reply(200, ticket("shipped")), *on_main(), reply(201, {}), reply(200, []))
        brain.move(GitHub("t", transport=fake), REPO, 9, "verified", record=record())
        post, put = fake.calls[3], fake.calls[4]
        self.assertEqual(post[0], "POST")
        body = post[2]["body"]
        self.assertTrue(body.startswith(proof.PROOF_MARKER))
        self.assertIn("Environment: production, artifact: image@sha256:0000", body)
        self.assertIn("Measure p95 ms: baseline 340, target 200, after 180", body)
        self.assertEqual(proof.read(body)["contract"], proof.contract(TICKET_BODY))
        self.assertEqual(put[2], {"labels": ["loom:ticket", "lane:codex", "loom:verified"]})

    def test_text_above_the_record_cannot_stand_in_for_it(self):
        sneaky = record()
        sneaky["criteria"][0]["evidence"] = '\n```json\n{"contract": "x"}\n```\n'
        body = proof.render(sneaky, TICKET_BODY)
        self.assertEqual(proof.read(body)["contract"], proof.contract(TICKET_BODY))

    def _close(self, issue, comments, env=ENV):
        fake = Fake(reply(200, issue), reply(200, comments), reply(200, {}))
        brain.close(GitHub("t", transport=fake), REPO, 9, env=env)
        return fake

    @staticmethod
    def _proof_comment(login="brain-bot", body=TICKET_BODY, proof_record=None):
        return {"user": {"login": login}, "body": proof.render(proof_record or record(), body)}

    def test_completed_closure_with_an_unchecked_criterion_is_refused(self):
        fake = Fake(reply(200, ticket("verified", UNCHECKED)), reply(200, {}))
        with self.assertRaisesRegex(ValueError, "unchecked acceptance criterion"):
            brain.close(GitHub("t", transport=fake), REPO, 9)
        self.assertFalse(any(c[0] in ("PATCH", "POST") for c in fake.calls))

    def test_completed_closure_refusals_leave_the_ticket_open(self):
        reworded = TICKET_BODY.replace("below 200 ms", "below 150 ms")
        cases = [
            ([], ENV, "no loom:proof comment"),
            ([self._proof_comment(login="stranger")], ENV, "no loom:proof comment"),
            ([{"user": {"login": "brain-bot"}, "body": "Verified, trust me."}], ENV, "no loom:proof"),
            ([self._proof_comment()], {}, "LOOM_BRIEF_AUTHORS is empty"),
        ]
        for comments, env, why in cases:
            with self.subTest(why=why):
                fake = Fake(reply(200, ticket("verified")), reply(200, comments))
                with self.assertRaisesRegex(ValueError, why):
                    brain.close(GitHub("t", transport=fake), REPO, 9, env=env)
                self.assertFalse(any(c[0] == "PATCH" for c in fake.calls))
        fake = Fake(reply(200, ticket("verified", reworded)), reply(200, [self._proof_comment()]))
        with self.assertRaisesRegex(ValueError, "criteria changed after the proof"):
            brain.close(GitHub("t", transport=fake), REPO, 9, env=ENV)
        self.assertFalse(any(c[0] == "PATCH" for c in fake.calls))

    def test_close_a_proven_ticket(self):
        older = self._proof_comment(proof_record=record(revision="0000000"))
        fake = self._close(ticket("verified"), [older, self._proof_comment(login="other")])
        self.assertEqual(fake.calls[-1][2], {"state": "closed", "state_reason": "completed"})

    def test_criteria_are_read_only_from_their_section(self):
        self.assertEqual([t for _, t in proof.criteria(TICKET_BODY)],
                         ["The probe logs one line per request.", "p95 latency drops below 200 ms."])
        self.assertEqual(proof.contract(TICKET_BODY), proof.contract(UNCHECKED))


def item(number, body=BODY, labels=(), **extra):
    out = {"number": number, "id": 1000 + number, "state": "open", "body": body,
           "labels": [{"name": name} for name in labels]}
    out.update(extra)
    return out


class Routes:
    """Replies by method and path; records every call. The issue list is
    served in pages of `page` items with Link headers."""

    def __init__(self, issues, goal_children=(), page=100, refuse_sub_issue=()):
        self.issues = list(issues)
        self.goal_children = list(goal_children)
        self.page = page
        self.refuse_sub_issue = set(refuse_sub_issue)
        self.calls = []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body) if body else None
        path = url.split("api.github.com")[1]
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/repos/%s/issues/1" % REPO:
            return reply(200, goal())
        if method == "GET" and "/issues/1/sub_issues" in path:
            return reply(200, self.goal_children)
        if method == "GET" and path.startswith("/repos/%s/issues?" % REPO):
            start = int(path.split("&page=")[1]) if "&page=" in path else 0
            chunk = self.issues[start:start + self.page]
            headers = {}
            if start + self.page < len(self.issues):
                headers["Link"] = '<https://api.github.com%s&page=%d>; rel="next"' % (
                    path.split("&page=")[0], start + self.page)
            return 200, headers, json.dumps(chunk).encode()
        if method == "POST" and path.endswith("/sub_issues"):
            if payload["sub_issue_id"] in self.refuse_sub_issue:
                return reply(422, {"message": "already has a parent"})
            return reply(201, {})
        if method == "POST":
            return reply(201, {})
        raise AssertionError("unexpected %s %s" % (method, path))

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def labelled(self):
        return {int(c[1].split("/issues/")[1].split("/")[0]): c[2]["labels"]
                for c in self.calls if c[0] == "POST" and c[1].endswith("/labels")}


class TriageTests(unittest.TestCase):
    def run_triage(self, routes, limit=25):
        return brain.triage(GitHub("t", transport=routes), REPO, 1, limit)

    def assert_never_edited(self, routes):
        # Only labels, sub-issue links and comments: no PATCH (close, retitle,
        # rewrite) and no other write of any kind.
        for method, path, _ in routes.writes():
            self.assertEqual(method, "POST", path)
            self.assertTrue(path.endswith(("/labels", "/comments", "/sub_issues")), path)

    def test_ready_issue_becomes_a_queued_ticket_under_the_goal(self):
        routes = Routes([item(5)])
        lines = self.run_triage(routes)
        self.assertEqual(routes.labelled(), {5: ["loom:ticket", "loom:queued"]})
        self.assertIn(("POST", "/repos/%s/issues/1/sub_issues" % REPO, {"sub_issue_id": 1005}),
                      routes.calls)
        self.assertFalse(any(c[1].endswith("/comments") for c in routes.calls))
        self.assertEqual(lines, ["#5 queued under #1"])
        self.assert_never_edited(routes)

    def test_not_ready_issue_gets_triage_and_names_what_is_missing(self):
        no_boxes = BODY.replace("- [ ] done", "done")
        routes = Routes([item(6, body="It is slow.\n## Why\nusers wait"), item(7, body=no_boxes),
                         item(8, body=None)])
        self.run_triage(routes)
        self.assertEqual(routes.labelled(), {6: ["loom:triage"], 7: ["loom:triage"], 8: ["loom:triage"]})
        comments = {int(c[1].split("/issues/")[1].split("/")[0]): c[2]["body"]
                    for c in routes.calls if c[1].endswith("/comments")}
        self.assertIn("Missing: What, Acceptance criteria, Size, Out of scope.", comments[6])
        self.assertNotIn("Why", comments[6].split("Missing:")[1].split(".")[0])
        self.assertIn("Missing: checkboxes under Acceptance criteria.", comments[7])
        self.assertIn("Missing: Why, What", comments[8])
        for body in comments.values():
            self.assertTrue(body.startswith(brain.TRIAGE_MARKER))
            text.check(body)
        self.assertFalse(any(c[1].endswith("/sub_issues") and c[0] == "POST" for c in routes.calls))
        self.assert_never_edited(routes)

    def test_a_body_the_runner_would_refuse_is_not_ready(self):
        routes = Routes([item(9, body=BODY + "\nRepo: other/repo")])
        self.run_triage(routes)
        self.assertEqual(routes.labelled(), {9: ["loom:triage"]})

    def test_pull_requests_epics_and_labelled_issues_are_skipped(self):
        routes = Routes([
            item(2, pull_request={"url": "x"}),
            item(3, labels=["Epic"]),
            item(4, sub_issues_summary={"total": 3, "completed": 0}),
            item(5, labels=["loom:triage"]),
            item(6, labels=["loom:human"]),
            item(7, labels=["needs-operator"]),
            item(8, labels=["bug"], body="broken"),
        ])
        self.run_triage(routes)
        self.assertEqual(routes.labelled(), {8: ["loom:triage"]})
        self.assert_never_edited(routes)

    def test_the_limit_holds_and_stops_reading_pages(self):
        routes = Routes([item(n, body="old issue") for n in range(10, 40)], page=10)
        lines = self.run_triage(routes, limit=12)
        self.assertEqual(sorted(routes.labelled()), list(range(10, 22)))
        self.assertEqual(len(lines), 12)
        pages = [c for c in routes.calls if c[1].startswith("/repos/%s/issues?" % REPO)]
        self.assertEqual(len(pages), 2)
        self.assertIn("sort=created&direction=asc", pages[0][1])
        self.assert_never_edited(routes)

    def test_ready_issues_wait_while_the_goal_is_at_its_cap(self):
        full = [item(50 + n, labels=["loom:ticket", "loom:queued"]) for n in range(2)]
        routes = Routes([item(5), item(6, body="vague")], goal_children=full)
        with unittest.mock.patch.dict(os.environ, {"LOOM_GOAL_CAP": "2"}):
            lines = self.run_triage(routes)
        self.assertEqual(routes.labelled(), {6: ["loom:triage"]})
        self.assertTrue(lines[0].startswith("#5 held"))

    def test_an_issue_github_refuses_as_a_sub_issue_is_left_alone(self):
        routes = Routes([item(5)], refuse_sub_issue={1005})
        lines = self.run_triage(routes)
        self.assertEqual(routes.labelled(), {})
        self.assertIn("skipped", lines[0])

    def test_bad_limit_and_bad_goal_write_nothing(self):
        routes = Routes([item(5)])
        with self.assertRaises(ValueError):
            self.run_triage(routes, limit=0)
        fake = Fake(reply(200, {"state": "open", "labels": []}))
        with self.assertRaises(ValueError):
            brain.triage(GitHub("t", transport=fake), REPO, 1)
        self.assertEqual(routes.writes() + [c for c in fake.calls if c[0] != "GET"], [])


class HumanAndRefsTests(unittest.TestCase):
    def test_human_posts_one_comment_and_swaps_triage_for_human(self):
        fake = Fake(reply(200, item(6, labels=["loom:triage"])), reply(201, {}), reply(200, []), reply(200, []))
        brain.human(GitHub("t", transport=fake), REPO, 6, "Buy a test phone.\n")
        self.assertEqual(fake.calls[1][2], {"body": brain.HUMAN_MARKER + "\nBuy a test phone."})
        self.assertEqual(fake.calls[2][0], "DELETE")
        self.assertTrue(fake.calls[2][1].endswith("/issues/6/labels/loom%3Atriage"))
        self.assertEqual(fake.calls[3][2], {"labels": ["loom:human"]})

    def test_human_refusals_write_nothing(self):
        cases = [(item(6, labels=["loom:human"]), "Buy it."), (item(6, state="closed"), "Buy it."),
                 (item(6, pull_request={}), "Buy it."), (item(6), "  ")]
        for issue, ask in cases:
            with self.subTest(ask=ask, issue=issue):
                fake = Fake(reply(200, issue))
                with self.assertRaises(ValueError):
                    brain.human(GitHub("t", transport=fake), REPO, 6, ask)
                self.assertTrue(all(c[0] == "GET" for c in fake.calls))

    def test_brief_refs_rewrites_without_touching_the_original(self):
        fake = Fake(reply(200, item(6, labels=["loom:triage"])), reply(200, goal()), reply(200, []),
                    reply(201, {"id": 202, "number": 20, "html_url": "https://example.com/t"}),
                    reply(201, {}), reply(201, {}), reply(200, []), reply(200, []))
        brain.brief(GitHub("t", transport=fake), REPO, 1, "Ticket", BODY, refs=6)
        self.assertTrue(fake.calls[3][2]["body"].endswith("\n\nRefs #6\n"))
        self.assertIn("Rewritten as ticket #20", fake.calls[5][2]["body"])
        self.assertEqual([c[0] for c in fake.calls[5:]], ["POST", "DELETE", "POST"])
        self.assertEqual(fake.calls[7][2], {"labels": ["loom:rewritten"]})
        self.assertFalse(any(c[0] == "PATCH" for c in fake.calls))

    def test_brief_refs_refuses_a_ticket_or_closed_original(self):
        for issue in (item(6, labels=["loom:ticket"]), item(6, state="closed")):
            fake = Fake(reply(200, issue))
            with self.assertRaises(ValueError):
                brain.brief(GitHub("t", transport=fake), REPO, 1, "Ticket", BODY, refs=6)
            self.assertTrue(all(c[0] == "GET" for c in fake.calls))


if __name__ == "__main__":
    unittest.main()
