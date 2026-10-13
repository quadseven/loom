"""The factory pass (bus/factory.py) with a fake GitHub and recorded actions."""

import os
import re
import tempfile
import unittest
from datetime import datetime, timezone

from bus import factory, station
from bus.github import Response
from bus.tickets import FIX_MARKER

REPO = "example-org/example-repo"
NOW = 2_000_000_000.0
# A loom:fix comment that asked for thread PRRT_1 and a failing check.
FIX_ASKED = FIX_MARKER + "\nPull request: x#50\n\n### Check: lint\n\n    E1\n\n### Thread PRRT_1 (comment 1) at a.py:1\n\n    rename"
ME = {"login": "alice-example"}
# Every rung of the ladder tried since the last restart.
ALL_RUNGS = [{"body": factory.RUNG_MARKER % n, "user": ME} for n in (1, 2, 3)]
STRANGER = {"login": "mallory-example"}


def iso(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.events = {}
        self.pulls = {}
        self.runs = {}
        self.details = {}
        self.writes = []
        self.behind = {}
        self.attempts = {}
        self.update_status = 202
        self.files = {}
        self.filed = []
        self.private = True

    def ticket(self, number, state, lane="codex", body="Difficulty: medium\n\n## Why\nx\n"):
        self.issues[number] = {"number": number, "state": "open", "body": body, "user": ME,
                               "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state},
                                          {"name": "lane:" + lane}]}
        self.comments[number] = []
        self.events[number] = []

    def pull(self, number, ticket, created=3600, mergeable=True, green=True,
             resolved=True, runs=(("unittest", "completed"), (factory.DEFAULT_REVIEW_CHECK, "completed")),
             updated=3600, files=("bus/a.py", "tests/test_a.py")):
        sha = "sha%d" % number
        self.files[number] = list(files)
        self.pulls[number] = {
            "number": number, "state": "open", "draft": False, "body": "", "user": ME,
            "head": {"ref": "loom/%d-some-title" % ticket, "sha": sha,
                     "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "created_at": iso(created), "updated_at": iso(updated), "mergeable": mergeable}
        self.runs[sha] = [{"name": n, "status": s} for n, s in runs]
        self.details[number] = {
            "isDraft": False, "mergeable": "MERGEABLE" if mergeable else "CONFLICTING",
            "headRefName": "loom/%d-some-title" % ticket, "headRefOid": sha,
            "headRepository": {"nameWithOwner": REPO},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "state": "SUCCESS" if green else "FAILURE"}}}]},
            "reviewThreads": {"nodes": [{"id": "PRRT_1", "isResolved": resolved,
                                         "comments": {"nodes": []}}]}}

    # -- the GitHub client surface the factory uses --------------------
    def paginate(self, path):
        if re.search(r"/issues\?labels=loom:ticket", path):
            return list(self.issues.values())
        if path.endswith("/pulls?state=open"):
            return list(self.pulls.values())
        if path.endswith("/issues?labels=loom:triage&state=open"):
            return list(self.filed)
        m = re.search(r"/issues/(\d+)/comments$", path)
        if m:
            return list(self.comments[int(m.group(1))])
        m = re.search(r"/issues/(\d+)/events$", path)
        if m:
            return list(self.events[int(m.group(1))])
        raise AssertionError("unexpected paginate %s" % path)

    def rest(self, method, path, body=None, etag=None, ok=()):
        if method == "GET":
            return self._get(path)
        return self._write(method, path, body)

    def _get(self, path):
        m = re.search(r"/pulls/(\d+)$", path)
        if m:
            return Response(200, self.pulls[int(m.group(1))])
        m = re.search(r"/pulls/(\d+)/files\?per_page=100&page=(\d+)$", path)
        if m:
            page = int(m.group(2))
            names = self.files[int(m.group(1))][(page - 1) * 100:page * 100]
            return Response(200, [{"filename": n} for n in names])
        m = re.search(r"/commits/(\w+)/check-runs", path)
        if m:
            return Response(200, {"check_runs": self.runs[m.group(1)]})
        m = re.search(r"/compare/main\.\.\.sha(\d+)$", path)
        if m:
            return Response(200, {"behind_by": self.behind.get(int(m.group(1)), 0)})
        if path == "/repos/%s" % REPO:
            return Response(200, {"private": self.private})
        m = re.search(r"/actions/runs/(\d+)$", path)
        if m:
            return Response(200, {"run_attempt": self.attempts.get(m.group(1), 1)})
        raise AssertionError("unexpected GET %s" % path)

    def _write(self, method, path, body):
        self.writes.append((method, path, body))
        if path.endswith("/update-branch"):
            return Response(self.update_status, {})
        if method == "POST" and path.endswith("/issues"):
            made = dict(body, number=900 + len(self.filed), user=ME)
            self.filed.append(made)
            return Response(201, made)
        m = re.search(r"/issues/(\d+)(/labels|/comments)?$", path)
        if m:
            number = int(m.group(1))
            if m.group(2) == "/labels":
                self.issues[number]["labels"] = [{"name": x} for x in body["labels"]]
            elif m.group(2) == "/comments":
                self.comments[number].append({"body": body["body"], "created_at": iso(0), "user": ME})
            elif method == "PATCH":
                self.issues[number]["body"] = body["body"]
        return Response(200, {})

    def graphql(self, query, variables=None):
        if "addPullRequestReviewThreadReply" in query:
            self.writes.append(("REPLY", variables["id"], variables["body"]))
            return {"addPullRequestReviewThreadReply": {"comment": {"id": "c"}}}
        if "resolveReviewThread" in query:
            self.writes.append(("RESOLVE", variables["id"], None))
            for details in self.details.values():
                for thread in details["reviewThreads"]["nodes"]:
                    if thread["id"] == variables["id"]:
                        thread["isResolved"] = True
            return {"resolveReviewThread": {"thread": {"isResolved": True}}}
        if variables and "number" in variables:
            number = variables["number"]
        else:
            number = int(re.search(r"pullRequest\(number:(\d+)\)", query).group(1))
        return {"repository": {"pullRequest": self.details[number]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.merged, self.fixed, self.moved = [], [], []

    def go(self, limited=(), no_test_repos=(), available=None):
        def merge(gh, repo, number, ticket):
            self.merged.append((number, ticket))
        def fix(gh, repo, number, ticket, notes):
            f.notes = notes
            self.fixed.append((number, ticket, gh.issues[ticket[1]]["labels"][:]))
        def move(gh, repo, number, state, lane=None, note=None):
            self.moved.append((number, state, lane))
        f = factory.Factory(self.gh, [REPO], NOW, trusted=["Alice-Example"],
                            merge=merge, fix=fix, move=move, limited=limited,
                            no_test_repos=no_test_repos, available=available)
        f.run()
        return f

    def lane(self, number):
        return factory._lane(self.gh.issues[number])

    def model(self, number):
        m = re.search(r"^Model: (\S+)$", self.gh.issues[number]["body"], re.M)
        return m.group(1) if m else None


class PullRequests(Base):
    def test_a_ready_pull_request_is_merged(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual(self.fixed, [])

    def test_code_without_a_test_goes_back_for_a_failing_test(self):
        self.gh.ticket(5, "pr", lane="codex")
        self.gh.pull(50, 5, files=("src/app.css", "README.md"))
        f = self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertIn("fails without the change", f.notes)
        # A finding round: the first one stays on its lane.
        self.assertEqual((self.lane(5), factory.rung_of(self.gh.comments[5])), ("codex", 0))

    def test_a_second_round_still_without_a_test_climbs(self):
        self.gh.ticket(5, "pr", lane="opencode-go")
        self.gh.comments[5].append({"body": FIX_MARKER + "\n\n" + factory.TEST_NOTES, "user": ME})
        self.gh.pull(50, 5, files=("src/main.cpp",))
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 1)

    def test_code_with_a_test_on_a_later_page_lands(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, files=["src/m%d.go" % n for n in range(150)] + ["src/m_test.go"])
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_doc_and_config_only_changes_land_without_a_test(self):
        for number, files in ((50, ("README.md", "docs/x.md")),
                              (51, (".github/workflows/ci.yml", "config.json", "Dockerfile"))):
            self.setUp()
            self.gh.ticket(5, "pr")
            self.gh.pull(number, 5, files=files)
            self.go()
            self.assertEqual(self.merged, [(number, (REPO, 5))], files)

    def test_an_opted_out_repo_lands_code_without_a_test(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, files=("src/a.lua",))
        self.go(no_test_repos=[REPO.upper()])
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_a_ticket_marked_tests_none_needed_lands_code_without_a_test(self):
        self.gh.ticket(5, "pr", body="Difficulty: medium\nTests: none needed (style only)\n\n## Why\nx\n")
        self.gh.pull(50, 5, files=("src/a.css",))
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_what_counts_as_code_and_as_a_test(self):
        for path in ("a/b.PY", "x.tsx", "m.hpp", "q.sql", "run.sh"):
            self.assertTrue(factory.is_code(path), path)
        for path in ("README.md", "a.yml", "a.json", "Makefile"):
            self.assertFalse(factory.is_code(path), path)
        for path in ("tests/x.py", "src/test/Foo.cpp", "test_x.py", "x_test.go",
                     "a.test.ts", "a.spec.js", "t/TestBlackfathomDeeps.cpp",
                     "spec/a_spec.rb", "src/__tests__/a.js", "testing.py"):
            self.assertTrue(factory.is_test(path), path)
        for path in ("src/latest.py", "src/latest_x.py", "contest/a.py",
                     "src/contest.py", "src/latest.cpp"):
            self.assertFalse(factory.is_test(path), path)

    def test_lacks_test_recognizes_t_test_and_test_prefix(self):
        self.assertFalse(factory.lacks_test(["src/a.cpp", "t/TestA.cpp"]))

    def test_it_waits_while_a_check_runs(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False, runs=(("unittest", "in_progress"),
                                                  (factory.DEFAULT_REVIEW_CHECK, "completed")))
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_it_never_merges_without_the_review_on_the_head_commit(self):
        self.gh.ticket(5, "pr")
        for updated in (60, factory.REVIEW_GRACE + 60):
            self.gh.pull(50, 5, runs=(("unittest", "completed"),), updated=updated)
            self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_an_unreviewed_failing_pull_request_still_goes_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False, runs=(("unittest", "completed"),))
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_unknown_mergeability_waits(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=None)
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_a_first_send_back_of_a_young_pull_request_stays_on_its_lane(self):
        self.gh.ticket(5, "pr", lane="codex")
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual((self.lane(5), self.model(5)), ("codex", None))
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 0)

    def test_a_thread_that_survives_a_fix_round_climbs_to_the_first_rung(self):
        self.gh.ticket(5, "pr", lane="opencode-go")
        self.gh.comments[5].append({"body": FIX_ASKED, "user": ME})
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual((self.lane(5), self.model(5)), ("claude", "claude-sonnet-5-5"))
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 1)
        # The lane label changed before the fix brief went out.
        self.assertIn({"name": "lane:claude"}, self.fixed[0][2])

    def test_a_review_round_that_finds_new_things_stays_on_its_lane(self):
        self.gh.ticket(5, "pr", lane="codex")
        self.gh.comments[5].append({"body": FIX_ASKED.replace("PRRT_1", "PRRT_0").replace(
            "### Check: lint\n\n    E1\n\n", ""), "user": ME})
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual((self.lane(5), self.model(5)), ("codex", None))
        self.assertEqual(len(self.fixed), 1)

    def test_checks_still_failing_after_a_round_that_asked_for_them_climb(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": FIX_ASKED, "user": ME})
        self.gh.pull(50, 5, green=False)
        self.go()
        self.assertEqual(self.model(5), "claude-sonnet-5-5")

    def test_a_conflict_alone_never_climbs(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": FIX_ASKED, "user": ME})
        self.gh.pull(50, 5, mergeable=False)
        self.go()
        self.assertEqual(self.model(5), None)
        self.assertEqual(len(self.fixed), 1)

    def test_a_fresh_push_without_its_review_yet_waits(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False, runs=(), updated=60)
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_a_rung_zero_marker_starts_the_ladder_again(self):
        self.gh.ticket(6, "failed")
        self.gh.comments[6] += [{"body": factory.RUNG_MARKER % 3, "user": ME},
                                {"body": factory.RUNG_MARKER % 0, "user": ME}]
        self.go()
        self.assertEqual(self.model(6), "claude-sonnet-5-5")

    def test_age_climbs_once_per_window_not_every_round(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": factory.RUNG_MARKER % 1, "user": ME,
                                    "created_at": iso(3600)})
        self.gh.pull(50, 5, mergeable=False, created=factory.STUCK_AGE * 2)
        self.go()
        self.assertEqual(self.model(5), None, "climbed an hour after the last climb")
        self.assertEqual(len(self.fixed), 1)
        self.gh.comments[5][0]["created_at"] = iso(factory.STUCK_AGE + 60)
        self.gh.issues[5]["labels"] = [{"name": x} for x in ("loom:ticket", "loom:pr", "lane:codex")]
        self.go()
        self.assertEqual(self.model(5), "gpt-6-sol")

    def test_an_old_pull_request_climbs_on_its_first_send_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=False, created=factory.STUCK_AGE + 60)
        self.go()
        self.assertEqual(self.model(5), "claude-sonnet-5-5")
        self.assertEqual(len(self.fixed), 1)

    def test_the_rungs_go_sonnet_then_sol_then_opus(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": FIX_ASKED, "user": ME})
        seen = []
        for _ in range(3):
            self.gh.pull(50, 5, resolved=False)
            self.gh.issues[5]["labels"] = [{"name": x} for x in
                                           ("loom:ticket", "loom:pr", "lane:" + (self.lane(5) or "codex"))]
            self.go()
            seen.append((self.lane(5), self.model(5)))
        self.assertEqual(seen, [("claude", "claude-sonnet-5-5"), ("codex", "gpt-6-sol"),
                                ("claude", "claude-opus-5-5")])

    def test_past_the_last_rung_it_is_left_failed_for_the_brain(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5] += ALL_RUNGS + [{"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.moved, [(5, "failed", None)])

    def test_a_rung_skipped_while_busy_is_still_tried_later(self):
        # Spilled straight to rung 3 while rungs 1 and 2 were busy: stuck
        # again, it goes to rung 1, not past the end of the ladder.
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 3, "user": ME},
                                {"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual(self.moved, [])
        self.assertEqual((self.lane(5), self.model(5)), ("claude", "claude-sonnet-5-5"))
        self.assertEqual(factory.tried_rungs(self.gh.comments[5]), {1, 3})

    def test_a_ticket_that_failed_on_a_ladder_model_does_not_get_it_again(self):
        self.gh.ticket(6, "failed", lane="claude",
                       body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        self.go()
        self.assertEqual(self.model(6), "gpt-6-sol")

    def test_a_ready_pull_request_behind_its_base_is_brought_up_to_date_not_merged(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 3
        self.go()
        self.assertEqual(self.merged, [])
        self.assertIn(("PUT", "/repos/%s/pulls/50/update-branch" % REPO,
                       {"expected_head_sha": "sha50"}), self.gh.writes)

    def test_a_refused_update_waits_for_the_next_pass(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 1
        self.gh.update_status = 422
        f = self.go()
        self.assertEqual((self.merged, self.fixed, f.actions), ([], [], 0))

    def test_each_repo_lands_one_pull_request_a_pass_oldest_first(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200)
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6, created=3600)
        self.gh.pulls = dict(reversed(list(self.gh.pulls.items())))
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_an_urgent_tickets_pull_request_lands_first(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200)
        self.gh.ticket(6, "pr")
        self.gh.issues[6]["labels"].append({"name": "loom:urgent"})
        self.gh.pull(60, 6, created=3600)
        self.go()
        self.assertEqual(self.merged, [(60, (REPO, 6))])

    def test_a_regression_tickets_pull_request_lands_first(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200)
        self.gh.ticket(6, "pr")
        self.gh.issues[6]["labels"].append({"name": "loom:regression"})
        self.gh.pull(60, 6, created=3600)
        self.go()
        self.assertEqual(self.merged, [(60, (REPO, 6))])

    def test_a_failed_actions_job_is_run_once_more_before_a_send_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": "unittest", "status": "completed", "conclusion": "failure",
             "details_url": "https://github.com/%s/actions/runs/77/job/1" % REPO},
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "success"}]
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertIn(("POST", "/repos/%s/actions/runs/77/rerun-failed-jobs" % REPO, None),
                      self.gh.writes)
        self.gh.attempts["77"] = 2
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_a_failed_review_is_never_run_again(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "failure",
             "details_url": "https://github.com/%s/actions/runs/78/job/1" % REPO}]
        self.go()
        self.assertEqual(len(self.fixed), 1)
        self.assertFalse(any("rerun" in w[1] for w in self.gh.writes))

    def test_pull_requests_of_tickets_not_in_pr_are_left_alone(self):
        self.gh.ticket(5, "briefed")
        self.gh.pull(50, 5)
        self.gh.pulls[51] = dict(self.gh.pulls[50], number=51, head={"ref": "feature/x"})
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))


