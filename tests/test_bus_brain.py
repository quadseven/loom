from __future__ import annotations

import json
import os
import sys
import unittest

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

class Routed:
    """A transport answering by method and URL suffix, recording every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[1]
        self.calls.append((method, path, json.loads(body) if body else None))
        for (m, suffix), answer in self.routes.items():
            if m == method and path.split("?")[0].endswith(suffix):
                return answer
        if method == "POST" and "/graphql" in path:
            return reply(200, {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}})
        return reply(200, [] if method == "GET" else {})

    def sent(self, method, suffix):
        return [c[2] for c in self.calls if c[0] == method and c[1].split("?")[0].endswith(suffix)]


class ModelFlagTests(unittest.TestCase):
    def test_with_model_rewrites_or_inserts_the_line(self):
        self.assertEqual(brain._with_model("Difficulty: hard\nModel: old\n\n" + BODY, "new-1"),
                         "Difficulty: hard\nModel: new-1\n\n" + BODY)
        self.assertEqual(brain._with_model("Difficulty: hard\n\n" + BODY, "new-1"),
                         "Difficulty: hard\nModel: new-1\n\n" + BODY)
        self.assertEqual(brain._with_model(BODY, "new-1"), "Model: new-1\n\n" + BODY)
        # A body line that merely starts with Model: is not a header.
        self.assertTrue(brain._with_model("x\nModel: old", "new-1").startswith("Model: new-1\n\nx"))

    def test_move_model_rewrites_the_body_before_the_labels(self):
        issue = {"state": "open", "body": "Model: old\n\n" + BODY,
                 "labels": [{"name": "loom:ticket"}, {"name": "loom:failed"}, {"name": "lane:claude"}]}
        fake = Fake(reply(200, issue), reply(200, {}), reply(200, []))
        brain.move(GitHub("t", transport=fake), REPO, 8, "briefed", "codex", model="gpt-6-sol")
        self.assertEqual([c[0] for c in fake.calls], ["GET", "PATCH", "PUT"])
        self.assertEqual(fake.calls[1][2], {"body": "Model: gpt-6-sol\n\n" + BODY})
        self.assertEqual(fake.calls[2][2], {"labels": ["loom:ticket", "loom:briefed", "lane:codex"]})

    def test_move_model_refusals(self):
        for state, model in (("failed", "gpt-6-sol"), ("briefed", "bad model"), ("briefed", "x\nRepo: a/b")):
            fake = Fake()
            with self.assertRaises(ValueError):
                brain.move(GitHub("t", transport=fake), REPO, 8, state, model=model)
            self.assertEqual(fake.calls, [])

    def test_fix_model_and_lane_rewrite_the_body_and_swap_the_lane(self):
        ticket = dict(fix_ticket(), body=BODY)
        fake = Routed({("GET", "/pulls/8"): reply(200, fix_pr()), ("GET", "/issues/9"): reply(200, ticket),
                       ("POST", "/issues/9/comments"): reply(201, {"id": 5})})
        brain.fix(GitHub("t", transport=fake), REPO, 8, (REPO, 9), "Notes.", "claude", "claude-opus-5-5")
        self.assertEqual(fake.sent("PATCH", "/issues/9"), [{"body": "Model: claude-opus-5-5\n\n" + BODY}])
        self.assertEqual(fake.sent("PUT", "/issues/9/labels"),
                         [{"labels": ["loom:ticket", "custom", "loom:briefed", "lane:claude"]}])
        order = [c[0] for c in fake.calls if c[0] in ("PATCH", "PUT")]
        self.assertEqual(order, ["PATCH", "PUT"])

    def test_fix_refuses_an_unknown_lane_or_bad_model(self):
        for kw in (dict(lane="bogus"), dict(model="bad model")):
            fake = Fake()
            with self.assertRaises(ValueError):
                brain.fix(GitHub("t", transport=fake), REPO, 8, (REPO, 9), "n", **kw)
            self.assertEqual(fake.calls, [])


LANE_TABLE = "<!-- loom:lanes v1 -->\n| lane | available | heartbeat (UTC) |\n|---|---|---|\n%s"


def lanes_routes(free):
    rows = "\n".join("| %s | %s | - |" % (lane, "yes" if lane in free else "no")
                     for lane in ("claude", "codex", "codex-2", "anthropic", "anthropic-2"))
    return {("GET", "/repos/quadseven/loom/issues"): reply(200, [{"number": 99, "labels": [{"name": "loom:lanes"}]}]),
            ("GET", "/issues/99/comments"): reply(200, [{"body": LANE_TABLE % rows, "created_at": "2030-01-01T00:00:00Z"}])}


def event(kind, label, at):
    return {"event": kind, "label": {"name": label}, "created_at": at}


def claim(at, lane="claude"):
    return {"body": "Claimed for lane %s (body sha256 0123456789ab)" % lane, "created_at": at}


def fixc(at):
    return {"body": "<!-- loom:fix v1 -->\nPull request: %s#8, branch loom/9-work\n" % REPO, "created_at": at}


def stuck_ticket(*states, body="Model: claude-sonnet-5-5\n\n" + BODY, created="2030-01-01T00:00:00Z"):
    names = ["loom:ticket", "lane:claude"] + ["loom:" + x for x in states]
    return {"number": 9, "state": "open", "title": "T", "body": body, "created_at": created,
            "labels": [{"name": n} for n in names]}


NOW = "2030-01-01T01:00:00Z"
OLD = "2020-01-01T00:00:00Z"


def ticket_routes(issue, events, comments, free=("claude", "codex", "codex-2")):
    routes = lanes_routes(free)
    routes.update({("GET", "/repos/%s/issues/9" % REPO): reply(200, issue),
                   ("GET", "/issues/9/events"): reply(200, events),
                   ("GET", "/issues/9/comments"): reply(200, comments),
                   ("GET", "/repos/%s/pulls" % REPO): reply(200, [dict(fix_pr(), number=8)]),
                   ("GET", "/pulls/8"): reply(200, fix_pr())})
    return routes


TWICE = [event("labeled", "loom:failed", "2030-01-01T00:10:00Z"),
         event("labeled", "loom:failed", "2030-01-01T00:40:00Z")]


class EscalateTests(unittest.TestCase):
    def run_escalate(self, routes):
        fake = Routed(routes)
        gh = GitHub("t", transport=fake)
        return fake, brain.escalate(gh, REPO, 9)

    def stuck_cases(self):
        sonnet_claim = [claim("2030-01-01T00:11:00Z")]
        return {
            "failed twice": (TWICE, sonnet_claim, "failed"),
            "two fix rounds": ([], [fixc("2030-01-01T00:20:00Z"), fixc("2030-01-01T00:30:00Z")], "pr"),
            "briefed unclaimed 2h twice": ([event("labeled", "loom:briefed", "2020-01-01T00:00:00Z"),
                                            event("unlabeled", "loom:briefed", "2020-01-02T00:00:00Z"),
                                            event("labeled", "loom:briefed", "2020-01-03T00:00:00Z")], [], "briefed"),
            "failed for 6 hours": ([event("labeled", "loom:failed", OLD)], [], "failed"),
        }

    def test_status_marks_each_kind_of_repeatedly_stuck_ticket(self):
        for name, (events, comments, state) in self.stuck_cases().items():
            with self.subTest(name):
                routes = {("GET", "/repos/%s/issues" % REPO): reply(200, [stuck_ticket(state)]),
                    ("GET", "/issues/9/events"): reply(200, events),
                    ("GET", "/issues/9/comments"): reply(200, comments)}
                routes.update(lanes_routes(()))
                routes[("GET", "/repos/%s/pulls" % REPO)] = reply(200, [])
                report = brain.status(GitHub("t", transport=Routed(routes)), [REPO], REPO)
                self.assertIn("REPEATEDLY STUCK", report)
                self.assertIn("next rung", report)

    def test_status_leaves_an_ordinary_ticket_unmarked(self):
        issue = dict(stuck_ticket("failed"), created_at="2030-01-01T00:00:00Z")
        issue["labels"] = [{"name": "loom:ticket"}, {"name": "loom:failed"}]
        routes = {("GET", "/repos/%s/issues" % REPO): reply(200, [issue]),
                  ("GET", "/issues/9/events"): reply(200, [event("labeled", "loom:failed", "2999-01-01T00:00:00Z")]),
                  ("GET", "/issues/9/comments"): reply(200, []),
                  ("GET", "/repos/%s/pulls" % REPO): reply(200, [])}
        routes.update(lanes_routes(()))
        self.assertNotIn("REPEATEDLY STUCK", brain.status(GitHub("t", transport=Routed(routes)), [REPO], REPO))

    def test_status_shows_attempts_and_the_next_rung(self):
        issue = stuck_ticket("failed")
        routes = {("GET", "/repos/%s/issues" % REPO): reply(200, [issue]),
                  ("GET", "/issues/9/events"): reply(200, TWICE),
                  ("GET", "/issues/9/comments"): reply(200, [claim("2030-01-01T00:11:00Z"), fixc("2030-01-01T00:50:00Z")]),
                  ("GET", "/repos/%s/pulls" % REPO): reply(200, [])}
        routes.update(lanes_routes(()))
        report = brain.status(GitHub("t", transport=Routed(routes)), [REPO], REPO)
        self.assertIn("failed 2 times", report)
        self.assertIn("attempts 3", report)
        self.assertIn("tried claude-sonnet-5-5", report)
        self.assertIn("next rung 2 gpt-6-sol", report)

    def test_escalate_moves_a_failed_ticket_to_the_next_rung(self):
        fake, outcome = self.run_escalate(ticket_routes(
            stuck_ticket("failed"), TWICE, [claim("2030-01-01T00:11:00Z")]))
        self.assertEqual(outcome, "rung 2 gpt-6-sol on codex")
        self.assertEqual(fake.sent("PATCH", "/issues/9"), [{"body": "Model: gpt-6-sol\n\n" + BODY}])
        self.assertEqual(fake.sent("PUT", "/issues/9/labels"),
                         [{"labels": ["loom:ticket", "loom:briefed", "lane:codex"]}])
        note = fake.sent("POST", "/issues/9/comments")[-1]["body"]
        self.assertIn("rung 2 of 3", note)
        self.assertIn("failed 2 times", note)
        self.assertIn("Rung: 2 gpt-6-sol codex", note)

    def test_escalate_skips_a_busy_lane_for_a_higher_rung(self):
        fake, outcome = self.run_escalate(ticket_routes(
            stuck_ticket("failed"), TWICE, [claim("2030-01-01T00:11:00Z")], free=("claude", "anthropic-2")))
        self.assertEqual(outcome, "rung 3 claude-opus-5-5 on claude")
        self.assertEqual(fake.sent("PATCH", "/issues/9"), [{"body": "Model: claude-opus-5-5\n\n" + BODY}])

    def test_escalate_uses_the_codex_standin_and_anthropic_slot(self):
        _, outcome = self.run_escalate(ticket_routes(
            stuck_ticket("failed"), TWICE, [claim("2030-01-01T00:11:00Z")], free=("codex-2",)))
        self.assertEqual(outcome, "rung 2 gpt-6-sol on codex-2")
        history = [claim("2030-01-01T00:11:00Z"), claim("2030-01-01T00:30:00Z", "codex")]
        _, outcome = self.run_escalate(ticket_routes(
            stuck_ticket("failed", body="Model: gpt-6-sol\n\n" + BODY), TWICE, history, free=("anthropic-2",)))
        self.assertEqual(outcome, "rung 3 claude-opus-5-5 on anthropic-2")

    def test_escalate_refuses_when_no_untried_rung_has_a_free_lane(self):
        fake = Routed(ticket_routes(stuck_ticket("failed"), TWICE, [claim("2030-01-01T00:11:00Z")], free=()))
        with self.assertRaises(ValueError):
            brain.escalate(GitHub("t", transport=fake), REPO, 9)
        self.assertFalse(fake.sent("PUT", "/issues/9/labels"))

    def test_escalate_sends_an_open_pull_request_back_as_a_fix_brief(self):
        issue = stuck_ticket("pr")
        fake, outcome = self.run_escalate(ticket_routes(
            issue, [], [fixc("2030-01-01T00:20:00Z"), claim("2030-01-01T00:25:00Z"), fixc("2030-01-01T00:30:00Z")]))
        self.assertEqual(outcome, "rung 2 gpt-6-sol on codex")
        comments = [b["body"] for b in fake.sent("POST", "/issues/9/comments")]
        self.assertTrue(comments[0].startswith("<!-- loom:fix v1 -->\nPull request: %s#8, branch loom/9-work" % REPO))
        self.assertTrue(comments[1].startswith("<!-- loom:escalate v1 -->"))
        self.assertEqual(fake.sent("PUT", "/issues/9/labels"),
                         [{"labels": ["loom:ticket", "loom:briefed", "lane:codex"]}])
        self.assertEqual(fake.sent("PATCH", "/issues/9"), [{"body": "Model: gpt-6-sol\n\n" + BODY}])

    def test_escalate_refuses_a_ticket_that_is_not_repeatedly_stuck(self):
        issue = dict(stuck_ticket("failed"), created_at="2999-01-01T00:00:00Z")
        fake = Routed(ticket_routes(issue, [event("labeled", "loom:failed", "2999-01-01T00:00:00Z")], []))
        with self.assertRaises(ValueError) as caught:
            brain.escalate(GitHub("t", transport=fake), REPO, 9)
        self.assertIn("not repeatedly stuck", str(caught.exception))
        self.assertFalse([c for c in fake.calls if c[0] in ("PUT", "PATCH") or c[0] == "POST"])

    def test_escalate_past_the_third_rung_adds_needs_brain(self):
        history = [claim("2030-01-01T00:11:00Z"), claim("2030-01-01T00:20:00Z", "codex"),
                   {"body": "<!-- loom:escalate v1 -->\nRung: 3 claude-opus-5-5 claude", "created_at": "2030-01-01T00:30:00Z"},
                   claim("2030-01-01T00:31:00Z")]
        issue = stuck_ticket("failed", body="Model: claude-opus-5-5\n\n" + BODY)
        fake, outcome = self.run_escalate(ticket_routes(issue, TWICE, history))
        self.assertEqual(outcome, "needs-brain")
        self.assertIn("needs-brain", fake.sent("PUT", "/issues/9/labels")[0]["labels"])
        self.assertFalse(fake.sent("PATCH", "/issues/9"))

    def test_escalate_tracks_the_rewritten_model_per_claim(self):
        # Sonnet claimed, escalated to codex and claimed again: the next rung is the last.
        history = [claim("2030-01-01T00:11:00Z"),
                   {"body": "<!-- loom:escalate v1 -->\nRung: 2 gpt-6-sol codex", "created_at": "2030-01-01T00:20:00Z"},
                   claim("2030-01-01T00:21:00Z", "codex")]
        issue = stuck_ticket("failed", body="Model: gpt-6-sol\n\n" + BODY)
        _, outcome = self.run_escalate(ticket_routes(issue, TWICE, history))
        self.assertEqual(outcome, "rung 3 claude-opus-5-5 on claude")



if __name__ == "__main__":
    unittest.main()
