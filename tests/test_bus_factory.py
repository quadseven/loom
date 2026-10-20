"""The factory pass (bus/factory.py) with a fake GitHub and recorded actions."""

import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from bus import factory, labels, station, status
from bus.github import GitHubError, Response
from bus.tickets import FIX_MARKER

REPO = "example-org/example-repo"
NOW = 2_000_000_000.0
# A loom:fix comment that asked for thread PRRT_1 and a failing check.
FIX_ASKED = FIX_MARKER + "\nPull request: x#50\n\n### Check: lint\n\n    E1\n\n### Thread PRRT_1 (comment 1) at a.py:1\n\n    rename"
ME = {"login": "alice-example"}
# Every rung of the ladder tried since the last restart.
CLAIM = {"body": "Claimed for lane claude (body sha256 0123456789ab)", "user": ME}
ALL_RUNGS = [c for n in (1, 2, 3) for c in ({"body": factory.RUNG_MARKER % n, "user": ME}, CLAIM)]
STRANGER = {"login": "mallory-example"}
# Every Anthropic lane, and claude: no rung of claude-opus-5-5 is up.
NO_OPUS = ("claude",) + factory.ANTHROPIC


def lane_models(**models):
    """The lanes' configured model settings (LOOM_<LANE>_MODEL) for a test,
    and no others."""
    env = {k: v for k, v in os.environ.items() if not re.fullmatch(r"LOOM_\w+_MODEL", k)}
    env.update({"LOOM_%s_MODEL" % lane.upper(): model for lane, model in models.items()})
    return mock.patch.dict(os.environ, env, clear=True)


def footer(lane, model, number=5):
    """A lane pull request body with the runner's footer (lanes/run_lane.sh)."""
    return ("## Why\nx\n\n---\nLoom ticket example-org.example-repo-%d, lane %s, model %s.\n"
            "Refs %s#%d\n" % (number, lane, model, REPO, number))


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
        self.logs = {}
        self.requests = 0

    def ticket(self, number, state, lane="codex", body="Difficulty: medium\n\n## Why\nx\n"):
        self.issues[number] = {"number": number, "state": "open", "body": body, "user": ME,
                               "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state},
                                          {"name": "lane:" + lane}]}
        self.comments[number] = []
        self.events[number] = []

    def pull(self, number, ticket, created=3600, mergeable=True, green=True,
             resolved=True, runs=(("unittest", "completed"), (factory.DEFAULT_REVIEW_CHECK, "completed")),
             updated=3600, files=("bus/a.py", "tests/test_a.py"), labels=(),
             branch=None, state="open", merged_at=None, merged=False, user=ME, body=""):
        sha = "sha%d" % number
        self.files[number] = list(files)
        ref = branch if branch is not None else ("loom/%d-some-title" % ticket)
        self.pulls[number] = {
            "number": number, "state": state, "draft": False, "body": body, "user": user,
            "labels": [{"name": x} for x in labels],
            "head": {"ref": ref, "sha": sha,
                     "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "created_at": iso(created), "updated_at": iso(updated), "mergeable": mergeable,
            "merged_at": merged_at, "merged": merged or bool(merged_at)}
        self.runs[sha] = [{"name": n, "status": s} for n, s in runs]
        self.details[number] = {
            "isDraft": False, "mergeable": "MERGEABLE" if mergeable else "CONFLICTING",
            "headRefName": ref, "headRefOid": sha,
            "headRepository": {"nameWithOwner": REPO},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "state": "SUCCESS" if green else "FAILURE"}}}]},
            "reviewThreads": {"nodes": [{"id": "PRRT_1", "isResolved": resolved,
                                         "comments": {"nodes": []}}]}}

    # -- the GitHub client surface the factory uses --------------------
    def paginate(self, path):
        if re.search(r"/issues\?labels=loom:ticket", path):
            return [i for i in self.issues.values() if i.get("state") == "open"]
        if path.endswith("/pulls?state=open") or re.search(r"/pulls\?.*state=open", path):
            return [p for p in self.pulls.values() if p.get("state") == "open"]
        if path.endswith("/pulls?state=closed") or re.search(r"/pulls\?.*state=closed", path):
            return [p for p in self.pulls.values() if p.get("state") == "closed"]
        if path.endswith("/pulls?state=all") or re.search(r"/pulls\?.*state=all", path) or path.endswith("/pulls"):
            return list(self.pulls.values())
        if path.endswith("/issues?labels=loom:triage&state=open"):
            return list(self.filed)
        m = re.search(r"/issues/(\d+)/comments$", path)
        if m:
            # As the GitHub client does: a status comment reads as its events.
            return status.expand(list(self.comments.get(int(m.group(1)), [])), [ME["login"]])
        m = re.search(r"/issues/(\d+)/events$", path)
        if m:
            return list(self.events[int(m.group(1))])
        raise AssertionError("unexpected paginate %s" % path)

    def rest(self, method, path, body=None, etag=None, ok=()):
        if method == "GET":
            return self._get(path)
        return self._write(method, path, body)

    def _get(self, path):
        m = re.search(r"/issues/(\d+)$", path)
        if m:
            number = int(m.group(1))
            if number not in self.issues:
                return Response(404, {"message": "Not Found"})
            return Response(200, self.issues[number])
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
        m = re.search(r"/actions/jobs/(\d+)/logs$", path)
        if m:
            return Response(302, None, headers={"location": "https://logs.example.com/" + m.group(1)})
        raise AssertionError("unexpected GET %s" % path)

    def transport(self, method, url, headers, body):
        return 200, {}, self.logs[url.rsplit("/", 1)[1]].encode("utf-8")

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
                if number in self.issues:
                    self.issues[number]["labels"] = [{"name": x} for x in body["labels"]]
                else:
                    have = [x["name"] for x in self.pulls[number].get("labels", [])]
                    self.pulls[number]["labels"] = [
                        {"name": x} for x in dict.fromkeys(have + list(body["labels"]))]
            elif m.group(2) == "/comments":
                self.next_comment = getattr(self, "next_comment", 1000) + 1
                self.comments.setdefault(number, []).append(
                    {"id": self.next_comment, "body": body["body"], "created_at": iso(0), "user": ME})
            elif method == "PATCH":
                self.issues[number]["body"] = body["body"]
        m = re.search(r"/issues/comments/(\d+)$", path)
        if m and method == "PATCH":
            for found in self.comments.values():
                for comment in found:
                    if comment.get("id") == int(m.group(1)):
                        comment["body"] = body["body"]
        m = re.search(r"/pulls/(\d+)$", path)
        if m and method == "PATCH":
            self.pulls[int(m.group(1))].update(body)
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
        self.merged, self.fixed, self.moved, self.closed = [], [], [], []

    def go(self, limited=(), no_test_repos=(), available=None, model_limits=None, held=(), silent=None,
           down=()):
        def merge(gh, repo, number, ticket):
            self.merged.append((number, ticket))
        def fix(gh, repo, number, ticket, notes, lane=None, model=None):
            f.notes = notes
            if lane:
                gh.issues[ticket[1]]["labels"] = [
                    x for x in gh.issues[ticket[1]]["labels"] if not x["name"].startswith("lane:")
                ] + [{"name": "lane:" + lane}]
            self.fixed.append((number, ticket, gh.issues[ticket[1]]["labels"][:]))
        def move(gh, repo, number, state, lane=None, note=None):
            self.moved.append((number, state, lane))
        def close(gh, repo, number, comment):
            self.closed.append((number, comment))
            factory._default_close(gh, repo, number, comment)
        f = factory.Factory(self.gh, [REPO], NOW, trusted=["Alice-Example"],
                            merge=merge, fix=fix, move=move, close=close,
                            limited=limited, no_test_repos=no_test_repos,
                            available=available, model_limits=model_limits, held=held,
                            silent=silent, down=down)
        f.run()
        return f

    def lane(self, number):
        return factory._lane(self.gh.issues[number])

    def model(self, number):
        m = re.search(r"^Model: (\S+)$", self.gh.issues[number]["body"], re.M)
        return m.group(1) if m else None