BOT = {"__typename": "Bot", "login": "grug-tribe"}
FIXED = "Fixed in 1234567: the flagged code changed and this review does not raise it again."
NO_CHANGE = "Lane claude made no change for the fix to #50 and said why on the pull request"


class ReviewBotThreads(Base):
    def thread(self, *comments):
        nodes = [{"author": a, "body": b} for a, b in comments]
        self.gh.details[50]["reviewThreads"]["nodes"][0].update(
            comments={"nodes": nodes}, root={"nodes": nodes[:1]})

    def test_a_thread_the_bot_marked_fixed_is_resolved_and_the_pull_request_lands(self):
        self.gh.ticket(5, "pr", lane="codex")
        self.gh.comments[5].append({"body": FIX_ASKED.replace("### Check: lint\n\n    E1\n\n", ""),
                                    "user": ME})
        self.gh.pull(50, 5, resolved=False)
        self.thread((BOT, "medium finding"), (BOT, FIXED))
        self.go()
        self.assertIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual((self.fixed, factory.rung_of(self.gh.comments[5])), ([], 0))

    def test_a_person_with_the_bot_login_does_not_resolve_a_thread(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False)
        self.thread((BOT, "finding"), ({"__typename": "User", "login": "grug-tribe"}, FIXED))
        self.go()
        self.assertNotIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual(self.merged, [])

    def test_a_thread_a_person_opened_stays_open_even_under_a_bot_fixed_reply(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False)
        self.thread((ME, "this breaks on an empty list"), (BOT, FIXED))
        self.go()
        self.assertNotIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual(self.merged, [])

    def test_a_reply_after_the_bot_marked_it_fixed_keeps_it_open(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False)
        self.thread((BOT, "finding"), (BOT, FIXED), (ME, "this still breaks when x is empty"))
        self.go()
        self.assertNotIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual(self.merged, [])

    def test_a_no_change_round_waits_for_the_review_it_asked_for(self):
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.comments[5] += [{"body": FIX_ASKED, "user": ME, "created_at": iso(600)},
                                {"body": NO_CHANGE, "user": ME, "created_at": iso(120)}]
        self.gh.pull(50, 5, resolved=False, green=False)
        self.gh.runs["sha50"][1]["completed_at"] = iso(900)
        self.go()
        self.assertEqual((self.fixed, self.merged), ([], []))
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 0)

    def test_a_no_change_round_is_judged_once_the_review_answered(self):
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.comments[5] += [{"body": FIX_ASKED, "user": ME, "created_at": iso(600)},
                                {"body": NO_CHANGE, "user": ME, "created_at": iso(120)}]
        self.gh.pull(50, 5, resolved=False, green=False)
        self.gh.runs["sha50"][1]["completed_at"] = iso(30)
        self.go()
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 1)

    def test_a_no_change_round_with_no_review_is_judged_after_the_grace(self):
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.comments[5] += [{"body": FIX_ASKED, "user": ME, "created_at": iso(7200)},
                                {"body": NO_CHANGE, "user": ME,
                                 "created_at": iso(factory.REVIEW_GRACE + 60)}]
        self.gh.pull(50, 5, resolved=False, green=False)
        self.go()
        self.assertEqual(len(self.fixed), 1)