class PullRequests(Base):
    def test_a_pull_request_with_unbuilt_non_live_criteria_goes_back(self):
        criteria = ["A%d" % n for n in range(1, 6)] + ["Live: observed after deploy"]
        ticket_body = ("Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n" +
                       "\n".join("- [ ] " + item for item in criteria) + "\n")
        pr_criteria = ("- [x] A1\n- [x] A2\n" +
                       "\n".join("- [ ] " + item for item in criteria[2:]) + "\n")
        self.gh.ticket(5, "pr", body=ticket_body)
        self.gh.pull(50, 5, body="## Acceptance criteria\n" + pr_criteria)
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])

    def test_a_pull_request_with_every_non_live_criterion_ticked_lands(self):
        criteria = ["A%d" % n for n in range(1, 6)] + ["Live: observed after deploy"]
        ticket_body = ("Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n" +
                       "\n".join("- [ ] " + item for item in criteria) + "\n")
        pr_criteria = ("\n".join("- [x] " + item for item in criteria[:5]) +
                       "\n- [ ] " + criteria[5] + "\n")
        self.gh.ticket(5, "pr", body=ticket_body)
        self.gh.pull(50, 5, body="## Acceptance criteria\n" + pr_criteria)
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_a_not_built_criterion_creates_a_follow_up_and_does_not_block_landing(self):
        criteria = ["A%d" % n for n in range(1, 6)] + ["Live: observed after deploy"]
        ticket_body = ("Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n" +
                       "\n".join("- [ ] " + item for item in criteria) + "\n")
        pr_criteria = ("\n".join("- [x] " + item for item in criteria[:4]) +
                       "\n- [ ] A5; not built: requires a later integration\n" +
                       "- [ ] " + criteria[5] + "\n")
        self.gh.ticket(5, "pr", body=ticket_body)
        self.gh.pull(50, 5, body="## Acceptance criteria\n" + pr_criteria)
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual(len(self.gh.filed), 1)
        self.assertIn("- [ ] A5", self.gh.filed[0]["body"])

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

    def test_a_fix_round_on_a_remote_lane_moves_to_a_local_lane(self):
        self.gh.ticket(5, "pr", lane="muse")
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertNotEqual(self.lane(5), "muse")
        self.assertEqual(self.lane(5), "spark")

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
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 1, "user": ME,
                                 "created_at": iso(3600)}, CLAIM]
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
            self.gh.comments[5].append(CLAIM)
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

    def test_a_ticket_past_opus_climbs_to_an_untried_strong_model_never_down_to_sonnet(self):
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 3, "user": ME}, CLAIM,
                                {"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual((self.lane(5), self.model(5)), ("codex", "gpt-6-sol"))
        self.assertNotIn("loom:runaway", [x["name"] for x in self.gh.issues[5]["labels"]])
        self.assertIn("rung 2, gpt-6-sol on lane codex", self.gh.comments[5][-1]["body"])

    def test_on_a_public_repo_every_ladder_rung_tried_climbs_to_gemini_argon(self):
        self.gh.private = False
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.comments[5] += ALL_RUNGS + [{"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual(self.moved, [])
        self.assertEqual((self.lane(5), self.model(5)), ("agy", "gemini-4-argon-high"))
        self.assertIn(factory.RUNG_MARKER % 4, self.gh.comments[5][-1]["body"])

    def test_a_rung_whose_model_is_limited_on_its_lanes_is_not_climbed_to(self):
        # On 2026-10-18 a ticket climbed to rung 4 while agy's limit on
        # gemini-4-argon-high had a day left; every claim there was parked.
        self.gh.private = False
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.comments[5] += ALL_RUNGS + [{"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.go(model_limits={"agy": {"gemini-4-argon-high": NOW + 3600}})
        self.assertEqual(self.moved, [])
        self.assertNotEqual(self.lane(5), "agy")
        self.assertFalse([c for c in self.gh.comments[5] if factory.RUNG_MARKER % 4 in c["body"]])

    def test_the_give_up_note_names_the_models_the_ticket_ran_on(self):
        notes = []
        self.gh.ticket(5, "pr")
        self.gh.comments[5] += ALL_RUNGS + [{"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        f = factory.Factory(self.gh, [REPO], NOW, trusted=["Alice-Example"],
                            merge=lambda *a: None, fix=lambda *a, **k: None,
                            move=lambda gh, repo, number, state, lane=None, note=None:
                            notes.append(note))
        f.run()
        self.assertEqual(len(notes), 1)
        self.assertIn("tried every rung", notes[0])
        self.assertIn("ran on claude-sonnet-5-5, gpt-6-sol, claude-opus-5-5", notes[0])

    def opus_runs(self, body="", private=False):
        """Ticket #5 on lane claude with no Model: line, five runs each with a
        fix round after it, and its pull request #50 (2026-10-17: lane claude
        ran claude-opus-5-5 five times)."""
        self.gh.private = private
        self.gh.ticket(5, "pr", lane="claude")
        for _ in range(5):
            self.gh.comments[5] += [CLAIM, {"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False, body=body)

    def test_five_runs_on_a_lane_set_to_opus_are_recorded_as_opus_and_climb_to_an_untried_model(self):
        # What ran comes from the pull request's runner footer, else from the
        # lane's configured model; never from a fixed lane-to-model map.
        for name, body, models in (("footer", footer("claude", "claude-opus-5-5"), {}),
                                   ("setting", "", {"claude": "claude-opus-5-5"})):
            with self.subTest(name), lane_models(**models):
                self.setUp()
                self.opus_runs(body)
                issue, comments = dict(self.gh.issues[5]), list(self.gh.comments[5])
                # No codex lane is up: Gemini 4 Argon high, never Opus again.
                self.go(limited=("codex", "codex-2"))
                self.assertEqual((self.lane(5), self.model(5)), ("agy", "gemini-4-argon-high"))
                self.assertEqual(self.moved, [])
                ran = factory.claim_models(issue, comments, factory.footer_run(body))
                self.assertEqual([m for c, m in ran if c], ["claude-opus-5-5"] * 5)
            with self.subTest(name + ", private"), lane_models(**models):
                self.setUp()
                self.opus_runs(body, private=True)
                self.go()
                self.assertEqual((self.lane(5), self.model(5)), ("codex", "gpt-6-sol"))

    def test_a_fork_or_stranger_footer_or_an_unknown_model_never_steers_the_ladder(self):
        opus = footer("claude", "claude-opus-5-5")
        self.assertEqual(factory.footer_run(opus, env={}), ("claude", "claude-opus-5-5"))
        self.assertIsNone(factory.footer_run(footer("claude", "claude-opus-9-9"), env={}))
        self.assertEqual(factory.footer_run(footer("spark", "spark/local-1"),
                                            env={"LOOM_SPARK_MODEL": "spark/local-1"}),
                         ("spark", "spark/local-1"))
        f = factory.Factory(self.gh, [REPO], NOW, trusted=["Alice-Example"])
        own = {"user": ME, "body": opus, "head": {"repo": {"full_name": REPO}}}
        self.assertEqual(f._footer(REPO, own), ("claude", "claude-opus-5-5"))
        self.assertIsNone(f._footer(REPO, dict(own, user=STRANGER)))
        self.assertIsNone(f._footer(REPO, dict(own, head={"repo": {"full_name": "someone-else/fork"}})))
        # Ignored, the claims name no model, and the climb starts at rung 1.
        for name, body, head in (("unknown model", footer("claude", "claude-opus-9-9"), REPO),
                                 ("fork", opus, "someone-else/fork")):
            with self.subTest(name), lane_models():
                self.setUp()
                self.opus_runs(body)
                self.gh.pulls[50]["head"]["repo"] = {"full_name": head}
                self.go(limited=("codex", "codex-2"))
                self.assertEqual((self.lane(5), self.model(5)), ("claude", "claude-sonnet-5-5"))

    def test_with_no_lane_up_for_the_next_rung_the_ticket_waits_and_nothing_is_posted(self):
        # 2026-10-17: no codex lane was free for rung 2, and the factory said it
        # tried every rung and moved the ticket to failed.
        self.gh.ticket(5, "pr", lane="claude",
                       body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 1, "user": ME}, CLAIM,
                                {"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        posted = len(self.gh.comments[5])
        self.go(limited=("codex", "codex-2") + NO_OPUS)
        self.assertEqual((self.moved, self.fixed), ([], []))
        self.assertEqual(len(self.gh.comments[5]), posted)
        self.assertFalse([w for w in self.gh.writes if "tried every rung" in str(w)])
        self.assertEqual((self.lane(5), self.model(5)), ("claude", "claude-sonnet-5-5"))
        # Once a lane of the next rung is up, it climbs there.
        self.go(limited=NO_OPUS)
        self.assertEqual((self.lane(5), self.model(5)), ("codex", "gpt-6-sol"))
        self.assertEqual(self.moved, [])

    def test_a_runaway_ticket_is_not_climbed_or_given_up_on(self):
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.issues[5]["labels"].append({"name": "loom:runaway"})
        self.gh.comments[5] += ALL_RUNGS + [{"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual((self.fixed, self.moved), ([], []))
        self.assertEqual(len(self.gh.comments[5]), len(ALL_RUNGS) + 1)

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

    def test_a_p0_tickets_pull_request_lands_before_an_older_one(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200)
        self.gh.ticket(6, "pr")
        self.gh.issues[6]["body"] = "Priority: P0\n\n" + str(self.gh.issues[6].get("body") or "")
        self.gh.pull(60, 6, created=3600)
        self.go()
        self.assertEqual(self.merged, [(60, (REPO, 6))])

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

    def test_a_revert_tickets_pull_request_lands_first_with_no_test_of_its_own(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200)
        self.gh.ticket(6, "pr")
        self.gh.issues[6]["labels"].append({"name": "loom:revert"})
        self.gh.pull(60, 6, created=3600, files=("bus/a.py",))
        self.go()
        self.assertEqual((self.merged, self.fixed), ([(60, (REPO, 6))], []))

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

    def test_a_partial_review_asks_for_improve_instead_of_a_fix_round(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "failure",
             "output": {"title": "WARN Elder review coverage partial"},
             "details_url": "https://github.com/%s/actions/runs/79/job/1" % REPO}]
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertFalse(any("rerun" in w[1] for w in self.gh.writes))
        bodies = [str(c.get("body") or "") for c in self.gh.comments[50]]
        self.assertTrue(any("/grug improve" in body for body in bodies))
        self.assertTrue(any(factory.REVIEW_AGAIN_MARKER % "sha50" in body for body in bodies))

    def test_a_second_partial_review_on_the_same_head_is_sent_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "failure",
             "output": {"title": "WARN Grug eyes clouded"},
             "details_url": "https://github.com/%s/actions/runs/80/job/1" % REPO}]
        self.go()
        self.assertEqual(self.fixed, [])
        improve = [c for c in self.gh.comments[50]
                   if "/grug improve" in str(c.get("body") or "")]
        self.assertEqual(len(improve), 1)
        # The same head, still partial: back to the usual fix round.
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(len([c for c in self.gh.comments[50]
                              if "/grug improve" in str(c.get("body") or "")]), 1)

    def test_pull_requests_of_tickets_not_in_pr_are_left_alone(self):
        self.gh.ticket(5, "briefed")
        self.gh.pull(50, 5)
        self.gh.pulls[51] = dict(self.gh.pulls[50], number=51, head={"ref": "feature/x"})
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_older_lane_pull_request_closed_when_newer_lane_pr_exists(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200, updated=7200, branch="loom/5-some-title")
        self.gh.pull(52, 5, created=3600, updated=3600, branch="loom/5-some-title-r2")
        self.go()
        self.assertEqual(self.closed, [(50, "Superseded by #52.")])
        self.assertEqual(self.gh.pulls[50]["state"], "closed")
        self.assertEqual(self.gh.comments[50][-1]["body"], "Superseded by #52.")
        self.assertEqual(self.merged, [(52, (REPO, 5))])

    def test_lane_pull_request_closed_when_ticket_merged_through_different_pr(self):
        self.gh.ticket(5, "merged")
        self.gh.pull(51, 5, created=7200, updated=7200, branch="loom/5-some-title-r2",
                     state="closed", merged_at=iso(100), merged=True)
        self.gh.pull(50, 5, created=9000, updated=9000, branch="loom/5-some-title")
        self.go()
        self.assertEqual(self.closed, [(50, "Superseded by #51.")])
        self.assertEqual(self.gh.pulls[50]["state"], "closed")
        self.assertEqual(self.gh.comments[50][-1]["body"], "Superseded by #51.")
        self.assertEqual(self.merged, [])

    def test_old_merge_does_not_supersede_regression_pull_request(self):
        self.gh.ticket(5, "merged")
        self.gh.issues[5]["labels"].append({"name": "loom:regression"})
        self.gh.events[5].append({"event": "labeled", "label": {"name": "loom:briefed"},
                                  "created_at": iso(60)})
        self.gh.pull(51, 5, state="closed", merged_at=iso(120), merged=True)
        self.gh.pull(50, 5, created=30, updated=30, branch="loom/5-some-title-r2")
        self.go()
        self.assertEqual(self.closed, [])
        self.assertEqual(self.gh.pulls[50]["state"], "open")

    def test_missing_merged_pull_details_leave_regression_pull_open(self):
        self.gh.ticket(5, "merged")
        self.gh.issues[5]["labels"].append({"name": "loom:regression"})
        self.gh.pull(51, 5, state="closed", merged_at=iso(120), merged=True)
        self.gh.pull(50, 5, branch="loom/5-some-title-r2")
        original_get = self.gh._get

        def get_without_old_pull(path):
            if path.endswith("/pulls/51"):
                return Response(200, None)
            return original_get(path)

        with mock.patch.object(self.gh, "_get", side_effect=get_without_old_pull):
            self.go()
        self.assertEqual(self.closed, [])
        self.assertEqual(self.gh.pulls[50]["state"], "open")

    def test_merge_after_regression_requeue_supersedes_duplicate(self):
        self.gh.ticket(5, "merged")
        self.gh.issues[5]["labels"].append({"name": "loom:regression"})
        self.gh.events[5].append({"event": "labeled", "label": {"name": "loom:briefed"},
                                  "created_at": iso(120)})
        self.gh.pull(51, 5, state="closed", merged_at=iso(60), merged=True)
        self.gh.pull(50, 5, created=30, updated=30, branch="loom/5-some-title-r2")
        self.go()
        self.assertEqual(self.closed, [(50, "Superseded by #51.")])

    def test_lane_pull_request_closed_when_its_closed_ticket_shipped_elsewhere(self):
        self.gh.ticket(5, "shipped")
        self.gh.issues[5]["state"] = "closed"
        self.gh.pull(51, 5, created=7200, updated=7200, branch="loom/5-some-title-r2",
                     state="closed", merged_at=iso(100), merged=True)
        self.gh.pull(50, 5, created=9000, updated=9000, branch="loom/5-some-title")
        self.go()
        self.assertEqual(self.closed, [(50, "Superseded by #51.")])
        self.assertEqual(self.gh.pulls[50]["state"], "closed")

    def test_non_loom_branch_pull_request_is_left_open(self):
        self.gh.ticket(5, "merged")
        self.gh.pull(51, 5, created=7200, updated=7200, branch="loom/5-some-title-r2",
                     state="closed", merged_at=iso(100), merged=True)
        self.gh.pull(60, 5, created=9000, updated=9000, branch="feature/my-branch",
                     body="Refs %s#5\n" % REPO)
        self.gh.pull(70, 5, created=9000, updated=9000, branch="loom/5-some-title",
                     user=STRANGER)
        self.go()
        self.assertEqual(self.closed, [])
        self.assertEqual(self.gh.pulls[60]["state"], "open")
        self.assertEqual(self.gh.pulls[70]["state"], "open")

    def test_failed_close_does_not_stop_the_rest_of_the_pass(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200, updated=7200, branch="loom/5-some-title")
        self.gh.pull(52, 5, created=3600, updated=3600, branch="loom/5-some-title-r2")
        real_rest = self.gh.rest

        def rest(method, path, *args, **kwargs):
            if method == "PATCH" and path.endswith("/pulls/50"):
                raise GitHubError("boom", 500)
            return real_rest(method, path, *args, **kwargs)
        with mock.patch.object(self.gh, "rest", side_effect=rest):
            self.go()
        self.assertEqual(self.gh.pulls[50]["state"], "open")
        self.assertEqual(len(self.merged), 1)


LEAK_JOB = "leak-scan / Scan diff and PR/issue text for private identifiers"
LEAK_BODY = ("Implements the ticket.\n\nThe shape follows example-org/other-repo#12 closely.\n"
             "It ran on build-box.example.internal.\n\nRefs %s#5\n" % REPO)
CLEAN = "leak guard: clean (9 shape patterns, deny-list not configured, %d lines scanned)"
HIT = "  %s:%d  [%s] %s - why"
TEXT = "/home/runner/work/_temp/text.md"


def leak_log(diff_hits=(), text_hits=()):
    """A leak-scan job log as GitHub serves it: stamped lines, two sections."""
    lines = ["##[group]Run set -euo pipefail", "##[endgroup]", "--- PR diff ---"]
    lines += ["BLOCKED: private infrastructure identifiers found", ""] if diff_hits else [CLEAN % 40]
    lines += [HIT % ("abc...def", n, rule, "'x'") for n, rule in diff_hits]
    lines.append("--- title and body ---")
    lines += ["BLOCKED: private infrastructure identifiers found", ""] if text_hits else [CLEAN % 8]
    lines += [HIT % (TEXT, n, rule, "'x'") for n, rule in text_hits]
    lines += ["", "This repo is PUBLIC.", "##[error]leak scan found private identifiers",
              "##[error]Process completed with exit code 1."]
    return "\n".join("2026-10-13T12:00:%02d.1234567Z %s" % (i % 60, l) for i, l in enumerate(lines))


BODY_ONLY = leak_log(text_hits=[(4, "private-issue-ref"), (5, "private-hostname")])


class LeakScanText(Base):
    """A leak scan that fails on the pull request's text alone is fixed in the
    text by the factory; anything else still goes to the lane."""

    def leaky(self, log, comments=()):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].extend(comments)
        self.gh.pull(50, 5, green=False)
        self.gh.pulls[50].update(title="Ticket title", body=LEAK_BODY)
        self.gh.runs["sha50"] = [
            {"id": 901, "name": LEAK_JOB, "status": "completed", "conclusion": "failure",
             "app": {"slug": "github-actions"},
             "details_url": "https://github.com/%s/actions/runs/77/job/901" % REPO},
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "success"}]
        self.gh.logs["901"] = log
        self.gh.attempts["77"] = 2

    def pr_edits(self):
        return [w for w in self.gh.writes if w[0] == "PATCH" and w[1].endswith("/pulls/50")]

    def test_a_body_only_failure_edits_the_body_and_sends_nothing_to_the_lane(self):
        self.leaky(BODY_ONLY)
        self.gh.attempts["77"] = 1
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertFalse(any("rerun" in w[1] for w in self.gh.writes))
        self.assertEqual(len(self.pr_edits()), 1)
        self.assertEqual(self.gh.pulls[50]["body"],
                         "Implements the ticket.\n\nThe shape follows issue 12 of another repo "
                         "closely.\n\nRefs %s#5" % REPO)
        self.assertEqual(self.gh.pulls[50]["title"], "Ticket title")
        note = self.gh.comments[5][-1]["body"]
        self.assertIn(factory.BODY_LEAK_MARKER % "sha50", note)
        self.assertIn("body line 3: put a cross-repo issue reference in words", note)
        self.assertIn("body line 4: dropped (flagged as private-hostname)", note)
        # The comment names lines, never the flagged text.
        self.assertNotIn("other-repo", note)
        self.assertNotIn("build-box", note)

    def test_a_diff_failure_still_goes_back_to_the_lane(self):
        self.leaky(leak_log(diff_hits=[(10, "private-issue-ref")],
                            text_hits=[(4, "private-issue-ref")]))
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.pr_edits(), [])

    def test_a_second_failure_on_the_same_head_is_not_edited_again(self):
        self.leaky(BODY_ONLY)
        self.go()
        self.assertEqual(len(self.pr_edits()), 1)
        self.gh.pulls[50]["body"] = LEAK_BODY
        self.go()
        self.assertEqual(len(self.pr_edits()), 1)
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_another_failing_check_goes_back_to_the_lane(self):
        self.leaky(BODY_ONLY)
        self.gh.runs["sha50"].append(
            {"id": 902, "name": "unittest", "status": "completed", "conclusion": "failure",
             "app": {"slug": "github-actions"},
             "details_url": "https://github.com/%s/actions/runs/77/job/902" % REPO})
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.pr_edits(), [])

    def test_a_flagged_title_reference_is_put_in_words(self):
        fixed = factory.repair_leak_text("Port example-org/other-repo#3", "a\nb",
                                         {1: "private-issue-ref"}, REPO)
        self.assertEqual(fixed, ("Port issue 3 of another repo", "a\nb",
                                 ["title: put a cross-repo issue reference in words"]))
        # A title cannot be dropped, nor a line past the end found.
        self.assertIsNone(factory.repair_leak_text("t", "a", {1: "private-hostname"}, REPO))
        self.assertIsNone(factory.repair_leak_text("t", "a", {3: "private-hostname"}, REPO))

    def test_the_same_repos_reference_reads_as_this_repo(self):
        self.assertEqual(factory.refs_in_words("see %s#4 and example-repo#5 and ab/cd#6" % REPO, REPO),
                         "see issue 4 of this repo and issue 5 of this repo and issue 6 of another repo")

    def test_a_log_that_is_not_a_leak_scan_is_not_read_as_one(self):
        self.assertIsNone(factory.body_leak_hits(["FAILED (errors=1)"]))
        self.assertIsNone(factory.body_leak_hits(leak_log().split("\n")))


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
                         ("Settle the review findings left from PR #50", ["loom:triage"]))
        self.assertIn("- medium | _robustness_ | `missing-error-handling` | quick win in `bus/a.py`:"
                      " https://example.invalid/pr/50#r1", filed["body"])
        self.assertTrue(all(ord(ch) < 128 for ch in filed["body"]))
        replies = [w for w in self.gh.writes if w[0] == "REPLY"]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0][2].startswith("Deferred to #900: "))
        self.assertIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual((self.merged, self.fixed), ([(50, (REPO, 5))], []))

    def test_a_new_medium_finding_on_the_first_round_is_deferred_not_sent_back(self):
        self.finding(MEDIUM, asked=(ASKED_OTHER,))
        self.go()
        self.assertEqual((len(self.gh.filed), self.merged, self.fixed),
                         (1, [(50, (REPO, 5))], []))

    def test_a_high_finding_on_a_logic_pull_request_still_goes_back(self):
        self.gh.pulls[50]["labels"] = [{"name": "class:logic"}]
        self.finding(HIGH, asked=())
        self.go()
        self.assertEqual((self.gh.filed, self.merged, len(self.fixed)), ([], [], 1))

    def test_a_revert_defers_a_medium_finding_without_a_fix_round(self):
        self.gh.issues[5]["labels"].append({"name": "loom:revert"})
        self.finding(MEDIUM, asked=())
        self.go()
        self.assertEqual((len(self.gh.filed), self.merged, self.fixed),
                         (1, [(50, (REPO, 5))], []))

    def test_a_revert_still_goes_back_for_a_high_finding(self):
        self.gh.issues[5]["labels"].append({"name": "loom:revert"})
        self.finding(HIGH, asked=())
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

    def test_an_open_follow_up_under_the_new_title_is_reused(self):
        self.gh.filed.append({"number": 778, "title": "Settle the review findings left from PR #50",
                              "user": ME})
        self.finding(MEDIUM)
        self.go()
        self.assertEqual(len(self.gh.filed), 1)
        self.assertTrue([w for w in self.gh.writes if w[0] == "REPLY"][0][2].startswith("Deferred to #778: "))

    def test_an_open_follow_up_from_an_earlier_pass_is_reused(self):
        self.gh.filed.append({"number": 777, "title": "Deferred review findings from %s#50" % REPO,
                              "user": ME})
        self.finding(MEDIUM)
        self.go()
        self.assertEqual(len(self.gh.filed), 1)
        self.assertTrue([w for w in self.gh.writes if w[0] == "REPLY"][0][2].startswith("Deferred to #777: "))
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_a_docs_pull_request_defers_a_medium_finding_at_once(self):
        self.gh.pulls[50]["labels"] = [{"name": "class:docs"}]
        self.finding(MEDIUM, asked=())
        self.go()
        self.assertEqual((len(self.gh.filed), self.merged), (1, [(50, (REPO, 5))]))
        self.assertEqual(self.fixed, [])

    def test_a_tests_pull_request_defers_a_low_finding_at_once(self):
        self.gh.pulls[50]["labels"] = [{"name": "class:tests"}]
        self.finding("low | _style_ | `nit`", asked=())
        self.go()
        self.assertEqual((len(self.gh.filed), self.merged), (1, [(50, (REPO, 5))]))

    def test_a_logic_pull_request_defers_a_minor_finding_without_a_fix_round(self):
        # 2026-10-14: medium findings no longer cost a fix round on any class.
        self.gh.pulls[50]["labels"] = [{"name": "class:logic"}]
        self.finding(MEDIUM, asked=())
        self.go()
        self.assertEqual((len(self.gh.filed), self.merged, self.fixed),
                         (1, [(50, (REPO, 5))], []))


class ChangeClasses(Base):
    def test_a_sensitive_pull_request_is_left_for_the_brain_and_marked(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=("class:sensitive",))
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))
        self.assertIn(("POST", "/repos/%s/issues/50/labels" % REPO, {"labels": ["needs-brain"]}),
                      self.gh.writes)
        names = [x["name"] for x in self.gh.pulls[50]["labels"]]
        self.assertIn("class:sensitive", names)
        self.assertIn("needs-brain", names)

    def test_a_sensitive_pull_request_with_a_conflict_goes_back_to_its_lane(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=False, labels=("class:sensitive", "needs-brain"))
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_a_sensitive_pull_request_with_failing_checks_goes_back_to_its_lane(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False, labels=("class:sensitive", "needs-brain"))
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual(len(self.fixed), 1)

    def test_a_sensitive_pull_request_already_marked_is_left_alone(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=("class:sensitive", "needs-brain"))
        self.go()
        self.assertEqual([w for w in self.gh.writes if w[1].endswith("/labels")], [])
        self.assertEqual(self.merged, [])


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


class Queued(Base):
    def queued(self, number, body="Difficulty: medium\n\n## Why\nx\n", age=0):
        self.gh.ticket(number, "queued", body=body)
        self.gh.issues[number]["labels"] = [
            {"name": "loom:ticket"}, {"name": "loom:queued"}]
        self.gh.issues[number]["created_at"] = iso(age)

    def assign(self, limited=(), available=None):
        worker = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"],
                                 limited=limited, available=available)
        worker.assign_queued()
        return worker

    def test_queued_ladder_model_uses_a_compatible_lane(self):
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        self.queued(7, body=body)
        self.assign(available=["opencode-go-3", "claude"])
        self.assertEqual(self.lane(7), "claude")
        self.assertEqual(self.gh.issues[7]["body"], body)

    def test_a_ticket_held_for_a_factory_incident_does_not_fill_its_lane(self):
        # 2026-10-18: tickets held for open factory incidents kept their
        # running labels, so their idle lanes counted as busy for hours and
        # every queued ticket waited for a free lane.
        self.gh.ticket(6, "running", lane="claude")
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        self.queued(7, body=body)
        worker = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"],
                                 available=["claude"], held=["%s#6" % REPO])
        worker.assign_queued()
        self.assertEqual(self.lane(7), "claude")

    def test_a_running_ticket_not_held_still_fills_its_lane(self):
        self.gh.ticket(6, "running", lane="claude")
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        self.queued(7, body=body)
        self.assign(available=["claude"])
        self.assertIn("loom:queued", [x["name"] for x in self.gh.issues[7]["labels"]])

    def test_queued_ladder_model_waits_when_only_incompatible_lanes_are_free(self):
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        self.queued(7, body=body)
        self.assign(available=["opencode-go-3"])
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")
        self.assertEqual(self.gh.issues[7]["body"], body)

    def test_private_medium_uses_the_least_loaded_available_tier_lane(self):
        self.queued(7)
        with mock.patch("bus.factory._say") as say:
            self.assign(available=["codex-2", "opencode-go-4"])
        self.assertEqual(factory._state(self.gh.issues[7]), "briefed")
        self.assertEqual(self.lane(7), "codex-2")
        self.assertIn("lane codex-2", self.gh.comments[7][-1]["body"])
        say.assert_called_once_with("gave queued %s#7 lane codex-2 (tier medium)" % REPO)

    def test_busy_briefed_lane_is_passed_over(self):
        self.gh.ticket(6, "briefed", lane="codex")
        self.queued(7)
        self.assign(available=["codex", "codex-2"])
        self.assertEqual(self.lane(7), "codex-2")

    OPUS = "Model: claude-opus-5-5\nDifficulty: hard\n\n## Why\nx\n"
    OUT = factory.ANTHROPIC + ("claude",)

    def test_opus_pin_goes_to_agy_with_argon_when_every_anthropic_lane_is_out(self):
        self.gh.private = False
        self.queued(7, body=self.OPUS)
        self.gh.comments[7] = [{"body": factory.RUNG_MARKER % 3, "user": ME}]
        self.assign(limited=self.OUT)
        self.assertEqual((factory._state(self.gh.issues[7]), self.lane(7), self.model(7)),
                         ("briefed", "agy", "gemini-4-argon-high"))
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("equivalent: Gemini 4 Argon high, index 53, while every Anthropic "
                      "lane is out", note)
        # The claim after the move ran Argon, and the opus rung stays untried.
        comments = self.gh.comments[7] + [CLAIM]
        self.assertEqual(factory.ran_models(self.gh.issues[7], comments),
                         {"gemini-4-argon-high"})
        self.assertEqual(factory.tried_rungs(comments), set())

    def test_opus_pin_on_a_private_repo_waits_when_every_anthropic_lane_is_out(self):
        self.gh.private = True
        self.queued(7, body=self.OPUS)
        self.assign(limited=self.OUT)
        self.assertEqual((factory._state(self.gh.issues[7]), self.model(7)),
                         ("queued", "claude-opus-5-5"))

    def test_opus_pin_waits_when_anthropic_lanes_are_only_busy(self):
        self.gh.private = False
        for n, lane in enumerate(self.OUT, 20):
            self.gh.ticket(n, "briefed", lane=lane)
        self.gh.ticket(30, "briefed", lane="agy")
        self.queued(7, body=self.OPUS)
        self.assign()
        self.assertEqual((factory._state(self.gh.issues[7]), self.model(7)),
                         ("queued", "claude-opus-5-5"))
        self.gh.issues[30]["labels"][1] = {"name": "loom:merged"}
        self.assign()
        self.assertEqual((self.lane(7), self.model(7)), ("agy", "claude-opus-5-5"))

    def test_future_not_before_stays_queued(self):
        self.queued(7, body="Not before: 2099-01-01T00:00:00Z\nDifficulty: medium\n\n## Why\nx\n")
        self.assign(available=["codex"])
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")

    def test_missing_difficulty_stays_queued(self):
        self.queued(7, body="## Why\nx\n")
        self.assign(available=["codex"])
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")

    def test_oldest_takes_the_free_lane_first(self):
        self.queued(7, age=60)
        self.queued(8, age=600)
        self.assign(available=["codex"])
        self.assertEqual(factory._state(self.gh.issues[8]), "briefed")
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")

    def test_a_p0_queued_ticket_takes_the_free_lane_before_an_older_one(self):
        self.queued(7, age=600)
        self.queued(8, body="Difficulty: medium\nPriority: P0\n\n## Why\nx\n", age=60)
        self.assign(available=["codex"])
        self.assertEqual(factory._state(self.gh.issues[8]), "briefed")
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")

    def test_limited_and_unavailable_lanes_are_passed_over(self):
        self.queued(7)
        self.assign(limited=["codex"], available=["codex", "codex-2"])
        self.assertEqual(self.lane(7), "codex-2")

    def test_a_queued_ticket_of_the_opencode_go_tier_takes_that_lane(self):
        # 2026-10-20: the opencode-go lane was held by a limit for days while
        # the tickets of its tier queued; once the lane comes back it takes
        # that queued work.
        self.queued(7)
        self.assign(available=["opencode-go"])
        self.assertEqual(self.lane(7), "opencode-go")

    def test_a_pass_that_assigns_no_queued_ticket_says_why_for_the_first(self):
        self.queued(7, body="Model: claude-opus-5-5\nDifficulty: hard\n\n## Why\nx\n", age=600)
        self.queued(8, age=60)
        with mock.patch("bus.factory._say") as say:
            self.assign(available=[])
        say.assert_called_with("assigned 0 of 2 queued: %s#7 waits: no free lane takes "
                               "model claude-opus-5-5" % REPO)

    def test_a_queued_ticket_with_no_free_tier_lane_names_its_tier(self):
        self.queued(7)
        with mock.patch("bus.factory._say") as say:
            self.assign(available=[])
        say.assert_called_once_with("assigned 0 of 1 queued: %s#7 waits: no free lane "
                                    "in tier medium" % REPO)

    def test_a_pass_that_assigns_a_queued_ticket_says_no_count(self):
        self.queued(7, age=600)
        self.queued(8, age=60)
        with mock.patch("bus.factory._say") as say:
            self.assign(available=["codex"])
        self.assertFalse([c for c in say.call_args_list if "assigned 0 of" in c.args[0]])


    def test_a_queued_medium_ticket_whose_tier_is_at_a_limit_falls_back(self):
        self.queued(7)
        with mock.patch("bus.factory._say") as say:
            self.assign(limited=factory.TIERS["medium"])
        self.assertEqual(self.lane(7), "claude")
        self.assertIn("(tier medium, fallback: no medium lane is up)", self.gh.comments[7][-1]["body"])
        say.assert_called_once_with("gave queued %s#7 lane claude "
                                    "(tier medium, fallback: no medium lane is up)" % REPO)
        self.queued(8)
        self.assign(limited=factory.TIERS["medium"])
        # Every easy lane is a medium lane too since the free lanes take
        # medium work (2026-10-19), so with claude taken there is no fallback.
        self.assertEqual(factory._state(self.gh.issues[8]), "queued")