MEDIUM = "\U0001f7e0 medium | _robustness_ | `missing-error-handling` | quick win\n\nGrug see a gap."
HIGH = "\U0001f525 high | _correctness_ | `null-deref` | quick win\n\nGrug see a crash."
ASKED_ONCE = FIX_MARKER + "\nPull request: x#50\n\n### Thread PRRT_1 (comment 1) at a.py:1\n\n    gap"
ASKED_OTHER = FIX_MARKER + "\nPull request: x#50\n\n### Thread PRRT_9 (comment 9) at a.py:9\n\n    other"


class DeferredFindings(Base):
    def setUp(self):
        super().setUp()
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False)

    def finding(self, body, author=BOT, asked=(ASKED_ONCE,)):
        root = {"author": author, "body": body, "url": "https://example.invalid/pr/50#r1"}
        self.gh.details[50]["reviewThreads"]["nodes"][0].update(
            path="bus/a.py", comments={"nodes": [root]}, root={"nodes": [root]})
        self.gh.comments[5] += [{"body": b, "user": ME} for b in asked]

    def test_a_medium_finding_a_round_asked_for_goes_to_a_follow_up_and_the_pull_request_lands(self):
        self.finding(MEDIUM)
        self.go()
        self.assertEqual(len(self.gh.filed), 1)
        filed = self.gh.filed[0]
        self.assertEqual((filed["title"], filed["labels"]),
                         ("Deferred review findings from %s#50" % REPO, ["loom:triage"]))
        self.assertIn("- medium | _robustness_ | `missing-error-handling` | quick win in `bus/a.py`:"
                      " https://example.invalid/pr/50#r1", filed["body"])
        self.assertTrue(all(ord(ch) < 128 for ch in filed["body"]))
        replies = [w for w in self.gh.writes if w[0] == "REPLY"]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0][2].startswith("Deferred to #900: "))
        self.assertIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual((self.merged, self.fixed), ([(50, (REPO, 5))], []))

    def test_a_new_medium_finding_on_the_first_round_goes_back_to_the_lane(self):
        self.finding(MEDIUM, asked=(ASKED_OTHER,))
        self.go()
        self.assertEqual((self.gh.filed, self.merged, len(self.fixed)), ([], [], 1))

    def test_after_two_fix_rounds_a_new_medium_finding_is_deferred(self):
        self.finding(MEDIUM, asked=(ASKED_OTHER, ASKED_OTHER))
        self.go()
        self.assertEqual((len(self.gh.filed), self.merged), (1, [(50, (REPO, 5))]))

    def test_prose_that_starts_with_low_is_not_a_low_finding(self):
        self.finding("Low risk, but this list can be empty.", asked=(ASKED_ONCE, ASKED_ONCE))
        self.go()
        self.assertEqual((self.gh.filed, self.merged), ([], []))

    def test_a_finding_line_survives_an_empty_or_blank_root(self):
        for root in ({}, {"body": "   "}):
            with self.subTest(root=root):
                line = factory.finding_line({"id": "PRRT_2", "path": "a.py", "root": {"nodes": [root]}})
                self.assertEqual(line, "- finding in `a.py`: PRRT_2")

    def test_a_high_finding_is_never_deferred(self):
        self.finding(HIGH, asked=(ASKED_ONCE, ASKED_ONCE, ASKED_ONCE))
        self.go()
        self.assertEqual((self.gh.filed, self.merged), ([], []))
        self.assertNotIn(("RESOLVE", "PRRT_1", None), self.gh.writes)

    def test_a_thread_a_person_opened_is_never_deferred(self):
        self.finding(MEDIUM, author={"__typename": "User", "login": "grug-tribe"},
                     asked=(ASKED_ONCE, ASKED_ONCE))
        self.go()
        self.assertEqual((self.gh.filed, self.merged), ([], []))

    def test_without_the_review_check_on_the_head_nothing_is_deferred(self):
        self.gh.runs["sha50"] = [{"name": "unittest", "status": "completed"}]
        self.gh.pulls[50]["updated_at"] = iso(factory.REVIEW_GRACE + 60)
        self.finding(MEDIUM)
        self.go()
        self.assertEqual((self.gh.filed, self.merged), ([], []))

    def test_an_open_follow_up_from_an_earlier_pass_is_reused(self):
        self.gh.filed.append({"number": 777, "title": "Deferred review findings from %s#50" % REPO,
                              "user": ME})
        self.finding(MEDIUM)
        self.go()
        self.assertEqual(len(self.gh.filed), 1)
        self.assertTrue([w for w in self.gh.writes if w[0] == "REPLY"][0][2].startswith("Deferred to #777: "))
        self.assertEqual(self.merged, [(50, (REPO, 5))])