class Tickets(Base):
    def test_a_failed_ticket_without_a_pull_request_is_rebriefed_a_rung_up(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude")])
        self.assertEqual(self.model(6), "claude-sonnet-5-5")

    def test_a_failed_medium_ticket_skips_unavailable_anthropic_lanes(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.go(available=["codex", "codex-2", "opencode-go"])
        self.assertIn(self.lane(6), ("codex", "codex-2", "opencode-go"))
        self.assertNotIn("anthropic", self.lane(6))

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

    def no_reason_failure(self, number):
        return {"body": "Lane zai failed: [work] agent made no commit and wrote no reason "
                        "on loom/%d-x" % number, "user": ME}

    # Its claim on lane zai, which runs no ladder model: a claim on lane claude
    # would count rung 1 as tried (factory.ran_rungs).
    ZAI_CLAIM = {"body": "Claimed for lane zai (body sha256 0123456789ab)", "user": ME}

    def test_a_first_no_reason_no_commit_failure_climbs(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.gh.comments[6] += [self.ZAI_CLAIM, self.no_reason_failure(6)]
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude")])

    def test_a_second_no_reason_no_commit_failure_is_held_for_the_brain(self):
        self.gh.ticket(6, "failed", lane="claude")
        self.gh.comments[6] += [CLAIM, self.no_reason_failure(6),
                                {"body": factory.RUNG_MARKER % 1, "user": ME}, CLAIM,
                                self.no_reason_failure(6)]
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))
        self.assertIn({"name": "needs-brain"}, self.gh.issues[6]["labels"])
        self.assertIn("does not climb", self.gh.comments[6][-1]["body"])
        self.assertNotIn("loom:rung", self.gh.comments[6][-1]["body"])
        posted = len(self.gh.comments[6])
        self.go()  # once: the next pass says nothing more
        self.assertEqual((self.moved, len(self.gh.comments[6])), ([], posted))

    def test_a_no_reason_failure_after_a_ladder_restart_climbs_again(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.gh.comments[6] += [self.no_reason_failure(6), {"body": factory.RUNG_MARKER % 0, "user": ME},
                                self.ZAI_CLAIM, self.no_reason_failure(6)]
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude")])

    def test_a_stranger_cannot_hold_a_ticket_with_a_no_reason_comment(self):
        self.gh.ticket(6, "failed", lane="zai")
        forged = dict(self.no_reason_failure(6), user=STRANGER)
        self.gh.comments[6] += [forged, self.ZAI_CLAIM, self.no_reason_failure(6)]
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude")])

    def test_a_failed_ticket_past_the_last_rung_is_left_alone(self):
        self.gh.ticket(6, "failed")
        self.gh.comments[6] += ALL_RUNGS
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))

    def stalled(self, number, lane="codex", body="Difficulty: medium\n\n## Why\nx\n"):
        self.gh.ticket(number, "briefed", lane=lane, body=body)
        self.gh.events[number] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                                   "created_at": iso(factory.BRIEFED_STALL + 600)}]

    def test_a_briefed_ticket_nobody_claims_moves_within_its_tier(self):
        self.stalled(7)
        self.go()
        self.assertEqual((self.lane(7), self.model(7)), ("spark", None))
        self.assertNotIn("loom:rung", "".join(c["body"] for c in self.gh.comments[7]))

    def test_a_stalled_rung_two_ticket_moves_to_the_other_rung_two_lane(self):
        self.stalled(7, body="Model: gpt-6-sol\nDifficulty: medium\n\n## Why\nx\n")
        self.go()
        self.assertEqual((self.lane(7), self.model(7)), ("codex-2", "gpt-6-sol"))
        self.assertNotIn("loom:rung", "".join(c["body"] for c in self.gh.comments[7]))

    def test_three_stalls_in_a_row_never_give_up(self):
        self.stalled(7)
        for _ in range(3):
            self.go()
            self.assertEqual(self.moved, [])
            self.assertNotIn("loom:rung", "".join(c["body"] for c in self.gh.comments[7]))
        self.assertEqual(self.gh.issues[7]["body"].count("Model:"), 0)

    def test_a_moved_stalled_ticket_waits_a_full_stall_on_its_new_lane(self):
        self.stalled(7)
        self.go()
        self.assertEqual(self.lane(7), "spark")
        self.gh.events[7].append({"event": "labeled", "label": {"name": "lane:spark"},
                                  "created_at": iso(0)})
        self.go()
        self.assertEqual((self.lane(7), len(self.gh.comments[7])), ("spark", 1))

    def test_a_moved_stalled_ticket_moves_again_after_another_stall(self):
        self.stalled(7)
        self.gh.events[7].append({"event": "labeled", "label": {"name": "lane:codex"},
                                  "created_at": iso(factory.BRIEFED_STALL + 60)})
        self.go()
        self.assertEqual(self.lane(7), "spark")

    def test_routine_moves_edit_the_tickets_one_status_comment(self):
        # loom#866: zippie#264 carried 77 bot comments, one email each. Two
        # stall moves are two events on one comment, read back as two.
        self.stalled(7)
        self.go()
        self.assertEqual(self.lane(7), "spark")
        self.gh.events[7].append({"event": "labeled", "label": {"name": "lane:spark"},
                                  "created_at": iso(factory.BRIEFED_STALL + 60)})
        # A full stall passes on the new lane: the first move's event ages too.
        sticky = self.gh.comments[7][0]
        sticky["body"] = status.render([(iso(factory.BRIEFED_STALL + 60), text)
                                        for _, text in status.parse(sticky["body"])])
        self.go()
        self.assertEqual(len(self.gh.comments[7]), 1)
        self.assertTrue(status.is_status(self.gh.comments[7][0]["body"]))
        moves = [c["body"] for c in self.gh.paginate("/repos/%s/issues/7/comments" % REPO)
                 if factory.STALL_MOVE_RE.match(c["body"])]
        self.assertEqual(len(moves), 2)

    def test_a_stall_with_no_available_lane_stays_briefed(self):
        self.stalled(7)
        self.go(available=["codex"])
        self.assertEqual((self.lane(7), self.moved, self.gh.comments[7]), ("codex", [], []))

    def test_a_ticket_the_station_holds_is_never_moved_as_stalled(self):
        # 2026-10-18: loom#633, refused every pass by the unproven-work gate,
        # was moved between lanes with a comment each time.
        self.stalled(7)
        self.go(held=["%s#7" % REPO])
        self.assertEqual((self.lane(7), self.moved, self.gh.comments[7]), ("codex", [], []))

    def test_a_held_ladder_ticket_is_not_spread_off_a_busy_lane(self):
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        for n in (6, 8, 9):
            self.gh.ticket(n, "briefed", lane="claude", body=body)
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.gh.comments[7] += [{"body": factory.RUNG_MARKER % 1, "user": ME,
                                 "created_at": iso(600)}]
        self.go(held=["%s#7" % REPO])
        self.assertEqual(self.lane(7), "claude")
        self.assertEqual(self.gh.comments[7][1:], [])

    def test_a_held_briefed_ticket_never_counts_as_stalled(self):
        self.gh.ticket(7, "briefed", body="Not before: 2099-01-01T00:00:00Z\nDifficulty: easy\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.go()
        self.assertEqual(self.model(7), None)

    def waiting(self, number=7, age=None, dependency="pr"):
        """A briefed ticket that waits on ticket 5, briefed `age` seconds ago."""
        self.gh.ticket(5, dependency)
        self.gh.ticket(number, "briefed", body="After: #5\nDifficulty: easy\n\n## Why\nx\n")
        self.gh.events[number] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                                   "created_at": iso(factory.BRIEFED_STALL + 600
                                                     if age is None else age)}]
        return number

    def test_a_ticket_waiting_on_another_never_counts_as_stalled(self):
        self.waiting()
        self.go()
        self.assertEqual((self.model(7), self.lane(7)), (None, "codex"))
        self.assertEqual(self.moved, [])
        self.assertEqual(self.gh.comments[7], [])

    def test_a_hold_of_more_than_two_days_is_reported_to_the_brain_once(self):
        self.waiting(age=factory.HOLD_STUCK + 600)
        self.go()
        said = [c["body"] for c in self.gh.comments[7]]
        self.assertEqual(len(said), 1, said)
        self.assertIn("Held 48 hours by After: waiting for #5", said[0])
        self.assertIn("needs-brain", [x["name"] for x in self.gh.issues[7]["labels"]])
        self.go()
        self.assertEqual(len(self.gh.comments[7]), 1)

    def test_a_hold_the_brain_already_heard_about_is_left_alone(self):
        self.waiting(age=factory.HOLD_STUCK + 600)
        self.gh.issues[7]["labels"].append({"name": "needs-brain"})
        self.go()
        self.assertEqual(self.gh.comments[7], [])

    def test_a_merged_dependency_is_no_hold_and_the_ticket_stalls_as_usual(self):
        self.waiting(dependency="merged")
        self.go()
        # Stalled with no hold: it moves to another easy lane, never up the ladder.
        self.assertNotEqual(self.lane(7), "codex")
        self.assertIsNone(self.model(7))
        self.assertNotIn("loom:rung", "".join(c["body"] for c in self.gh.comments[7]))

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

    def limited_easy(self, busy=(), available=None, limited=factory.SPARKS):
        """An easy ticket briefed on spark at a limit (both local coder slots
        by default), with a ticket waiting on each lane in `busy`."""
        self.gh.ticket(7, "briefed", lane="spark", body="Model: spark/coder\nDifficulty: easy\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(60)}]
        for n, lane in enumerate(busy, 20):
            self.gh.ticket(n, "briefed", lane=lane)
        self.go(limited=list(limited), available=available)

    def test_a_private_easy_ticket_at_a_limit_moves_within_its_tier(self):
        self.limited_easy()
        # The first free provider slot allowed private code (infra#5617).
        self.assertEqual(self.lane(7), "free-ollama")
        self.assertEqual(self.model(7), None)  # the lane's own model runs it
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("Not a failure (lane spark is at a limit)", note)
        self.assertNotIn("loom:rung", note)
        self.assertEqual(self.moved, [])

    def test_a_ticket_briefed_on_a_down_lane_steps_aside_at_once(self):
        # A lane whose key expired never claims its briefed tickets; they
        # waited the two-hour stall before (2026-10-19).
        self.gh.ticket(7, "briefed", lane="opencode-go-2", body="Difficulty: medium\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(60)}]
        with mock.patch("bus.factory._say") as say:
            self.go(down=["opencode-go-2"],
                    available=[x for x in factory.TIERS["medium"] if x != "opencode-go-2"])
        self.assertNotEqual(self.lane(7), "opencode-go-2")
        self.assertIn("Not a failure (lane opencode-go-2 is down)", self.gh.comments[7][-1]["body"])
        self.assertTrue(any("off lane opencode-go-2, down, to" in c.args[0]
                            for c in say.call_args_list), say.call_args_list)
        self.assertEqual(self.moved, [])

    def test_a_limit_move_posts_no_rung_marker(self):
        self.limited_easy()
        self.assertFalse(any(factory.RUNG_RE.search(c["body"]) for c in self.gh.comments[7]))

    def test_a_private_easy_ticket_never_goes_to_a_public_only_lane(self):
        self.limited_easy(busy=("opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4", "openrouter",
                                "workersai", "free-ollama", "free-gemma"))
        self.assertEqual((self.lane(7), self.model(7)), ("spark", "spark/coder"))

    def test_a_public_easy_ticket_may_go_to_the_muse_spark_slot_and_a_private_one_never(self):
        self.gh.private = False
        self.limited_easy(busy=("muse", "agy", "opencode-go", "opencode-go-2", "opencode-go-3")
                          + factory.FREE_POOL)
        self.assertEqual(self.lane(7), "opencode-go-4")
        self.assertIn("opencode-go-4", factory.PUBLIC_ONLY)

    def test_a_private_easy_ticket_goes_to_spark_2_when_spark_is_busy(self):
        self.assertEqual(factory.lanes_of("easy", True)[:4], factory.SPARKS)
        self.gh.private = True
        self.limited_easy(limited=("spark",))
        self.assertEqual(self.lane(7), "spark-2")

    def test_the_sparks_take_every_tier_first_then_muse_and_agy(self):
        # The operator's priority (2026-10-17): the Sparks first in every tier;
        # then Muse's and agy's allowances on public repos (2026-10-14). A
        # private repo's ticket never reaches Muse or agy.
        for tier in ("easy", "medium", "hard"):
            self.assertEqual(factory.TIERS[tier][:4], factory.SPARKS, tier)
        self.assertEqual(factory.TIERS["easy"][4:6], ("muse", "agy"))
        self.assertEqual(factory.TIERS["medium"][4:6], ("muse", "agy"))
        # Then the free provider slots: on a private repo only the two allowed
        # private code (infra#5617), then codex.
        self.assertEqual(factory.TIERS["easy"][6:11], factory.FREE_POOL)
        self.assertEqual(factory.TIERS["medium"][6:11], factory.FREE_POOL)
        self.assertEqual(factory.lanes_of("medium", True)[4:7],
                         ("free-ollama", "free-gemma", "openrouter"))

    def test_the_free_lanes_take_medium_work_before_the_paid_codex_lanes(self):
        # Operator 2026-10-19: "I always want my free tier used up".
        medium = factory.TIERS["medium"]
        for lane in ("openrouter", "workersai", "zen", "zai"):
            self.assertLess(medium.index(lane), medium.index("codex"))
        # On a private repo the public-only ones are skipped.
        self.assertEqual(factory.lanes_of("medium", True)[4:9],
                         ("free-ollama", "free-gemma", "openrouter", "workersai", "codex"))

    def test_free_provider_slots_that_may_train_take_public_repos_only(self):
        for lane in ("free-nvidia", "free-kilo", "free-groq"):
            self.assertIn(lane, factory.PUBLIC_ONLY)
        for lane in ("free-ollama", "free-gemma"):
            self.assertNotIn(lane, factory.PUBLIC_ONLY)
        self.assertNotIn("free-nvidia", factory.lanes_of("easy", True))
        self.assertIn("free-nvidia", factory.lanes_of("easy", False))
        # Never on hard work, and never counted as a Spark.
        self.assertFalse(set(factory.FREE_POOL) & set(factory.TIERS["hard"]))
        self.assertFalse(set(factory.FREE_POOL) & set(factory.SPARKS))

    def test_a_public_easy_ticket_may_go_to_zen(self):
        self.gh.private = False
        self.limited_easy(busy=("muse", "agy", "opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4",
                                "openrouter", "workersai") + factory.FREE_POOL)
        self.assertEqual(self.lane(7), "zen")

    def test_an_easy_ticket_never_goes_to_claude_or_anthropic(self):
        self.gh.private = False
        self.limited_easy(busy=[x for x in factory.TIERS["easy"] if x != "spark"])
        self.assertEqual(self.lane(7), "spark")
        self.assertEqual([c for c in self.gh.comments[7]], [])

    def test_a_limit_hand_off_of_a_briefed_ticket_never_picks_a_remote_lane(self):
        self.gh.private = False
        self.limited_easy()
        self.assertNotEqual(self.lane(7), "muse")
        self.assertEqual(self.lane(7), "agy")

    def test_a_lane_without_an_available_heartbeat_is_not_free(self):
        self.limited_easy(available=["openrouter"])
        self.assertEqual(self.lane(7), "openrouter")

    def test_a_medium_ticket_takes_an_anthropic_slot_only_when_codex_and_opencode_go_are_full(self):
        body = "Difficulty: medium\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="codex", body=body)
        self.gh.ticket(8, "briefed", lane="codex", body=body)
        self.go(limited=["codex"])
        self.assertEqual(sorted((self.lane(7), self.lane(8))), ["spark", "spark-2"])
        self.setUp()
        self.gh.ticket(7, "briefed", lane="codex", body=body)
        for n, lane in enumerate(("codex-2", "opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4"), 20):
            self.gh.ticket(n, "briefed", lane=lane)
        self.go(limited=["codex"])
        # Next come the Sparks, and an anthropic slot only once they are busy.
        self.assertEqual(self.lane(7), "spark")
        self.setUp()
        self.gh.ticket(7, "briefed", lane="codex", body=body)
        busy = (("codex-2", "opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4")
                + factory.SPARKS + factory.FREE_POOL + ("openrouter", "workersai", "zen", "zai"))
        for n, lane in enumerate(busy, 20):
            self.gh.ticket(n, "briefed", lane=lane)
        self.go(limited=["codex"])
        self.assertEqual(self.lane(7), "anthropic")

    def limited_medium(self, busy=()):
        """A medium ticket briefed on codex with every medium lane at a limit,
        and a ticket waiting on each lane in `busy`."""
        self.gh.ticket(7, "briefed", lane="codex")
        for n, lane in enumerate(busy, 20):
            self.gh.ticket(n, "briefed", lane=lane)
        with mock.patch("bus.factory._say") as say:
            self.go(limited=factory.TIERS["medium"])
        return say

    def test_a_medium_ticket_whose_tier_is_at_a_limit_falls_back_to_claude(self):
        say = self.limited_medium()
        self.assertEqual((self.lane(7), self.model(7)), ("claude", None))
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("fallback: no medium lane is up", note)
        self.assertNotIn("loom:rung", note)
        say.assert_any_call("moved %s#7 off lane codex, at a limit, to claude "
                            "(fallback: no medium lane is up)" % REPO)

    def test_with_claude_busy_a_medium_ticket_falls_back_to_a_free_easy_lane(self):
        self.limited_medium(busy=("claude",))
        # The free lanes are medium lanes since 2026-10-19, so when every
        # medium lane is limited they are limited too: the ticket waits.
        self.assertEqual(self.lane(7), "codex")

    def test_a_private_ticket_never_falls_back_to_a_public_only_lane(self):
        # A private ticket with codex at a limit and the other private-safe
        # medium lanes busy waits; zen and zai are public only.
        body = "Difficulty: medium\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="codex", body=body)
        busy = (factory.SPARKS + ("free-ollama", "free-gemma", "openrouter", "workersai",
                                  "codex-2", "opencode-go", "opencode-go-2", "opencode-go-3")
                + factory.ANTHROPIC)
        for n, lane in enumerate(busy, 20):
            self.gh.ticket(n, "briefed", lane=lane)
        self.go(limited=["codex"])
        self.assertNotIn(self.lane(7), ("zen", "zai", "agy", "muse", "opencode-go-4"))

    def test_a_medium_lane_that_is_up_but_busy_is_waited_for(self):
        self.gh.ticket(7, "briefed", lane="codex")
        self.gh.ticket(20, "briefed", lane="codex-2")
        self.go(limited=[x for x in factory.TIERS["medium"] if x != "codex-2"] + ["codex"])
        self.assertEqual(self.lane(7), "codex")

    def test_a_fallback_ticket_returns_to_its_tier_on_its_next_move(self):
        self.limited_medium()
        self.assertEqual(self.lane(7), "claude")
        self.go(limited=["claude"])
        self.assertEqual(self.lane(7), "spark")
        self.assertIn("in its difficulty tier", self.gh.comments[7][-1]["body"])

    def test_a_ticket_whose_model_is_limited_on_its_lane_moves_within_its_tier(self):
        self.gh.private = False
        body = "Model: claude-sonnet-5-5\nDifficulty: hard\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="agy", body=body)
        self.go(model_limits={"agy": {"claude-sonnet-5-5": NOW + 3600}})
        self.assertNotEqual(self.lane(7), "agy")
        self.assertIn(self.lane(7), factory.TIERS["hard"])
        self.assertIn("Not a failure (lane agy is at a limit)", self.gh.comments[7][-1]["body"])

    def test_a_rung_ticket_whose_model_is_limited_on_its_only_lane_waits(self):
        self.gh.private = False
        body = "Model: gemini-4-argon-high\nDifficulty: hard\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="agy", body=body)
        self.go(model_limits={"agy": {"gemini-4-argon-high": NOW + 3600}})
        self.assertEqual((self.lane(7), self.model(7)), ("agy", "gemini-4-argon-high"))

    def test_a_rung_ticket_whose_only_lane_is_limited_for_a_day_climbs(self):
        self.gh.private = False
        body = "Model: gemini-4-argon-high\nDifficulty: hard\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="agy", body=body)
        with mock.patch("bus.factory._say") as say:
            self.go(model_limits={"agy": {"gemini-4-argon-high": NOW + 86400}})
        self.assertNotEqual(self.lane(7), "agy")
        self.assertIn(self.model(7), factory.STRONG)
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("Not a failure (lane agy is at a limit for over 2 hours)", note)
        self.assertTrue(any(c.args[0].startswith("climbed %s#7 off lane agy" % REPO)
                            for c in say.call_args_list), say.call_args_list)
        # The rung it left was not failed on, so it stays untried.
        self.assertEqual(self.moved, [])

    def test_a_ladder_ticket_at_a_limit_stays_on_its_rung(self):
        body = "Model: claude-opus-5-5\nDifficulty: easy\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.go(limited=["claude"])
        self.assertEqual((self.lane(7), self.model(7)), ("anthropic", "claude-opus-5-5"))

    def test_a_model_agy_lacks_does_not_move_to_agy_at_a_limit(self):
        self.gh.private = False
        body = "Model: gpt-6-sol\nDifficulty: easy\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="spark", body=body)
        self.go(limited=["spark"], available=["agy"])
        self.assertEqual((self.lane(7), self.model(7)), ("spark", "gpt-6-sol"))

    def test_a_stalled_ticket_with_a_model_agy_lacks_does_not_move_to_agy(self):
        self.gh.private = False
        body = "Model: gpt-6-sol\nDifficulty: easy\n\n## Why\nx\n"
        self.stalled(7, lane="spark", body=body)
        self.go(available=["agy"])
        self.assertEqual((self.lane(7), self.model(7)), ("spark", "gpt-6-sol"))

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

    def escalated_after_sol(self, number, state):
        """A ticket whose first attempts ran on gpt-6-sol (its Model: line, two
        codex claims, no rung marker) and that the brain escalated to rung 1."""
        self.gh.ticket(number, state, lane="claude",
                       body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        sol_claim = {"body": "Claimed for lane codex (body sha256 0123456789ab)", "user": ME}
        self.gh.comments[number] += [sol_claim, sol_claim, {
            "body": "<!-- loom:escalate v1 -->\nEscalated to rung 1 of 3: claude-sonnet-5-5 "
                    "on lane claude.\nReason: 2 fix rounds (attempts 2, tried gpt-6-sol).\n"
                    "Rung: 1 claude-sonnet-5-5 claude", "user": ME}]

    @lane_models(codex="gpt-6-sol")
    def test_a_ticket_that_ran_on_sol_is_never_spread_back_to_sol(self):
        # 2026-10-16: three attempts on gpt-6-sol with no rung marker, escalated
        # to rung 1, then spread off the busy claude lane to gpt-6-sol again.
        for n in range(1, 4):
            self.gh.ticket(n, "briefed", lane="claude")
        self.escalated_after_sol(7, "briefed")
        self.go()
        self.assertNotIn(self.lane(7), ("codex", "codex-2"))
        self.assertNotEqual(self.model(7), "gpt-6-sol")
        self.assertIn((self.lane(7), self.model(7)),
                      [("claude", "claude-sonnet-5-5")]
                      + [(lane, "claude-opus-5-5") for lane in factory.ANTHROPIC])
        self.go(limited=factory.ANTHROPIC)  # rung 3 full: it waits on its rung
        self.assertNotEqual(self.model(7), "gpt-6-sol")

    @lane_models(codex="gpt-6-sol")
    def test_a_ticket_that_ran_on_sol_climbs_past_sol_when_it_fails(self):
        self.escalated_after_sol(7, "failed")
        self.gh.comments[7].append(CLAIM)  # it ran on rung 1 and failed there
        self.go()
        self.assertEqual(self.model(7), "claude-opus-5-5")
        self.assertIn("rung 3", self.gh.comments[7][-1]["body"])

    @lane_models(claude="claude-sonnet-5-5", codex="gpt-6-sol")
    def test_ran_models_reads_claims_the_model_line_and_markers(self):
        sol = {"body": "Claimed for lane codex (body sha256 0123456789ab)"}
        opus = {"body": "Model: claude-opus-5-5\n\n## Why\nx\n"}
        self.assertEqual(factory.ran_models(opus, [CLAIM]), {"claude-opus-5-5"})
        # With no Model: line, a claim ran on its lane's configured model.
        self.assertEqual(factory.ran_rungs({"body": "x"}, [sol, CLAIM]), {1, 2})
        marked = [sol, {"body": factory.RUNG_MARKER % 3}, CLAIM]
        self.assertEqual(factory.ran_models(opus, marked), {"gpt-6-sol", "claude-opus-5-5"})
        # A rung 0 marker starts the ladder again; an unclaimed ticket ran on nothing.
        self.assertEqual(factory.ran_models(opus, [sol, {"body": factory.RUNG_MARKER % 0}]), set())
        self.assertEqual(factory.ran_models(opus, []), set())

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


class SilentLane(Base):
    """A running ticket on a lane that has sent no heartbeat for SILENT_MAX."""

    def silent_ticket(self, body="Difficulty: medium\n\n## Why\nx\n"):
        self.gh.ticket(7, "running", lane="codex", body=body)
        self.gh.comments[7].append({"body": "Claimed for lane codex (body sha256 0123456789ab)",
                                    "user": ME})

    def labels(self, number=7):
        return {x["name"] for x in self.gh.issues[number]["labels"]}

    def test_a_ticket_on_a_lane_silent_31_minutes_moves_to_a_free_lane_of_its_tier(self):
        self.silent_ticket()
        f = self.go(silent={"codex": 31 * 60})
        self.assertNotEqual(self.lane(7), "codex")
        self.assertIn(self.lane(7), factory.TIERS["medium"])
        self.assertEqual(self.labels() & {"loom:running", "loom:queued"}, set())
        self.assertIn("loom:briefed", self.labels())
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("Not a failure", note)
        self.assertIn("lane codex", note)
        self.assertIn("31 minutes", note)
        self.assertIn("lane %s" % self.lane(7), note)
        self.assertEqual(f.silent_running, {"codex": [(REPO, 7)]})

    def test_the_move_records_no_failure_and_uses_no_retry(self):
        self.silent_ticket()
        self.go(silent={"codex": 31 * 60})
        comments = self.gh.comments[7]
        self.assertEqual(station.retry_chain(comments), (0, set(), None))
        self.assertEqual(factory.rung_of(comments), 0)
        self.assertFalse(any(factory.FAILED_RE.match(c["body"]) for c in comments))
        self.assertEqual(self.moved, [])
        # Its claim is no attempt on the new lane: the move is a new brief.
        self.assertEqual(factory.attempts_since_brief(comments), [])

    def test_a_model_ticket_moves_to_a_free_lane_that_takes_its_model(self):
        self.silent_ticket(body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        self.go(silent={"codex": 31 * 60})
        self.assertIn("loom:briefed", self.labels())
        self.assertTrue(station.lane_takes_model(self.lane(7), "claude-sonnet-5-5"))
        self.assertEqual(self.model(7), "claude-sonnet-5-5")

    def test_a_lane_silent_29_minutes_leaves_the_ticket_alone(self):
        self.silent_ticket()
        f = self.go(silent={"codex": 29 * 60})
        self.assertEqual((self.lane(7), self.gh.writes, f.silent_running), ("codex", [], {}))

    def test_a_live_lane_leaves_the_ticket_alone(self):
        self.silent_ticket()
        f = self.go(silent={})
        self.assertEqual((self.lane(7), self.gh.writes, f.silent_running), ("codex", [], {}))

    def test_a_remote_lane_is_left_to_the_remote_heartbeat_rules(self):
        self.gh.ticket(7, "running", lane="muse")
        f = self.go(silent={"muse": 31 * 60})
        self.assertEqual((self.gh.writes, f.silent_running), ([], {}))

    def test_a_held_ticket_is_left_alone(self):
        self.silent_ticket()
        self.go(silent={"codex": 31 * 60}, held=["%s#7" % REPO])
        self.assertEqual(self.gh.writes, [])

    def test_with_no_free_lane_of_the_tier_the_ticket_goes_back_to_queued(self):
        self.silent_ticket()
        busy = [x for x in factory.lanes_of("medium", True) if x != "codex"]
        for n, lane in enumerate(busy, 20):
            self.gh.ticket(n, "briefed", lane=lane)
        f = self.go(silent={"codex": 31 * 60}, available=busy)
        self.assertEqual(self.labels() & {"loom:running", "loom:briefed"}, set())
        self.assertIn("loom:queued", self.labels())
        self.assertNotIn("lane:codex", self.labels())
        self.assertIn("Not a failure", self.gh.comments[7][-1]["body"])
        self.assertEqual(station.retry_chain(self.gh.comments[7]), (0, set(), None))
        self.assertEqual(f.silent_running, {"codex": [(REPO, 7)]})


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
        # Past Opus only the untried strong models are left: sol, then Argon.
        self.assertEqual(factory.next_rung({3}, {}), 2)
        self.assertEqual(factory.next_rung({1, 2, 3}, {}), 4)
        self.assertEqual(factory.next_rung({1, 2, 3, 4}, {}), 0)
        # Every untried rung busy: wait on the least loaded one.
        all_busy = dict(busy, agy=2, **{lane: 2 for lane in factory.ANTHROPIC})
        self.assertEqual(factory.next_rung({1}, all_busy), 2)
        self.assertEqual(factory.next_rung(set(), dict(all_busy, claude=30)), 2)
        self.assertEqual(factory.next_rung({1}, dict(all_busy, agy=0)), 4)

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

    def test_the_fallback_order_matches_muse_md(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "MUSE.md")) as fh:
            doc = " ".join(fh.read().split())
        for difficulty, fallback in factory.FALLBACK.items():
            self.assertTrue(set(fallback) <= set(factory.TIERS) - {difficulty})
            self.assertIn("%s falls back to %s" % (difficulty.capitalize(), ", then ".join(fallback)), doc)
        for difficulty in set(factory.TIERS) - set(factory.FALLBACK):
            self.assertRegex(doc, re.compile(r"\b%s\b[\w ]* have no fallback" % difficulty, re.I))

    def test_without_model_drops_only_the_header(self):
        self.assertEqual(factory.without_model("Model: m\nDifficulty: easy\n\nModel: x"),
                         "Difficulty: easy\n\nModel: x")
        self.assertEqual(factory.without_model("Difficulty: easy\n\nx"), "Difficulty: easy\n\nx")

    def test_tried_rungs_start_again_at_a_rung_zero_marker(self):
        comments = ALL_RUNGS + [{"body": factory.RUNG_MARKER % 0}, {"body": factory.RUNG_MARKER % 2}, CLAIM]
        self.assertEqual(factory.tried_rungs(comments), {2})

    def test_a_rung_with_no_claim_is_not_tried(self):
        comments = [{"body": factory.RUNG_MARKER % 1}, CLAIM, {"body": factory.RUNG_MARKER % 2}]
        self.assertEqual(factory.tried_rungs(comments), {1})

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


    def test_the_station_tells_the_factory_which_tickets_it_holds(self):
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], ["alice-example"], [], factory_on=True)
            st = station.Station(FakeGitHub(), settings, now=NOW)
            os.makedirs(st.state, exist_ok=True)
            st._note_held(REPO, 6, "%s#741" % REPO)
            self.assertEqual(st._factory().held, {"%s#6" % REPO})

    def test_the_station_tells_the_factory_which_tickets_the_gate_refused(self):
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], ["alice-example"], [], factory_on=True)
            st = station.Station(FakeGitHub(), settings, now=NOW)
            os.makedirs(st.state, exist_ok=True)
            st._gate_holds = {"%s#30" % REPO: {"repo": REPO, "goal": 30, "count": 64,
                                               "limit": 60, "tickets": {633: "x"}, "idle": 0}}
            self.assertIn("%s#633" % REPO, st._factory().held)

    def test_the_station_runs_the_factory_when_on_and_throttles_it(self):
        calls = []

        class Fake:
            def __init__(self, gh, repos, now, trusted, review_check, limited, review_bot=None,
                         no_test_repos=(), available=None, briefers=(), held=(),
                         model_limits=None, silent=None, down=()):
                self.silent_running, self.silent = {}, silent
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

    def test_the_station_assigns_queued_on_every_pass(self):
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], ["alice-example"], [], factory_on=True)
            st = station.Station(FakeGitHub(), settings, now=NOW)
            with mock.patch.object(st, "_factory") as make_factory:
                st.assign_queued()
                st.assign_queued()
                self.assertEqual(make_factory.return_value.assign_queued.call_count, 2)
                settings.factory_on = False
                st.assign_queued()
                self.assertEqual(make_factory.return_value.assign_queued.call_count, 2)


class DeadLane(unittest.TestCase):
    def comments(self, *bodies):
        at = "2026-10-17T05:00:00Z"
        return [{"body": b, "created_at": at} for b in bodies]

    def test_claims_after_the_last_brief_are_the_attempts(self):
        comments = self.comments(
            "The station gave this queued ticket lane spark (tier medium).",
            "Claimed for lane spark (body sha256 0123456789ab)",
            "Not a failure (lane spark is at a limit): the factory moved this "
            "ticket to lane codex-2, in its difficulty tier.",
            "Claimed for lane codex-2 (body sha256 0123456789ab)",
            "Claimed for lane codex-2 (body sha256 0123456789ab)",
            "Runaway: three attempts within six hours.")
        self.assertEqual(factory.attempts_since_brief(comments),
                         [(a[0], a[1], "codex-2", "") for a in
                          factory.attempts(comments)[-2:]])
        self.assertEqual(factory.dead_lane(comments), "codex-2")

    def test_attempts_on_two_lanes_are_not_one_dead_lane(self):
        comments = self.comments(
            "Claimed for lane codex (body sha256 0123456789ab)",
            "Claimed for lane codex-2 (body sha256 0123456789ab)")
        self.assertIsNone(factory.dead_lane(comments))

    def test_no_claims_is_not_one_dead_lane(self):
        self.assertIsNone(factory.dead_lane(self.comments("Runaway: three attempts.")))

    def test_a_retry_and_a_ladder_move_are_briefs(self):
        comments = self.comments(
            "Claimed for lane codex (body sha256 0123456789ab)",
            "Automatic retry 1 of 2 (transient): moved from lane codex to lane "
            "codex-2. Lane codex failed: [transient] agent exited 1",
            "Claimed for lane codex-2 (body sha256 0123456789ab)")
        self.assertEqual(factory.dead_lane(comments), "codex-2")
        comments = self.comments(
            "Claimed for lane codex (body sha256 0123456789ab)",
            "<!-- loom:rung 2 -->\nRepeatedly stuck (x): the factory moved this ticket "
            "up the model ladder to rung 2, gpt-6-sol on lane codex.",
            "Claimed for lane codex (body sha256 0123456789ab)")
        self.assertEqual(factory.dead_lane(comments), "codex")