class Trust(Base):
    def test_a_strangers_ticket_or_pull_request_is_never_touched(self):
        self.gh.ticket(5, "pr")
        self.gh.issues[5]["user"] = STRANGER
        self.gh.pull(50, 5)
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6)
        self.gh.pulls[60]["user"] = STRANGER
        self.gh.ticket(7, "failed")
        self.gh.issues[7]["user"] = STRANGER
        self.go()
        self.assertEqual((self.merged, self.fixed, self.moved), ([], [], []))

    def test_a_strangers_rung_or_fix_comment_does_not_move_the_ladder(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5] += [{"body": FIX_MARKER, "user": STRANGER},
                                {"body": factory.RUNG_MARKER % 3, "user": STRANGER}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        # Treated as a first send-back on its own lane, not past the last rung.
        self.assertEqual(len(self.fixed), 1)
        self.assertEqual((self.lane(5), self.model(5)), ("codex", None))


class Tickets(Base):
    def test_a_failed_ticket_without_a_pull_request_is_rebriefed_a_rung_up(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude")])
        self.assertEqual(self.model(6), "claude-sonnet-5-5")

    def test_failed_tickets_spread_past_a_busy_sonnet_lane(self):
        for n in (1, 2, 3, 4):
            self.gh.ticket(n, "failed", lane="zai")
        self.go()
        self.assertEqual([m[2] for m in self.moved], ["claude", "claude", "codex", "codex-2"])

    def test_a_failed_ticket_with_a_pull_request_gets_a_fix_brief(self):
        self.gh.ticket(6, "failed")
        self.gh.pull(60, 6, runs=(("unittest", "in_progress"),))
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(60, (REPO, 6))])
        self.assertIn({"name": "loom:pr"}, self.fixed[0][2])
        self.assertNotIn({"name": "loom:failed"}, self.fixed[0][2])

    def test_a_failed_ticket_past_the_last_rung_is_left_alone(self):
        self.gh.ticket(6, "failed")
        self.gh.comments[6] += ALL_RUNGS
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))

    def test_a_briefed_ticket_nobody_claims_moves_up(self):
        self.gh.ticket(7, "briefed", lane="codex")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.go()
        self.assertEqual((self.lane(7), self.model(7)), ("claude", "claude-sonnet-5-5"))

    def test_a_held_briefed_ticket_never_counts_as_stalled(self):
        self.gh.ticket(7, "briefed", body="Not before: 2099-01-01T00:00:00Z\nDifficulty: easy\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.go()
        self.assertEqual(self.model(7), None)

    def test_a_claimed_or_fresh_briefed_ticket_stays(self):
        self.gh.ticket(7, "briefed")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.gh.comments[7].append({"body": "Claimed for lane codex (body sha256 0123456789ab)",
                                    "created_at": iso(600), "user": ME})
        self.gh.ticket(8, "briefed")
        self.gh.events[8] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(600)}]
        self.go()
        self.assertEqual((self.model(7), self.model(8)), (None, None))

    def limited_easy(self, busy=(), available=None):
        """An easy ticket briefed on spark at a limit, with a ticket waiting
        on each lane in `busy`."""
        self.gh.ticket(7, "briefed", lane="spark", body="Model: spark/coder\nDifficulty: easy\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(60)}]
        for n, lane in enumerate(busy, 20):
            self.gh.ticket(n, "briefed", lane=lane)
        self.go(limited=["spark"], available=available)

    def test_a_private_easy_ticket_at_a_limit_moves_within_its_tier(self):
        self.limited_easy()
        self.assertEqual(self.lane(7), "opencode-go")
        self.assertEqual(self.model(7), None)  # the lane's own model runs it
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("Not a failure (lane spark is at a limit)", note)
        self.assertNotIn("loom:rung", note)
        self.assertEqual(self.moved, [])

    def test_a_limit_move_posts_no_rung_marker(self):
        self.limited_easy()
        self.assertFalse(any(factory.RUNG_RE.search(c["body"]) for c in self.gh.comments[7]))

    def test_a_private_easy_ticket_never_goes_to_a_public_only_lane(self):
        self.limited_easy(busy=("opencode-go", "opencode-go-2", "opencode-go-3", "openrouter", "workersai"))
        self.assertEqual((self.lane(7), self.model(7)), ("spark", "spark/coder"))

    def test_a_public_easy_ticket_may_go_to_zen(self):
        self.gh.private = False
        self.limited_easy(busy=("opencode-go", "opencode-go-2", "opencode-go-3", "openrouter", "workersai"))
        self.assertEqual(self.lane(7), "zen")

    def test_an_easy_ticket_never_goes_to_claude_or_anthropic(self):
        self.gh.private = False
        self.limited_easy(busy=factory.TIERS["easy"][1:])
        self.assertEqual(self.lane(7), "spark")
        self.assertEqual([c for c in self.gh.comments[7]], [])

    def test_a_lane_without_an_available_heartbeat_is_not_free(self):
        self.limited_easy(available=["openrouter"])
        self.assertEqual(self.lane(7), "openrouter")

    def test_a_medium_ticket_takes_an_anthropic_slot_only_when_codex_and_opencode_go_are_full(self):
        body = "Difficulty: medium\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="codex", body=body)
        self.gh.ticket(8, "briefed", lane="codex", body=body)
        self.go(limited=["codex"])
        self.assertEqual(sorted((self.lane(7), self.lane(8))), ["codex-2", "opencode-go"])
        self.setUp()
        self.gh.ticket(7, "briefed", lane="codex", body=body)
        for n, lane in enumerate(("codex-2", "opencode-go", "opencode-go-2", "opencode-go-3"), 20):
            self.gh.ticket(n, "briefed", lane=lane)
        self.go(limited=["codex"])
        self.assertEqual(self.lane(7), "anthropic")

    def test_a_ladder_ticket_at_a_limit_stays_on_its_rung(self):
        body = "Model: claude-opus-5-5\nDifficulty: easy\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.go(limited=["claude"])
        self.assertEqual((self.lane(7), self.model(7)), ("anthropic", "claude-opus-5-5"))

    def test_a_ticket_on_a_lane_that_is_not_limited_waits_as_before(self):
        self.gh.ticket(7, "briefed", lane="spark", body="Model: spark/coder\nDifficulty: easy\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(60)}]
        self.go(limited=["zen"])
        self.assertEqual((self.lane(7), self.model(7)), ("spark", "spark/coder"))

    def test_ladder_tickets_queued_on_a_busy_lane_spread_to_free_rungs(self):
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        for n in range(1, 6):
            self.gh.ticket(n, "briefed", lane="claude", body=body)
            self.gh.comments[n].append({"body": factory.RUNG_MARKER % 1, "user": ME})
        self.go()
        lanes = sorted(self.lane(n) for n in range(1, 6))
        # Two may wait on claude (RUNG_BUSY); the rest move to a free rung.
        self.assertEqual(lanes.count("claude"), factory.RUNG_BUSY)
        moved = [n for n in range(1, 6) if self.lane(n) != "claude"]
        self.assertTrue(all(self.model(n) == "gpt-6-sol" for n in moved))

    def test_a_first_brief_is_never_spread(self):
        for n in range(1, 6):
            self.gh.ticket(n, "briefed", lane="claude")
        self.go()
        self.assertEqual([self.lane(n) for n in range(1, 6)], ["claude"] * 5)

    def test_a_pass_stops_after_its_action_cap(self):
        for n in range(1, factory.MAX_ACTIONS + 5):
            self.gh.ticket(n, "failed")
        self.go()
        self.assertEqual(len(self.moved), factory.MAX_ACTIONS)


class Helpers(unittest.TestCase):
    def test_with_model_replaces_or_inserts_the_header(self):
        self.assertEqual(factory.with_model("Difficulty: hard\nModel: a\n\n## Why\nModel: x", "b"),
                         "Difficulty: hard\nModel: b\n\n## Why\nModel: x")
        self.assertEqual(factory.with_model("Difficulty: hard\n\n## Why", "b"),
                         "Model: b\nDifficulty: hard\n\n## Why")

    def test_a_busy_rung_spills_to_the_next_untried(self):
        busy = {"claude": 2, "codex": 2, "codex-2": 3}
        self.assertEqual(factory.next_rung(set(), {}), 1)
        self.assertEqual(factory.next_rung(set(), {"claude": 2}), 2)
        self.assertEqual(factory.next_rung(set(), busy), 3)
        self.assertEqual(factory.next_rung({1}, {"codex": 0}), 2)
        self.assertEqual(factory.next_rung({3}, {}), 1)
        self.assertEqual(factory.next_rung({1, 2, 3}, {}), 0)
        # Every untried rung busy: wait on the least loaded one.
        all_busy = dict(busy, **{lane: 2 for lane in factory.ANTHROPIC})
        self.assertEqual(factory.next_rung({1}, all_busy), 2)
        self.assertEqual(factory.next_rung(set(), dict(all_busy, claude=30)), 2)

    def test_the_tiers_match_the_lane_choice_in_muse_md(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "MUSE.md")) as fh:
            doc = fh.read()
        for difficulty, lanes in factory.TIERS.items():
            m = re.search(r"^- %s:(.*?)(?=^- |^$)" % difficulty.capitalize(), doc, re.M | re.S)
            bullet = " ".join(m.group(1).split())
            for lane in lanes:
                # A slot (anthropic-2, codex-2) is its lane in the doc.
                self.assertRegex(bullet, r"\b%s\b" % re.sub(r"-\d+$", "", lane),
                                 "%s lane %s" % (difficulty, lane))
            if difficulty == "easy":
                public = re.search(r"\bor ([\w, -]+?) on public repos only", bullet).group(1)
                for lane in factory.PUBLIC_ONLY:
                    self.assertIn(lane, public)
                self.assertFalse([x for x in lanes if x.startswith(("anthropic", "claude"))])
        self.assertFalse(set(factory.PUBLIC_ONLY) - set(factory.TIERS["easy"]))

    def test_without_model_drops_only_the_header(self):
        self.assertEqual(factory.without_model("Model: m\nDifficulty: easy\n\nModel: x"),
                         "Difficulty: easy\n\nModel: x")
        self.assertEqual(factory.without_model("Difficulty: easy\n\nx"), "Difficulty: easy\n\nx")

    def test_tried_rungs_start_again_at_a_rung_zero_marker(self):
        comments = ALL_RUNGS + [{"body": factory.RUNG_MARKER % 0}, {"body": factory.RUNG_MARKER % 2}]
        self.assertEqual(factory.tried_rungs(comments), {2})

    def test_pick_lane_takes_the_least_loaded(self):
        self.assertEqual(factory.pick_lane(("codex", "codex-2"), {"codex": 2, "codex-2": 1}), "codex-2")
        self.assertEqual(factory.pick_lane(("codex", "codex-2"), {}), "codex")

    def test_due_runs_at_most_every_check_interval(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(factory.due(d, NOW))
            self.assertFalse(factory.due(d, NOW + 10))
            self.assertFalse(factory.due(d, NOW - 10), "a clock stepped back still waits")
            self.assertTrue(factory.due(d, NOW + factory.FACTORY_CHECK + 1))
            # A stored time far in the future does not shut the factory out.
            self.assertTrue(factory.due(d, NOW - 10 * factory.FACTORY_CHECK))

    def test_check_runs_are_read_across_pages(self):
        gh = FakeGitHub()
        pages = {1: [{"name": "c%d" % i, "status": "completed"} for i in range(100)],
                 2: [{"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed"}]}

        def rest(method, path, body=None, etag=None, ok=()):
            page = int(re.search(r"page=(\d+)$", path).group(1))
            return Response(200, {"check_runs": pages.get(page, [])})
        gh.rest = rest
        f = factory.Factory(gh, [REPO], NOW, trusted=["alice-example"])
        self.assertEqual(f._review_state(REPO, 1, "sha", NOW), "reviewed")

    def test_the_station_setting_turns_it_off(self):
        self.assertFalse(station.Settings.from_env({"LOOM_FACTORY": "0"}).factory_on)
        on = station.Settings.from_env({})
        self.assertTrue(on.factory_on)
        self.assertEqual(on.review_check, factory.DEFAULT_REVIEW_CHECK)
        self.assertEqual(on.no_test_repos, [])
        self.assertEqual(station.Settings.from_env(
            {"LOOM_NO_TEST_REPOS": "a/b c/d"}).no_test_repos, ["a/b", "c/d"])


    def test_the_station_runs_the_factory_when_on_and_throttles_it(self):
        calls = []

        class Fake:
            def __init__(self, gh, repos, now, trusted, review_check, limited, review_bot=None,
                         no_test_repos=(), available=None):
                calls.append((tuple(repos), tuple(trusted), review_check, tuple(limited),
                              tuple(no_test_repos)))

            def run(self):
                calls.append("ran")

        real = factory.Factory
        factory.Factory = Fake
        try:
            with tempfile.TemporaryDirectory() as home:
                settings = station.Settings(home, [REPO, "not a repo"], ["alice-example"], [],
                                            factory_on=True, review_check="Review",
                                            no_test_repos=["example-org/no-tests"])
                st = station.Station(FakeGitHub(), settings, now=NOW)
                os.makedirs(st.state, exist_ok=True)
                st.run_factory()
                st.run_factory()
                settings.factory_on = False
                st.now += factory.FACTORY_CHECK + 1
                st.run_factory()
        finally:
            factory.Factory = real
        self.assertEqual(calls, [((REPO,), ("alice-example",), "Review", (),
                                  ("example-org/no-tests",)), "ran"])


if __name__ == "__main__":
    unittest.main()