class LaneKinds(unittest.TestCase):
    """What each lane costs (loom#968): the Tiers page marks every tier and
    ladder entry free, own hardware, subscription or never-paid."""

    def test_every_lane_in_the_tiers_and_the_ladder_has_a_kind(self):
        named = {lane for lanes in factory.TIERS.values() for lane in lanes}
        named |= {lane for lanes, _ in factory.LADDER for lane in lanes}
        missing = sorted(lane for lane in named if factory.lane_kind(lane) is None)
        self.assertEqual(missing, [])
        self.assertEqual(set(factory.LANE_KINDS.values()) - set(factory.KINDS), set())

    def test_lane_kinds_lists_base_lanes_only(self):
        # A numbered slot takes its lane's kind through lane_kind.
        slots = sorted(lane for lane in factory.LANE_KINDS if labels.base_lane(lane) != lane)
        self.assertEqual(slots, [])

    def test_a_slot_maps_to_its_lane_by_one_rule(self):
        self.assertEqual(labels.base_lane("anthropic-7"), "anthropic")
        self.assertEqual(labels.base_lane("opencode-go-3"), "opencode-go")
        self.assertEqual(labels.base_lane("spark-2"), "spark")
        # A free provider slot is its own lane: its name has no number.
        self.assertEqual(labels.base_lane("free-kilo"), "free-kilo")

    def test_sparks_are_own_hardware_and_the_free_pool_is_free(self):
        self.assertEqual({factory.lane_kind(lane) for lane in factory.SPARKS}, {"own-hardware"})
        self.assertEqual({factory.lane_kind(lane) for lane in factory.FREE_POOL}, {"free"})
        self.assertEqual({factory.lane_kind(lane) for lane in factory.ANTHROPIC}, {"never-paid"})
        self.assertEqual(factory.lane_kind("claude"), "subscription")
        self.assertEqual(factory.lane_kind("anthropic-9"), "never-paid")
        self.assertIsNone(factory.lane_kind("no-such-lane"))


if __name__ == "__main__":
    unittest.main()
