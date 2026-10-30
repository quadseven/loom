"""The factory pass (bus/factory.py) with a fake GitHub and recorded actions."""

import base64
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from urllib.parse import unquote
from unittest import mock

from bus import brain, factory, hunt_plan, labels, merge_tier, pr_repair, station, status, tickets
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
        self.reviews = {}
        self.pulls = {}
        self.runs = {}
        self.details = {}
        self.writes = []
        self.behind = {}
        self.attempts = {}
        self.update_status = 202
        self.files = {}
        self.patches = {}
        self.contents = {}
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
        if re.search(r"/issues\?labels=loom:(triage|factory)&state=open$", path):
            return list(self.filed)
        if "/issues?labels=loom%3Aanswer-pending&state=open" in path:
            return [i for i in self.issues.values() if i.get("state") == "open"
                    and "loom:answer-pending" in [x["name"] for x in i.get("labels") or []]]
        m = re.search(r"/issues/(\d+)/comments$", path)
        if m:
            # As the GitHub client does: a status comment reads as its events.
            return status.expand(list(self.comments.get(int(m.group(1)), [])), [ME["login"]])
        m = re.search(r"/issues/(\d+)/events$", path)
        if m:
            return list(self.events[int(m.group(1))])
        m = re.search(r"/pulls/(\d+)/reviews$", path)
        if m:
            return list(self.reviews.get(int(m.group(1)), []))
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
            # A one-line change, unless the test gave the file its own patch.
            return Response(200, [{"filename": n, "patch": self.patches.get(
                n, "@@ -1 +1 @@\n-old\n+new")} for n in names])
        if path == "/repos/%s/commits/main" % REPO:
            return Response(200, {"sha": "shamain"})
        m = re.search(r"/commits/(\w+)/check-runs", path)
        if m:
            if m.group(1) == "shamain":
                return Response(200, {"check_runs": self.runs.get("shamain", [])})
            return Response(200, {"check_runs": self.runs[m.group(1)]})
        m = re.search(r"/compare/main\.\.\.sha(\d+)$", path)
        if m:
            return Response(200, {"behind_by": self.behind.get(int(m.group(1)), 0)})
        if path == "/repos/%s" % REPO:
            return Response(200, {"private": self.private})
        m = re.search(r"/contents/(.+?)\?ref=", path)
        if m:
            # A folder lists the files under it; a file comes back encoded.
            name = m.group(1)
            if name in self.contents:
                return Response(200, {"content": base64.b64encode(
                    self.contents[name].encode()).decode()})
            listed = [{"name": k.rsplit("/", 1)[1]} for k in self.contents
                      if k.rsplit("/", 1)[0] == name]
            return Response(200, listed) if listed else Response(404, None)
        m = re.search(r"/actions/runs/(\d+)$", path)
        if m:
            return Response(200, {"run_attempt": self.attempts.get(m.group(1), 1)})
        m = re.search(r"/actions/jobs/(\d+)/logs$", path)
        if m:
            return Response(302, None, headers={"location": "https://logs.example.com/" + m.group(1)})
        raise AssertionError("unexpected GET %s" % path)

    def transport(self, method, url, headers, body):
        # A job with no log given answers as an expired log does.
        log = self.logs.get(url.rsplit("/", 1)[1])
        return (404, {}, b"") if log is None else (200, {}, log.encode("utf-8"))

    def _write(self, method, path, body):
        self.writes.append((method, path, body))
        m = re.search(r"/pulls/(\d+)/reviews/(\d+)/dismissals$", path)
        if m:
            for review in self.reviews.get(int(m.group(1)), []):
                if review["id"] == int(m.group(2)):
                    review["state"] = "DISMISSED"
            return Response(200, {})
        if path.endswith("/update-branch"):
            return Response(self.update_status, {})
        if method == "POST" and path.endswith("/issues"):
            made = dict(body, number=900 + len(self.filed), id=5000 + len(self.filed), user=ME)
            self.filed.append(made)
            return Response(201, made)
        m = re.search(r"/issues/(\d+)/labels/([^/]+)$", path)
        if m and method == "DELETE":
            # As GitHub does: one label off, a missing one is a 404 the caller allows.
            item = self.issues.get(int(m.group(1))) or self.pulls.get(int(m.group(1))) or {}
            gone = unquote(m.group(2))
            item["labels"] = [x for x in item.get("labels") or [] if x["name"] != gone]
            return Response(200, {})
        m = re.search(r"/issues/(\d+)(/labels|/comments)?$", path)
        if m:
            number = int(m.group(1))
            if m.group(2) == "/labels":
                if number in self.issues and method == "POST":
                    # Added, as GitHub's POST adds; a PUT replaces them all.
                    have = [x["name"] for x in self.issues[number]["labels"]]
                    self.issues[number]["labels"] = [{"name": x} for x in dict.fromkeys(have + body["labels"])]
                elif number in self.issues:
                    self.issues[number]["labels"] = [{"name": x} for x in body["labels"]]
                else:
                    have = [x["name"] for x in self.pulls[number].get("labels", [])]
                    self.pulls[number]["labels"] = [
                        {"name": x} for x in dict.fromkeys(have + list(body["labels"]))]
                    # As GitHub does: each label put on leaves a labeled event.
                    self.events.setdefault(number, []).extend(
                        {"event": "labeled", "label": {"name": x}, "created_at": iso(0)}
                        for x in body["labels"] if x not in have)
            elif m.group(2) == "/comments":
                self.next_comment = getattr(self, "next_comment", 1000) + 1
                self.comments.setdefault(number, []).append(
                    {"id": self.next_comment, "body": body["body"], "created_at": iso(0), "user": ME})
            elif method == "PATCH":
                target = self.issues.get(number) or next(
                    (f for f in self.filed if f["number"] == number), None)
                target.update(body)
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
        # Every risk tier may merge here: these tests are about the other
        # merge rules; bus/merge_tier.py has its own (test_merge_tier.py).
        tiers = mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": ",".join(merge_tier.TIERS)})
        tiers.start()
        self.addCleanup(tiers.stop)
        self.gh = FakeGitHub()
        # Each test's pull request reuses head shas; the reclass memo is per station process.
        factory._CLASSED.clear()
        factory._KEPT_AT.clear()
        self.merged, self.fixed, self.moved, self.closed = [], [], [], []
        self.humans = []
        self.placed = []

    def go(self, limited=(), no_test_repos=(), available=None, model_limits=None, held=(), silent=None,
           down=(), limit_until=None, scope=None, known_waits=None, demoted=None,
           no_lane_since=None, review_brief=None, now=NOW):
        def merge(gh, repo, number, ticket):
            # As GitHub does: a standing change request blocks the merge.
            if any(r["state"] == "CHANGES_REQUESTED" for r in gh.reviews.get(number, [])):
                raise GitHubError("PUT /repos/%s/pulls/%d/merge -> 405" % (repo, number), 405)
            self.merged.append((number, ticket))
        def fix(gh, repo, number, ticket, notes, lane=None, model=None, authors=None):
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
        def place(gh, repo, issue):
            # As brain.place_under_epic does: the ticket ends up under an epic.
            self.placed.append(issue["number"])
            gh.issues[issue["number"]]["body"] += "\nPart of #77\n"
            return 77
        def human(gh, repo, number, ask):
            self.humans.append((number, ask))
            gh.comments[number].append({"body": brain.HUMAN_MARKER + "\n" + ask, "user": ME})
            gh.issues[number]["labels"].append({"name": "loom:human"})
        f = factory.Factory(self.gh, [REPO], now, trusted=["Alice-Example"],
                            merge=merge, fix=fix, move=move, close=close, human=human, place=place,
                            limited=limited, no_test_repos=no_test_repos,
                            available=available, model_limits=model_limits, held=held,
                            silent=silent, down=down, limit_until=limit_until, scope=scope,
                            known_waits=known_waits, demoted=demoted,
                            no_lane_since=no_lane_since, review_brief=review_brief)
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

    def unticked_ticket(self, lane="codex"):
        criteria = ["A1 the cache is read once", "A2 a stale entry is dropped",
                    "Live: observed after deploy"]
        ticket_body = ("Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n" +
                       "\n".join("- [ ] " + item for item in criteria) + "\n")
        self.gh.ticket(5, "pr", body=ticket_body, lane=lane)
        self.gh.pull(50, 5, body="## Acceptance criteria\n- [x] A1 the cache is read once\n"
                                 "- [ ] A2 a stale entry is dropped\n- [ ] Live: observed after deploy\n")

    def test_unticked_criteria_go_back_naming_each_one(self):
        # 2026-10-20: the note said "failing checks, open review threads or a
        # conflict" and listed none, so a lane had nothing to act on and the
        # same green pull request came back six times.
        self.unticked_ticket()
        f = self.go()
        self.assertEqual(self.merged, [])
        self.assertIn(factory.UNTICKED_HEADING, f.notes)
        self.assertIn("- A2 a stale entry is dropped", f.notes)
        self.assertNotIn("A1 the cache is read once", f.notes)
        self.assertNotIn("Live: observed", f.notes)
        self.assertIn("not built: <reason>", f.notes)
        self.assertEqual((self.lane(5), factory.rung_of(self.gh.comments[5])), ("codex", 0))

    def test_a_criterion_the_text_check_refuses_still_goes_back_with_the_plain_note(self):
        body = ("Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n"
                "- [ ] Fixes #12 once the cache is read once\n")
        self.gh.ticket(5, "pr", body=body, lane="codex")
        self.gh.pull(50, 5, body="## Acceptance criteria\n- [ ] Fixes #12 once the cache is read once\n")
        f = self.go()
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(f.notes, factory.FIX_NOTES)

    def test_a_second_round_with_the_same_criteria_unticked_climbs(self):
        self.unticked_ticket(lane="opencode-go")
        self.gh.comments[5].append({"body": FIX_MARKER + "\n\n" + factory.UNTICKED_HEADING +
                                    "\n\n- A2 a stale entry is dropped", "user": ME})
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 1)

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

    def test_a_criterion_follow_up_is_filed_under_an_epic_not_under_its_ticket(self):
        # loom#951: it said "Part of" the ticket it came from, which is no
        # epic, so it sat in none.
        criteria = ["A%d" % n for n in range(1, 6)] + ["Live: observed after deploy"]
        ticket_body = ("Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n" +
                       "\n".join("- [ ] " + item for item in criteria) + "\n\nPart of #7\n")
        pr_criteria = ("\n".join("- [x] " + item for item in criteria[:4]) +
                       "\n- [ ] A5; not built: requires a later integration\n" +
                       "- [ ] " + criteria[5] + "\n")
        self.gh.ticket(5, "pr", body=ticket_body)
        self.gh.pull(50, 5, body="## Acceptance criteria\n" + pr_criteria)
        with mock.patch.object(factory.brain, "placement_epic",
                                        return_value=60) as placed:
            self.go()
        self.assertEqual(placed.call_args[0][2:], (7, 1))
        self.assertEqual(len(self.gh.filed), 1)
        self.assertTrue(self.gh.filed[0]["body"].rstrip().endswith("Part of #60"))
        self.assertIn(("POST", "/repos/%s/issues/60/sub_issues" % REPO, {"sub_issue_id": 5000}),
                      self.gh.writes)

    def test_a_ready_pull_request_is_merged(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual(self.fixed, [])

    # -- the scope check (bus/scope_fit.py; loom#1348) ------------------
    SCOPE_TICKET = ("Difficulty: medium\n\n## Why\nx\n\n## What\nKeep four Deployments off one "
                    "worker with an anti-affinity in each Deployment's own repo.\n\n## Size\nS\n")

    def scoped(self, files=("charts/runner/values.yaml", "docs/x.md"), private=False):
        self.gh.private = private
        self.gh.ticket(5, "pr", body=self.SCOPE_TICKET)
        self.gh.pull(50, 5, files=files)
        self.asked = []

        def ask(prompt):
            self.asked.append(prompt)
            return self.reply
        return ask

    def test_a_pull_request_whose_files_do_not_fit_its_ticket_is_held_not_merged(self):
        # 2026-10-21: the ticket named four Deployments in other repos; the lane
        # changed the CI runner pool's values instead, and it landed.
        ask = self.scoped()
        self.reply = "NO: it edits the runner pool values, not the four Deployments' repos."
        self.go(scope=ask)
        self.assertEqual(self.merged, [])
        self.assertEqual(len(self.asked), 1)
        self.assertIn("anti-affinity", self.asked[0])
        self.assertIn("charts/runner/values.yaml", self.asked[0])
        self.assertIn({"name": "needs-brain"}, self.gh.pulls[50]["labels"])
        held = self.gh.comments[50][-1]["body"]
        self.assertIn(factory.SCOPE_HOLD_MARKER % "sha50", held)
        self.assertIn("runner pool values", held)
        self.assertIn("runner pool values", self.gh.comments[5][-1]["body"])

    def test_a_held_pull_request_is_not_asked_about_again_for_the_same_head(self):
        ask = self.scoped()
        self.reply = "NO: wrong repo."
        self.go(scope=ask)
        posted = len(self.gh.comments[50])
        self.reply = "YES"
        self.go(scope=ask)
        self.assertEqual((self.merged, len(self.asked), len(self.gh.comments[50])), ([], 1, posted))

    def test_a_new_head_commit_is_asked_about_again(self):
        ask = self.scoped()
        self.reply = "NO: wrong repo."
        self.go(scope=ask)
        self.gh.pulls[50]["head"]["sha"] = "sha51"
        self.gh.details[50]["headRefOid"] = "sha51"
        self.gh.runs["sha51"] = self.gh.runs["sha50"]
        self.reply = "YES"
        self.go(scope=ask)
        self.assertEqual((self.merged, len(self.asked)), ([(50, (REPO, 5))], 2))

    def test_a_pull_request_whose_files_fit_is_merged_as_before(self):
        ask = self.scoped()
        self.reply = "YES"
        self.go(scope=ask)
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual(len(self.asked), 1)

    def test_no_answer_never_blocks_a_merge(self):
        ask = self.scoped()
        for reply in (None, "", "I am not sure what you mean", "NO", "maybe"):
            self.setUp()
            ask = self.scoped()
            self.reply = reply
            self.go(scope=ask)
            self.assertEqual(self.merged, [(50, (REPO, 5))], reply)

    def test_a_broken_asker_never_blocks_a_merge(self):
        self.scoped()

        def broken(prompt):
            raise RuntimeError("route down")
        self.go(scope=broken)
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_a_private_repo_is_never_asked(self):
        ask = self.scoped(private=True)
        self.reply = "NO: wrong repo."
        self.go(scope=ask)
        self.assertEqual((self.merged, self.asked), ([(50, (REPO, 5))], []))

    def test_without_a_scope_route_the_check_is_off(self):
        self.scoped()
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])

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

    def blocked_by(self, login, user_type="Bot", submitted=600):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        # The review check passed 60s ago; the change request is older.
        self.gh.runs["sha50"] = [{"name": "unittest", "status": "completed", "conclusion": "success"},
                                 {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed",
                                  "conclusion": "success", "completed_at": iso(60)}]
        self.gh.reviews[50] = [{"id": 7, "state": "CHANGES_REQUESTED", "submitted_at": iso(submitted),
                                "user": {"login": login, "type": user_type}}]
        self.go()
        return [w for w in self.gh.writes if "/dismissals" in w[1]]

    def test_a_ready_pull_request_blocked_by_the_review_bots_old_change_request_lands(self):
        # The bot's failing review left a change request that its own later
        # green review on the same head never dismissed, and the merge was
        # refused every pass (2026-10-21).
        (dismissal,) = self.blocked_by("grug-tribe[bot]")
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertTrue(dismissal[1].endswith("/reviews/7/dismissals"))
        self.assertEqual(dismissal[2]["event"], "DISMISS")
        self.assertIn(factory.DEFAULT_REVIEW_CHECK, dismissal[2]["message"])

    def test_a_persons_or_another_bots_change_request_is_never_dismissed(self):
        for login, user_type in (("Alice-Example", "User"), ("security-scanner[bot]", "Bot")):
            with self.subTest(login=login):
                self.merged.clear()
                self.assertEqual(self.blocked_by(login, user_type), [])
                self.assertEqual(self.merged, [])

    def test_a_change_request_newer_than_the_passing_review_stays(self):
        self.assertEqual(self.blocked_by("grug-tribe[bot]", submitted=10), [])
        self.assertEqual(self.merged, [])

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

    def test_a_fix_round_for_a_ticket_with_no_lane_label_names_a_lane(self):
        # A silent-lane requeue drops the lane label and the run's pull request
        # can still open after it; the fix brief then needs exactly one lane.
        self.gh.ticket(5, "pr")
        self.gh.issues[5]["labels"] = [x for x in self.gh.issues[5]["labels"]
                                       if not x["name"].startswith("lane:")]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
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
        # An open thread, not a conflict: a conflict alone never climbs, by
        # age either (loom#1963).
        self.gh.ticket(5, "pr")
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 1, "user": ME,
                                 "created_at": iso(3600)}, CLAIM]
        self.gh.pull(50, 5, resolved=False, created=factory.STUCK_AGE * 2)
        self.go()
        self.assertEqual(self.model(5), None, "climbed an hour after the last climb")
        self.assertEqual(len(self.fixed), 1)
        self.gh.comments[5][0]["created_at"] = iso(factory.STUCK_AGE + 60)
        self.gh.issues[5]["labels"] = [{"name": x} for x in ("loom:ticket", "loom:pr", "lane:codex")]
        self.go()
        self.assertEqual(self.model(5), "gpt-6-luna")

    def test_an_old_pull_request_climbs_on_its_first_send_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False, created=factory.STUCK_AGE + 60)
        self.go()
        self.assertEqual(self.model(5), "claude-sonnet-5-5")
        self.assertEqual(len(self.fixed), 1)

    def test_an_old_conflicting_pull_request_gets_a_brief_on_its_lane_without_climbing(self):
        # loom#1963: a ready pull request that hand merges left conflicting
        # read as stuck by age, and waited for a rung instead of a brief.
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.issues[5]["labels"].append({"name": "loom:ready"})
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 1, "user": ME}, CLAIM]
        self.gh.pull(50, 5, mergeable=False, created=factory.STUCK_AGE * 3)
        self.go(limited=("codex", "codex-2") + NO_OPUS)
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.model(5), None)
        self.assertEqual((self.moved, self.closed), ([], []))

    def test_a_conflict_past_the_last_rung_gets_a_brief_not_a_give_up(self):
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.comments[5] += ALL_RUNGS
        self.gh.pull(50, 5, mergeable=False, created=factory.STUCK_AGE * 3)
        self.go()
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertEqual((self.moved, self.closed), ([], []))

    def test_a_conflict_on_a_ticket_a_person_holds_gets_a_hygiene_round_only(self):
        # Changed by loom#2135: the judged send-back still refuses a ticket
        # a person holds, which left an operator-held pull request's
        # conflict unassigned until the operator came to merge it. Branch
        # hygiene opens the conflict round anyway (the loom:human hold
        # guards the merge, not the branch, and stays on the ticket);
        # nothing else about the ticket moves.
        self.gh.ticket(5, "pr")
        self.gh.issues[5]["labels"].append({"name": "loom:human"})
        self.gh.pull(50, 5, mergeable=False, created=factory.STUCK_AGE * 3)
        self.go()
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertEqual((self.moved, self.closed, self.merged), ([], [], []))
        self.assertIn("loom:human", [x["name"] for x in self.gh.issues[5]["labels"]])

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
        self.assertEqual(seen, [("claude", "claude-sonnet-5-5"), ("codex", "gpt-6-luna"),
                                ("claude", "claude-opus-5-5")])

    def test_past_the_last_rung_it_is_left_failed_for_the_brain(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5] += ALL_RUNGS + [{"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.moved, [(5, "failed", None)])

    def test_a_ticket_past_opus_never_climbs_down_to_luna_or_sonnet(self):
        # Before 2026-10-21 rung 2 was gpt-6-sol, a strong model a ticket
        # past Opus could still climb to; luna is not one.
        for private in (True, False):
            with self.subTest(private=private):
                self.setUp()
                self.gh.private = private
                self.gh.ticket(5, "pr", lane="claude")
                self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 3, "user": ME}, CLAIM,
                                        {"body": FIX_ASKED, "user": ME}]
                self.gh.pull(50, 5, resolved=False)
                self.go()
                self.assertEqual(self.moved, [(5, "failed", None)])
                self.assertNotIn(self.model(5), ("gpt-6-luna", "claude-sonnet-5-5"))

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
        self.assertIn("ran on claude-sonnet-5-5, gpt-6-luna, claude-opus-5-5", notes[0])

    def opus_runs(self, body="", private=False):
        """Ticket #5 on lane claude with no Model: line, five runs each with a
        fix round after it, and its pull request #50 (2026-10-17: lane claude
        ran claude-opus-5-5 five times)."""
        self.gh.private = private
        self.gh.ticket(5, "pr", lane="claude")
        for _ in range(5):
            self.gh.comments[5] += [CLAIM, {"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False, body=body)

    def test_five_runs_on_a_lane_set_to_opus_are_recorded_as_opus_and_never_climb_down(self):
        # What ran comes from the pull request's runner footer, else from the
        # lane's configured model; never from a fixed lane-to-model map. Past
        # Opus no stronger rung is left, so the ticket is given up on, never
        # sent down to luna or Sonnet.
        for name, body, models in (("footer", footer("claude", "claude-opus-5-5"), {}),
                                   ("setting", "", {"claude": "claude-opus-5-5"})):
            for private in (False, True):
                with self.subTest(name, private=private), lane_models(**models):
                    self.setUp()
                    self.opus_runs(body, private=private)
                    issue, comments = dict(self.gh.issues[5]), list(self.gh.comments[5])
                    self.go()
                    self.assertEqual(self.moved, [(5, "failed", None)])
                    self.assertNotIn(self.model(5), ("gpt-6-luna", "claude-sonnet-5-5"))
                    ran = factory.claim_models(issue, comments, factory.footer_run(body))
                    self.assertEqual([m for c, m in ran if c], ["claude-opus-5-5"] * 5)

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
        self.assertEqual((self.lane(5), self.model(5)), ("codex", "gpt-6-luna"))
        self.assertEqual(self.moved, [])

    def answered_thread(self, number=50, lane="codex", login="alice-example"):
        """The pull request's only open thread, with a lane's answer on it."""
        self.gh.details[number]["reviewThreads"]["nodes"] = [{
            "id": "PRRT_1", "isResolved": False,
            "comments": {"nodes": [{
                "author": {"__typename": "User", "login": login},
                "body": "Lane %s answered this finding instead of changing the code:"
                        "\n\nThis finding does not apply to the current code" % lane}]}}]

    def test_an_answered_thread_with_no_lane_up_asks_the_review_bot_to_judge_the_answer(self):
        # 2026-10-19: grug#1189 waited from 18:25Z with no lane up for its next
        # rung, and its only open thread carried a lane's answer nothing judged.
        self.gh.ticket(5, "pr", lane="claude",
                       body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 1, "user": ME}, CLAIM,
                                {"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.answered_thread()
        self.go(limited=("codex", "codex-2") + NO_OPUS)
        # It still waits for its rung, and the review bot is asked to judge the
        # answer instead.
        self.assertEqual((self.lane(5), self.model(5)), ("claude", "claude-sonnet-5-5"))
        self.assertEqual(len(self.gh.comments[50]), 1)
        self.assertIn("/grug improve", self.gh.comments[50][0]["body"])
        # Once per head commit: the next pass asks no more.
        self.go(limited=("codex", "codex-2") + NO_OPUS)
        self.assertEqual(len(self.gh.comments[50]), 1)

    def test_a_lane_up_for_the_next_rung_climbs_and_the_review_bot_is_not_asked(self):
        self.gh.ticket(5, "pr", lane="claude",
                       body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 1, "user": ME}, CLAIM,
                                {"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.answered_thread()
        self.go(limited=NO_OPUS)
        self.assertEqual((self.lane(5), self.model(5)), ("codex", "gpt-6-luna"))
        self.assertEqual(self.gh.comments.get(50, []), [])

    def test_a_stranger_s_answer_on_a_thread_asks_nothing(self):
        # The answer is the App's own, so a stranger's words on a thread are
        # data and never move the pull request.
        self.gh.ticket(5, "pr", lane="claude",
                       body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        self.gh.comments[5] += [{"body": factory.RUNG_MARKER % 1, "user": ME}, CLAIM,
                                {"body": FIX_ASKED, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.answered_thread(login="mallory-example")
        self.go(limited=("codex", "codex-2") + NO_OPUS)
        self.assertEqual(self.gh.comments.get(50, []), [])

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
        self.assertEqual(self.model(6), "gpt-6-luna")

    def test_a_ready_pull_request_up_to_date_with_main_merges_with_no_update_call(self):
        # Acceptance: a ready pull request that is up to date with main
        # merges as before, with no update call.
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        with mock.patch("bus.factory._say") as say:
            self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertNotIn(("PUT", "/repos/%s/pulls/50/update-branch" % REPO,
                          {"expected_head_sha": "sha50"}), self.gh.writes)
        for call in say.call_args_list:
            self.assertNotIn("up to date", call.args[0])

    def test_a_ready_pull_request_behind_its_base_is_brought_up_to_date_not_merged(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 3
        with mock.patch("bus.factory._say") as say:
            self.go()
        self.assertEqual(self.merged, [])
        self.assertIn(("PUT", "/repos/%s/pulls/50/update-branch" % REPO,
                       {"expected_head_sha": "sha50"}), self.gh.writes)
        # One log line per update.
        say.assert_called_once_with(
            "brought %s#50 up to date with its base; it lands once its checks "
            "and review pass on the new head" % REPO)

    def test_a_held_ready_pull_request_behind_its_base_is_kept_current_never_merged(self):
        # loom#1963: a pull request held for the operator never reached
        # _land, the only update-branch call, so it drifted into a conflict.
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 4
        with mock.patch.object(factory.Factory, "_operator_holds", return_value=True):
            self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))
        self.assertIn(("PUT", "/repos/%s/pulls/50/update-branch" % REPO,
                       {"expected_head_sha": "sha50"}), self.gh.writes)

    def test_a_clean_sensitive_pull_request_behind_its_base_is_kept_current_never_merged(self):
        # 2026-10-27: the brain's merge gate refuses a pull request behind its
        # base, and nothing brought a sensitive one up to date, so green
        # sensitive pull requests waited behind by 4 to 66 commits.
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=("class:sensitive", "needs-brain"))
        self.gh.behind[50] = 4
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))
        self.assertIn(("PUT", "/repos/%s/pulls/50/update-branch" % REPO,
                       {"expected_head_sha": "sha50"}), self.gh.writes)

    def test_a_held_pull_request_up_to_date_with_its_base_is_left_alone(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        with mock.patch.object(factory.Factory, "_risk_holds", return_value=True):
            self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))
        self.assertEqual([w for w in self.gh.writes if w[1].endswith("/update-branch")], [])

    def test_held_pull_requests_behind_their_base_take_turns_being_kept_current(self):
        # 2026-10-27: one held pull request (the first in landing order) took
        # the repo's one keep-current slot pass after pass as merges kept
        # putting it behind again, so green sensitive ones fell 10 to 71
        # commits behind and never reached the brain's merge gate.
        for ticket, number, created in ((5, 50, 7200), (6, 60, 3600)):
            self.gh.ticket(ticket, "pr")
            self.gh.pull(number, ticket, created=created)
            self.gh.behind[number] = 3
        with mock.patch.object(factory.Factory, "_operator_holds", return_value=True):
            self.go()
            self.go()
        updated = [w[1] for w in self.gh.writes if w[1].endswith("/update-branch")]
        self.assertEqual(updated, ["/repos/%s/pulls/50/update-branch" % REPO,
                                   "/repos/%s/pulls/60/update-branch" % REPO])
        self.assertEqual(self.merged, [])

    def test_a_held_pull_request_whose_update_is_refused_is_not_sent_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 1
        self.gh.update_status = 422
        with mock.patch.object(factory.Factory, "_operator_holds", return_value=True):
            self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_a_held_update_does_not_stop_another_pull_request_landing(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200)
        self.gh.behind[50] = 2
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6, created=3600)
        self.gh.behind[60] = 2
        real = factory.Factory._operator_holds
        with mock.patch.object(factory.Factory, "_operator_holds", autospec=True,
                               side_effect=lambda f, repo, number, *a, **k:
                               number == 50 or real(f, repo, number, *a, **k)):
            self.go()
        self.assertIn(("PUT", "/repos/%s/pulls/50/update-branch" % REPO,
                       {"expected_head_sha": "sha50"}), self.gh.writes)
        self.assertIn(("PUT", "/repos/%s/pulls/60/update-branch" % REPO,
                       {"expected_head_sha": "sha60"}), self.gh.writes)
        self.assertEqual(self.merged, [])

    def test_a_refused_update_goes_back_for_a_fix_round_like_a_conflict(self):
        # 2026-10-20: an update that cannot be applied cleanly goes back for
        # a fix round, as a conflict does, instead of silently waiting.
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 1
        self.gh.update_status = 422
        f = self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(f.actions, 1)

    def test_an_updated_pull_request_lands_on_a_later_pass_once_green(self):
        # First pass: the pull request is behind, so it is updated, not
        # merged. Second pass: its new head is up to date and green, so it
        # merges with no further update call. The update takes the landing
        # lock (loom#2066) and the merge gives it back.
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 3
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))
        self.assertEqual([w[:2] for w in self.gh.writes],
                         [("PUT", "/repos/%s/pulls/50/update-branch" % REPO),
                          ("POST", "/repos/%s/issues/50/labels" % REPO)])
        self.gh.behind[50] = 0
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual(self.gh.writes[2:], [
            ("DELETE", "/repos/%s/issues/50/labels/loom%%3Alanding" % REPO, None)])

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

    def _red_from_base(self, base_conclusion="success", behind=2, failing="unittest"):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": failing, "status": "completed", "conclusion": "failure",
             "details_url": "https://github.com/%s/actions/runs/77/job/1" % REPO},
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "success"}]
        self.gh.behind[50] = behind
        self.gh.runs["shamain"] = [
            {"name": "unittest", "status": "completed", "conclusion": base_conclusion}]

    UPDATE = ("PUT", "/repos/%s/pulls/50/update-branch" % REPO, {"expected_head_sha": "sha50"})

    def test_a_check_red_from_the_base_and_green_on_main_is_updated_not_sent_back(self):
        # loom#1961: four pull requests failed tests their main had fixed;
        # a re-run reuses the old merge commit, and a fix round waits on a lane.
        self._red_from_base()
        f = self.go()
        self.assertIn(self.UPDATE, self.gh.writes)
        self.assertEqual((self.fixed, self.merged), ([], []))
        self.assertFalse(any("rerun" in w[1] for w in self.gh.writes))
        self.assertEqual(f.actions, 1)

    def test_a_check_also_red_on_main_is_run_again_not_updated(self):
        self._red_from_base(base_conclusion="failure")
        self.go()
        self.assertNotIn(self.UPDATE, self.gh.writes)
        self.assertIn(("POST", "/repos/%s/actions/runs/77/rerun-failed-jobs" % REPO, None),
                      self.gh.writes)

    def test_a_check_that_never_ran_on_main_is_not_updated(self):
        self._red_from_base(failing="e2e")
        self.go()
        self.assertNotIn(self.UPDATE, self.gh.writes)
        self.assertIn(("POST", "/repos/%s/actions/runs/77/rerun-failed-jobs" % REPO, None),
                      self.gh.writes)

    def test_a_red_pull_request_up_to_date_with_main_is_not_updated(self):
        self._red_from_base(behind=0)
        self.gh.attempts["77"] = 2
        self.go()
        self.assertNotIn(self.UPDATE, self.gh.writes)
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])

    def test_a_failed_review_never_takes_the_update_path(self):
        self._red_from_base()
        self.gh.runs["sha50"][1]["conclusion"] = "failure"
        self.gh.runs["shamain"].append(
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "success"})
        self.go()
        self.assertNotIn(self.UPDATE, self.gh.writes)
        self.assertEqual(len(self.fixed), 1)

    def test_a_refused_update_of_a_base_red_check_falls_back_to_the_old_path(self):
        self._red_from_base()
        self.gh.update_status = 422
        self.gh.attempts["77"] = 2
        self.go()
        self.assertIn(self.UPDATE, self.gh.writes)
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])

    def test_a_failed_review_is_never_run_again(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "failure",
             "details_url": "https://github.com/%s/actions/runs/78/job/1" % REPO}]
        self.go()
        self.assertEqual(len(self.fixed), 1)
        self.assertFalse(any("rerun" in w[1] for w in self.gh.writes))

    INSTALL_TIMEOUT = ("Setting up build-essential ...\n"
                       "##[error]The action 'Install what the core's own Dockerfile installs' "
                       "has timed out after 10 minutes.")

    def flaky_head(self, log):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"id": 501, "name": "adapter", "status": "completed", "conclusion": "failure",
             "details_url": "https://github.com/%s/actions/runs/88/job/501" % REPO},
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "failure",
             "output": {"title": "FAIL Elder found 1 problem", "summary": "Fix the null check."},
             "details_url": "https://grug.example"}]
        self.gh.logs["501"] = log

    def test_an_infra_timeout_is_run_again_even_when_the_review_also_failed(self):
        # loom#1962: the adapter job timed out installing packages beside a
        # failing review; _run_again refused on the review and the job was
        # never re-run, so the fix round read it as a stuck failure.
        self.flaky_head(self.INSTALL_TIMEOUT)
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertIn(("POST", "/repos/%s/actions/runs/88/rerun-failed-jobs" % REPO, None),
                      self.gh.writes)
        self.assertEqual(self.merged, [])

    def test_an_infra_timeout_on_its_second_attempt_goes_back_as_before(self):
        self.flaky_head(self.INSTALL_TIMEOUT)
        self.gh.attempts["88"] = 2
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertFalse(any("rerun" in w[1] for w in self.gh.writes))

    def test_a_refused_rerun_of_an_infra_timeout_is_judged_as_usual(self):
        self.flaky_head(self.INSTALL_TIMEOUT)
        write = self.gh._write

        def refuse(method, path, body):
            if path.endswith("/rerun-failed-jobs"):
                raise GitHubError("POST %s -> 500" % path, 500)
            return write(method, path, body)
        self.gh._write = refuse
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_a_real_test_failure_beside_a_failed_review_is_not_run_again(self):
        self.flaky_head("FAILED tests/test_a.py::test_x - assert 1 == 2\n##[error]Process completed "
                        "with exit code 1.")
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

    def test_a_prefixed_low_confidence_review_asks_for_improve(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "failure",
             "output": {"title": "Living Hunt 3c63e18b..30d00ebb - WARN Elder review "
                                 "low confidence (deep append)",
                        "summary": "Degraded review (low_confidence_review): not a verdict."},
             "details_url": "https://github.com/%s/actions/runs/81/job/1" % REPO}]
        self.go()
        self.assertEqual(self.fixed, [])
        bodies = [str(c.get("body") or "") for c in self.gh.comments[50]]
        self.assertTrue(any("/grug improve" in body
                            and factory.REVIEW_AGAIN_MARKER % "sha50" in body for body in bodies))

    def test_degraded_review_reads_the_bot_marker_and_titles_anywhere(self):
        self.assertTrue(factory.degraded_review("anything", "Degraded review (timeout): not a verdict."))
        self.assertTrue(factory.degraded_review("Living Hunt a..b - WARN Grug eyes clouded", ""))
        self.assertTrue(factory.degraded_review("WARN Elder review coverage partial", ""))
        self.assertFalse(factory.degraded_review("FAIL Elder found 2 problems", "Fix the null check."))

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

    SPLIT_SUMMARY = (
        "Walked part of the diff.\n\nCoverage: 3/4 cohorts completed.\n\n**Reviewability**\n"
        "- `cross-cohort-proof`: Related implementation and verification changes do not fit in "
        "one bounded proof unit; split the pull request or reduce coupling so behavior and its "
        "evidence can be reviewed together. Paths: `bus/a.py`, `tests/test_a.py`, `bus/b.py`, "
        "`tests/test_b.py`")

    def split_ticket(self, title="WARN Elder review coverage partial", summary=None, **extra):
        body = ("Difficulty: hard\n\n## Why\nx\n\n## Acceptance criteria\n- [ ] A1 first\n"
                "- [ ] A2 second\n\nPart of #7\n")
        self.gh.ticket(5, "pr", body=body)
        self.gh.issues[5]["title"] = "Do the thing"
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "failure",
             "output": {"title": title, "summary": summary or self.SPLIT_SUMMARY},
             "details_url": "https://github.com/%s/actions/runs/80/job/1" % REPO}]

    def test_a_pull_request_the_review_asks_to_split_is_split_into_tickets(self):
        # loom#1218: the fix round sent the same lane back with the same
        # instructions and nothing re-split the work.
        self.split_ticket()
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(len(self.gh.filed), 2)
        for index, made in enumerate(self.gh.filed, start=1):
            self.assertEqual(made["labels"], ["loom:ticket", "loom:queued"])
            self.assertIn("(part %d of 2)" % index, made["title"])
            self.assertIn("Difficulty: hard", made["body"])
            self.assertTrue(made["body"].rstrip().endswith("Part of #7"))
        self.assertIn("`bus/a.py`, `tests/test_a.py`", self.gh.filed[0]["body"])
        self.assertIn("`bus/b.py`, `tests/test_b.py`", self.gh.filed[1]["body"])
        self.assertEqual([c[0] for c in self.closed], [50])
        self.assertIn("#900, #901", self.closed[0][1])
        self.assertEqual(self.gh.pulls[50]["state"], "closed")
        self.assertEqual(self.gh.issues[5]["state"], "closed")
        self.assertIn("loom:closed-unmerged", [x["name"] for x in self.gh.issues[5]["labels"]])
        self.assertEqual(len([c for c in self.gh.comments[5] if "loom:split pr=50" in c["body"]]), 1)

    def sub_issue_links(self):
        return [(w[1].split("/issues/")[1].split("/")[0], w[2]) for w in self.gh.writes
                if w[0] == "POST" and w[1].endswith("/sub_issues")]

    def test_the_parts_of_a_split_go_under_an_epic_with_room_for_all_of_them(self):
        # loom#1294: the parts named the ticket's epic whatever it held, and
        # took it past 15 tickets.
        self.split_ticket()
        with mock.patch.object(factory.brain, "placement_epic",
                                        return_value=60) as placed:
            self.go()
        self.assertEqual(placed.call_args[0][2:], (7, 2))
        self.assertEqual(len(self.gh.filed), 2)
        for made in self.gh.filed:
            self.assertTrue(made["body"].rstrip().endswith("Part of #60"))
            self.assertNotIn("Part of #7", made["body"])
        self.assertEqual(self.sub_issue_links(), [("60", {"sub_issue_id": 5000}),
                                                  ("60", {"sub_issue_id": 5001})])

    def test_the_parts_of_a_split_stay_with_their_epic_when_no_goal_is_found(self):
        self.split_ticket()
        with mock.patch.object(factory.brain, "placement_epic",
                                        side_effect=ValueError("no goal")):
            self.go()
        self.assertEqual(len(self.gh.filed), 2)
        self.assertTrue(all(m["body"].rstrip().endswith("Part of #7") for m in self.gh.filed))
        self.assertEqual(self.sub_issue_links(), [])

    def test_a_split_that_filed_but_did_not_close_is_not_filed_twice(self):
        self.split_ticket()
        self.gh.comments[5].append({"body": "<!-- loom:split pr=50 -->\nsplit into #900, #901.",
                                    "user": ME})
        self.go()
        self.assertEqual(self.gh.filed, [])
        self.assertEqual([c[0] for c in self.closed], [50])

    def test_a_pull_request_with_a_blocking_finding_beside_the_note_still_goes_back(self):
        self.split_ticket(title="Elder markings - 1 blocking, 2 total")
        self.go()
        self.assertEqual(self.gh.filed, [])
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_another_failing_check_beside_the_note_still_goes_back(self):
        self.split_ticket()
        self.gh.runs["sha50"].append({"name": "unittest", "status": "completed",
                                      "conclusion": "failure"})
        self.go()
        self.assertEqual(self.gh.filed, [])
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_a_note_that_names_nothing_to_split_goes_back_for_a_fix_round(self):
        summary = ("**Reviewability**\n- `cross-cohort-proof`: split the pull request. "
                   "Paths: `bus/a.py`, `tests/test_a.py`")
        self.split_ticket(title="Elder markings - 0 blocking, 0 total", summary=summary)
        self.gh.issues[5]["body"] = "Difficulty: hard\n\n## Why\nx\n\nPart of #7\n"
        self.go()
        self.assertEqual(self.gh.filed, [])
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_one_file_pair_is_split_by_acceptance_criterion(self):
        summary = ("**Reviewability**\n- `cross-cohort-proof`: split the pull request. "
                   "Paths: `bus/a.py`, `tests/test_a.py`")
        self.split_ticket(summary=summary)
        self.go()
        self.assertEqual(len(self.gh.filed), 2)
        self.assertIn("A1 first", self.gh.filed[0]["body"].split("## What")[1].split("The whole")[0])
        self.assertEqual([c[0] for c in self.closed], [50])

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
        self.assertTrue(self.gh.pulls[50]["body"].endswith("Superseded by #52."))
        self.assertEqual(self.gh.comments.get(50, []), [])
        self.assertEqual(self.merged, [(52, (REPO, 5))])

    def test_lane_pull_request_closed_when_ticket_merged_through_different_pr(self):
        self.gh.ticket(5, "merged")
        self.gh.pull(51, 5, created=7200, updated=7200, branch="loom/5-some-title-r2",
                     state="closed", merged_at=iso(100), merged=True)
        self.gh.pull(50, 5, created=9000, updated=9000, branch="loom/5-some-title")
        self.go()
        self.assertEqual(self.closed, [(50, "Superseded by #51.")])
        self.assertEqual(self.gh.pulls[50]["state"], "closed")
        self.assertTrue(self.gh.pulls[50]["body"].endswith("Superseded by #51."))
        self.assertEqual(self.gh.comments.get(50, []), [])
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


OTHER = "example-org/other-repo"
LANDING = "loom:landing"


def landing_event(seconds_ago):
    return {"event": "labeled", "label": {"name": LANDING}, "created_at": iso(seconds_ago)}


class TwoRepos:
    """Two fake repos behind one client: OTHER's calls go to the second
    fake with its name read as REPO, so each fake keeps its own state."""

    def __init__(self, one, two):
        self.one, self.two = one, two
        self.requests = 0

    def _route(self, path):
        return (self.two, path.replace(OTHER, REPO)) if OTHER in path else (self.one, path)

    def paginate(self, path):
        gh, path = self._route(path)
        return gh.paginate(path)

    def rest(self, method, path, body=None, etag=None, ok=()):
        gh, path = self._route(path)
        return gh.rest(method, path, body, etag, ok)

    def graphql(self, query, variables=None):
        name = OTHER.split("/")[1]
        gh = (self.two if '"%s"' % name in query or name in (variables or {}).values()
              else self.one)
        return gh.graphql(query, variables)

    def transport(self, method, url, headers, body):
        return self.one.transport(method, url, headers, body)


class LandingLock(Base):
    """loom#2066: one green lane pull request at a time is brought up to
    date and landed in a repo, so main does not move under it."""

    def updates(self, gh=None):
        return [w[1].split("/")[-2] for w in (gh or self.gh).writes
                if w[1].endswith("/update-branch")]

    def carries(self, number, gh=None):
        return LANDING in [x["name"] for x in (gh or self.gh).pulls[number]["labels"]]

    def held_by_50(self, seconds_ago=600, gh=None):
        """PR 50 holds the lock, marked seconds_ago, its checks still running."""
        gh = gh or self.gh
        gh.ticket(5, "pr")
        gh.pull(50, 5, labels=(LANDING,), runs=(("unittest", "in_progress"),))
        gh.events[50] = [landing_event(seconds_ago)]

    def test_of_two_green_behind_pull_requests_only_the_oldest_is_updated_and_marked(self):
        for ticket, number, created in ((5, 50, 7200), (6, 60, 3600)):
            self.gh.ticket(ticket, "pr")
            self.gh.pull(number, ticket, created=created)
            self.gh.behind[number] = 3
        self.go()
        self.assertEqual(self.updates(), ["50"])
        self.assertTrue(self.carries(50))
        self.assertFalse(self.carries(60))
        self.assertEqual(self.merged, [])

    def test_while_a_fresh_lock_is_held_no_other_pull_request_is_updated_or_landed(self):
        self.held_by_50()
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6)
        self.gh.behind[60] = 2
        self.gh.ticket(7, "pr")
        self.gh.pull(70, 7)
        self.go()
        self.assertEqual((self.updates(), self.merged), ([], []))
        self.assertTrue(self.carries(50))

    def test_the_brain_merge_step_refuses_another_pull_request_while_the_lock_is_held(self):
        self.held_by_50()
        self.gh.ticket(7, "pr")
        self.gh.pull(70, 7)
        args = brain.parser().parse_args(["merge", "%s#70" % REPO])
        with mock.patch.object(brain, "merge") as merge, \
                mock.patch("time.time", return_value=NOW):
            with self.assertRaisesRegex(ValueError, "#50 holds the repo's landing lock"):
                brain._cmd_merge(self.gh, args)
            merge.assert_not_called()
            # The holder itself merges, and its lock comes off.
            brain._cmd_merge(self.gh, brain.parser().parse_args(["merge", "%s#50" % REPO]))
        self.assertEqual(merge.call_args.args[1:3], (REPO, 50))
        self.assertFalse(self.carries(50))

    def test_a_lock_40_minutes_old_is_released_and_the_next_one_taken(self):
        self.held_by_50(seconds_ago=40 * 60)
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6)
        self.gh.behind[60] = 2
        self.go()
        self.assertFalse(self.carries(50))
        self.assertEqual(self.updates(), ["60"])
        self.assertTrue(self.carries(60))

    def test_the_landing_pull_request_lands_when_current_and_its_lock_comes_off(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=(LANDING,), created=60)
        self.gh.events[50] = [landing_event(600)]
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6, created=7200)
        self.gh.behind[60] = 2
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertFalse(self.carries(50))
        self.assertEqual(self.updates(), [])

    def test_the_landing_pull_request_behind_again_is_updated_and_keeps_the_lock(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=(LANDING,), created=60)
        self.gh.events[50] = [landing_event(600)]
        self.gh.behind[50] = 1
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6, created=7200)
        self.go()
        self.assertEqual((self.updates(), self.merged), (["50"], []))
        self.assertTrue(self.carries(50))
        self.assertEqual(self.gh.events[50], [landing_event(600)])

    def test_a_landing_pull_request_merged_elsewhere_holds_nothing(self):
        self.gh.ticket(5, "merged")
        self.gh.pull(50, 5, labels=(LANDING,), state="closed", merged_at=iso(60))
        self.gh.events[50] = [landing_event(600)]
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6)
        self.gh.behind[60] = 2
        self.go()
        self.assertEqual(self.updates(), ["60"])
        self.assertTrue(self.carries(60))

    def test_a_lock_in_one_repo_does_not_block_another_repo(self):
        other = FakeGitHub()
        self.held_by_50()
        self.gh.ticket(7, "pr")
        self.gh.pull(70, 7)
        other.ticket(6, "pr")
        other.pull(60, 6, body="Refs %s#6\n" % OTHER, branch="loom/6-some-title")
        both = TwoRepos(self.gh, other)
        merged = []
        f = factory.Factory(both, [REPO, OTHER], NOW, trusted=["Alice-Example"],
                            merge=lambda gh, repo, number, ticket: merged.append((repo, number)),
                            fix=mock.Mock(), move=mock.Mock(), close=mock.Mock(),
                            human=mock.Mock(), place=mock.Mock())
        f.run()
        self.assertEqual(merged, [(OTHER, 60)])
        with mock.patch.object(brain, "merge") as merge, \
                mock.patch("time.time", return_value=NOW):
            brain._cmd_merge(both, brain.parser().parse_args(["merge", "%s#60" % OTHER]))
        self.assertEqual(merge.call_args.args[1:3], (OTHER, 60))


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
        with mock.patch("bus.factory._say") as say:
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
        say.assert_any_call("rewrote 2 flagged line(s) of %s#50's text after the leak scan failed on its text"
                            % REPO)

    def test_a_diff_failure_still_goes_back_to_the_lane(self):
        self.leaky(leak_log(diff_hits=[(10, "private-issue-ref")],
                            text_hits=[(4, "private-issue-ref")]))
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.pr_edits(), [])

    def test_a_repeated_text_failure_is_held_for_the_brain_with_its_location(self):
        self.leaky(BODY_ONLY)
        self.go()
        self.assertEqual(len(self.pr_edits()), 1)
        self.gh.pulls[50]["body"] = LEAK_BODY
        self.go()
        self.assertEqual(len(self.pr_edits()), 1)
        self.assertEqual(self.fixed, [])
        self.assertIn("needs-brain", factory._labels(self.gh.issues[5]))
        notes = [c["body"] for c in self.gh.comments[5]
                 if factory.BODY_LEAK_HOLD_MARKER % "sha50" in c["body"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("line 4 (private-issue-ref)", notes[0])

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


CHIEF = factory.DEFAULT_PLAN_CHECK


def chief_summary(rows):
    """A Grug - Chief check-run summary table; rows are (check, status, detail)."""
    return "\n".join(["| Check | Status | Detail |", "|---|---|---|"] +
                     ["| %s | %s | %s |" % row for row in rows])


EPIC_FAIL = ("which epic", "fail", "#5 belong to no epic - add each as a sub-issue of an epic, "
             "or write `Part of #<epic>` in its body")
BODY_PASS = ("why hunt", "pass", "## Why has 64 words")


class OrphanTicketEpic(Base):
    """A pull request held by Chief only because the ticket it names sits under
    no epic is repaired on the ticket by the station; a lane cannot edit it."""

    def held(self, rows, extra_runs=()):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": CHIEF, "status": "completed", "conclusion": "failure",
             "output": {"title": "Hunt Plan hold", "summary": chief_summary(rows)}},
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed",
             "conclusion": "success"}] + list(extra_runs)

    def test_a_which_epic_only_failure_puts_the_ticket_under_an_epic_and_sends_nothing(self):
        self.held([BODY_PASS, EPIC_FAIL])
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.placed, [5])
        self.assertIn("Part of #77", self.gh.issues[5]["body"])
        note = self.gh.comments[5][-1]["body"]
        self.assertIn(factory.EPIC_REPAIR_MARKER % "sha50", note)
        self.assertIn("#77", note)

    def test_a_failing_body_row_still_goes_back_to_the_lane(self):
        self.held([("what good look like", "fail", "0 bullets"), EPIC_FAIL])
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.placed, [])

    def test_another_failing_check_goes_back_to_the_lane(self):
        self.held([EPIC_FAIL], extra_runs=[
            {"name": "unittest", "status": "completed", "conclusion": "failure"}])
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.placed, [])

    def test_a_ticket_the_pull_request_names_besides_its_own_is_left_to_the_lane(self):
        self.held([("which epic", "fail", "#5, #9 belong to no epic - add each")])
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.placed, [])

    def test_the_repair_runs_once_per_head_and_the_lane_is_still_not_asked(self):
        self.held([EPIC_FAIL])
        self.go()
        self.go()
        self.assertEqual(self.placed, [5])
        self.assertEqual(self.fixed, [])
        self.assertEqual(len([c for c in self.gh.comments[5]
                              if factory.EPIC_REPAIR_MARKER % "sha50" in c["body"]]), 1)

    def test_a_ticket_with_no_epic_to_go_under_is_held_without_a_lane_round(self):
        self.held([EPIC_FAIL])
        with mock.patch.object(factory, "_say"):
            f = factory.Factory(self.gh, [REPO], NOW, trusted=["Alice-Example"],
                                fix=lambda *a, **k: self.fixed.append(a),
                                place=mock.Mock(side_effect=ValueError("no open goal home")))
            f.run()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.gh.comments[5], [])

    def test_the_summary_reader_names_the_tickets_of_a_lone_which_epic_failure(self):
        self.assertEqual(factory.chief_orphans(chief_summary([BODY_PASS, EPIC_FAIL])), [5])
        self.assertEqual(factory.chief_orphans(chief_summary(
            [("which epic", "fail", "#5, #9 belong to no epic - add")])), [5, 9])
        self.assertIsNone(factory.chief_orphans(chief_summary(
            [("how big", "fail", "no Size"), EPIC_FAIL])))
        self.assertIsNone(factory.chief_orphans(chief_summary([BODY_PASS])))
        self.assertIsNone(factory.chief_orphans(chief_summary(
            [("which epic", "warn", "#5 belong to no epic")])))
        self.assertIsNone(factory.chief_orphans(""))

    def test_place_under_epic_files_the_ticket_under_the_goals_intake_epic(self):
        issue = {"number": 5, "id": 55, "body": "x"}
        with mock.patch.object(brain, "repo_goal", return_value=30) as goal, \
                mock.patch.object(brain, "intake_epic", return_value=77) as intake, \
                mock.patch.object(brain, "file_under_epic") as file:
            self.assertEqual(brain.place_under_epic(self.gh, REPO, issue), 77)
        goal.assert_called_once_with(self.gh, REPO)
        intake.assert_called_once_with(self.gh, REPO, 30)
        file.assert_called_once_with(self.gh, REPO, 77, issue)

    def test_place_under_epic_refuses_a_repo_with_no_goal(self):
        with mock.patch.object(brain, "repo_goal", return_value=None):
            with self.assertRaises(ValueError):
                brain.place_under_epic(self.gh, REPO, {"number": 5})


BOT = {"__typename": "Bot", "login": "grug-tribe"}
FIXED = "Fixed in 1234567: the flagged code changed and this review does not raise it again."
NO_CHANGE = "Lane claude made no change for the fix to #50 and said why on the pull request"


PLAN_TICKET = """Difficulty: medium

## Why
The page query is slow for every visitor on the home page.

## What
Cache the page query.

## Acceptance
- [ ] The query runs once per minute.
- [ ] A stale entry is dropped.
- [ ] A test fails without the cache.

## Size
**Size:** S

## Out of scope
- Other pages.
"""

# The body the runner wrote for PLAN_TICKET before #1838 and the size fix:
# an empty criteria section and the size's label as its letter (two lane
# pull requests in other repos, 2026-10-25).
PLAN_BODY = """Intent: logic.

## Why
The page query is slow for every visitor on the home page.

## Summary
Cache the page query.

## Acceptance criteria


Size: **Size:**

## Out of scope
- Other pages.

## Operator steps before merge
none stated

---
Loom ticket example-org.example-repo-5, lane codex, model gpt-6-luna.
Refs %s""" % ("%s#%d" % (REPO, 5))

BODY_FAIL = ("what good look like", "fail", "## Acceptance criteria has 0 non-empty bullets")
SIZE_FAIL = ("how big", "fail", "no Size: XS/S/M/L/XL in body")


class PlanBodyRepair(Base):
    """loom#1837: a pull request held by the plan check on its body alone is
    rebuilt from its ticket, never sent to a lane whose agent edits code."""

    def held(self, ticket_body=PLAN_TICKET, chief_done=None, extra_runs=()):
        self.gh.ticket(5, "pr", body=ticket_body)
        self.gh.pull(50, 5, green=False, body=PLAN_BODY)
        chief = {"name": CHIEF, "status": "completed", "conclusion": "failure",
                 "output": {"title": "Hunt Plan hold",
                            "summary": chief_summary([BODY_FAIL, SIZE_FAIL])}}
        if chief_done is not None:
            chief["completed_at"] = chief_done
        self.gh.runs["sha50"] = [chief, {"name": factory.DEFAULT_REVIEW_CHECK,
                                         "status": "completed", "conclusion": "success"}
                                 ] + list(extra_runs)

    def body_edits(self):
        return [w for w in self.gh.writes if w[0] == "PATCH" and w[1].endswith("/pulls/50")]

    def notes(self):
        return [c for c in self.gh.comments[5]
                if c["body"].startswith(pr_repair.PLAN_NOTE_MARKER)]

    def test_a_body_only_failure_rebuilds_the_body_from_the_ticket_and_sends_no_round(self):
        self.held()
        self.go()
        self.assertEqual(self.fixed, [])
        body = self.gh.pulls[50]["body"]
        self.assertEqual(hunt_plan.problems(body), [])
        self.assertIn("- [ ] A stale entry is dropped.", body)
        self.assertIn("\nSize: S\n", body)
        self.assertTrue(body.startswith("Intent: logic."))
        self.assertEqual(len(self.body_edits()), 1)
        events = [c["body"] for c in status.expand(self.gh.comments[5], [ME["login"]])]
        self.assertTrue(any(factory.PLAN_REPAIR_MARKER % "sha50" in e for e in events))
        # Kept on the status comment: one comment, no new one per attempt.
        self.assertEqual(len(self.gh.comments[5]), 1)

    def test_the_repair_runs_once_per_head_then_waits_for_the_check_to_read_it_again(self):
        self.held()
        self.go()
        self.gh.pulls[50]["body"] = PLAN_BODY  # as if the edit had not reached the check
        self.go()
        self.assertEqual(len(self.body_edits()), 1)
        self.assertEqual(self.fixed, [])
        self.assertEqual(len(self.gh.comments[5]), 1)

    def test_a_check_that_read_the_repaired_body_and_still_fails_goes_the_usual_way(self):
        self.held()
        self.go()
        self.gh.runs["sha50"][0]["completed_at"] = iso(-60)
        self.go()
        self.assertEqual(len(self.body_edits()), 1)
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_a_ticket_that_cannot_fill_the_body_is_held_with_one_note_and_no_round(self):
        self.held(ticket_body=PLAN_TICKET.replace("## Acceptance\n", "## Notes\n"))
        self.go()
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.body_edits(), [])
        self.assertIn("needs-brain", factory._labels(self.gh.issues[5]))
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("## Acceptance criteria", notes[0]["body"])
        self.assertNotIn("## Size", notes[0]["body"])  # the ticket fills the size
        self.assertEqual(len(self.gh.comments[5]), 1)

    def test_the_hold_lifts_once_the_ticket_gains_what_it_lacked(self):
        self.held(ticket_body=PLAN_TICKET.replace("## Acceptance\n", "## Notes\n"))
        self.go()
        self.gh.issues[5]["body"] = PLAN_TICKET
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(hunt_plan.problems(self.gh.pulls[50]["body"]), [])
        self.assertNotIn("needs-brain", factory._labels(self.gh.issues[5]))
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("hold is lifted", notes[0]["body"])

    def test_a_needs_brain_set_for_another_reason_stays_when_the_hold_lifts(self):
        # Security review: the plan hold takes off only a needs-brain it
        # added itself, which its note records.
        self.held(ticket_body=PLAN_TICKET.replace("## Acceptance\n", "## Notes\n"))
        self.gh.issues[5]["labels"].append({"name": "needs-brain"})
        self.go()
        self.assertNotIn("set-needs-brain", self.notes()[0]["body"])
        self.gh.issues[5]["body"] = PLAN_TICKET
        self.go()
        self.assertEqual(hunt_plan.problems(self.gh.pulls[50]["body"]), [])
        self.assertIn("needs-brain", factory._labels(self.gh.issues[5]))
        self.assertIn("hold is lifted", self.notes()[0]["body"])

    def test_the_runners_note_is_edited_not_posted_again(self):
        self.held(ticket_body=PLAN_TICKET.replace("## Acceptance\n", "## Notes\n"))
        self.gh.comments[5].append({"id": 77, "body": pr_repair.PLAN_NOTE_MARKER +
                                    "\nThe runner's words.", "user": ME, "created_at": iso(60)})
        self.go()
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("Held for the brain", notes[0]["body"])

    def test_another_failing_check_still_goes_back_to_the_lane(self):
        self.held(extra_runs=[{"name": "unittest", "status": "completed",
                               "conclusion": "failure"}])
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.body_edits(), [])


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

    def test_a_thread_whose_bot_root_comment_starts_with_fixed_in_and_has_no_later_comment_is_resolved(self):
        self.gh.ticket(5, "pr", lane="codex")
        self.gh.comments[5].append({"body": FIX_ASKED.replace("### Check: lint\n\n    E1\n\n", ""),
                                    "user": ME})
        self.gh.pull(50, 5, resolved=False)
        self.thread((BOT, "Fixed in abc1234: the flagged code changed and this review clears it."))
        self.go()
        self.assertIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual((self.fixed, factory.rung_of(self.gh.comments[5])), ([], 0))

    def test_an_edited_bot_root_comment_with_a_later_person_comment_stays_open(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False)
        self.thread((BOT, "Fixed in abc1234: finding"), (ME, "this still breaks when x is empty"))
        self.go()
        self.assertNotIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual(self.merged, [])

    def test_an_edited_bot_root_comment_with_a_later_lane_comment_stays_open(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False)
        self.thread((BOT, "Fixed in abc1234: finding"),
                    (ME, "Lane codex %s already guarded" % factory.ANSWER_REPLY))
        self.go()
        self.assertNotIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual(self.merged, [])

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

    def stuck_case(self, author, lane="codex", model=None):
        """Ticket 5's last round asked about a finding at a.py:3; the open
        thread, under a new id, holds the same rule at a.py:5."""
        self.gh.ticket(5, "pr", lane=lane)
        if model:
            self.gh.issues[5]["body"] = "Model: %s\n" % model + self.gh.issues[5]["body"]
        self.gh.comments[5].append({"body": FIX_MARKER + "\nPull request: x#50\n\n"
                                    "### Thread PRRT_old (comment 1) at a.py:3\n\n"
                                    "    grug-tribe: high | `rule-x` | quick win", "user": ME})
        self.gh.pull(50, 5, resolved=False)
        self.thread((author, "high | `rule-x` | quick win"))
        self.gh.details[50]["reviewThreads"]["nodes"][0].update(path="a.py", line=5)
        self.go()

    def test_the_review_bots_finding_posted_again_climbs_the_ladder(self):
        self.stuck_case(BOT)
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 1)

    def test_a_stuck_send_back_past_the_last_rung_goes_to_the_hand_off_pool(self):
        self.stuck_case(BOT, lane="claude", model="claude-opus-5-5")
        self.assertIn((5, "failed", None), self.moved)
        self.assertIn(factory.HANDOFF_LABEL, [x["name"] for x in self.gh.issues[5]["labels"]])
        self.assertEqual(self.fixed, [])

    def test_a_strangers_thread_at_the_same_place_never_counts_toward_stuck(self):
        # On a public repo anyone can open a thread: one copying a finding's
        # path, line and rule must not climb the ticket or stop its rounds.
        self.stuck_case({"__typename": "User", "login": "a-stranger"})
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 0)
        self.assertEqual(self.lane(5), "codex")

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


class NoChangeRounds(Base):
    def rounds(self, *kinds):
        for n, kind in enumerate(kinds):
            body = FIX_ASKED if kind == "fix" else NO_CHANGE
            self.gh.comments[5].append({"body": body, "user": ME,
                                        "created_at": iso(20000 - 1000 * n)})

    def setUp(self):
        super().setUp()
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.pull(50, 5, resolved=False, green=False)

    def test_two_no_change_rounds_on_one_check_hand_the_ticket_to_a_person(self):
        self.rounds("fix", "none", "fix", "none")
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(len(self.humans), 1)
        self.assertIn("`lint`", self.humans[0][1])
        self.assertTrue(self.humans[0][1].startswith("Kind: decision"))

    def test_a_second_pass_neither_sends_a_round_nor_repeats_the_comment(self):
        self.rounds("fix", "none", "fix", "none")
        self.go()
        count = len(self.gh.comments[5])
        self.go()
        self.assertEqual((len(self.humans), self.fixed), (1, []))
        self.assertEqual(len(self.gh.comments[5]), count)

    def test_one_no_change_round_still_sends_the_next_round_up_the_ladder(self):
        self.rounds("fix", "none")
        self.go()
        self.assertEqual((len(self.fixed), self.humans), (1, []))
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 1)

    def test_a_round_that_changed_something_resets_the_count(self):
        self.rounds("fix", "none", "fix", "fix", "none")
        self.go()
        self.assertEqual(self.humans, [])

    def test_a_check_a_later_round_does_not_name_loses_its_count(self):
        other = FIX_ASKED.replace("### Check: lint", "### Check: unit")
        self.rounds("fix", "none")
        self.gh.comments[5] += [{"body": other, "user": ME, "created_at": iso(18000)},
                                {"body": NO_CHANGE, "user": ME, "created_at": iso(17000)}]
        self.assertEqual(factory.no_change_rounds(self.gh.comments[5]), {"unit": 1})


MEDIUM = "\U0001f7e0 medium | _robustness_ | `missing-error-handling` | quick win\n\nGrug see a gap."
HIGH = "\U0001f525 high | _correctness_ | `null-deref` | quick win\n\nGrug see a crash."
ASKED_ONCE = FIX_MARKER + "\nPull request: x#50\n\n### Thread PRRT_1 (comment 1) at a.py:1\n\n    gap"
ASKED_OTHER = FIX_MARKER + "\nPull request: x#50\n\n### Thread PRRT_9 (comment 9) at a.py:9\n\n    other"


class DeferredFindings(Base):
    def setUp(self):
        super().setUp()
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False)

    def settle(self, merged=True, outdated=False):
        """The pass after the pull request closed: the ticket is merged now."""
        self.gh.pulls[50].update(state="closed", merged=merged, merged_at=iso(0) if merged else None)
        self.gh.details[50]["reviewThreads"]["nodes"][0]["isOutdated"] = outdated
        self.gh.issues[5]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:merged"}]
        self.go()

    def finding(self, body, author=BOT, asked=(ASKED_ONCE,)):
        root = {"author": author, "body": body, "url": "https://example.invalid/pr/50#r1"}
        self.gh.details[50]["reviewThreads"]["nodes"][0].update(
            path="bus/a.py", comments={"nodes": [root]}, root={"nodes": [root]})
        self.gh.comments[5] += [{"body": b, "user": ME} for b in asked]

    def test_a_medium_finding_a_round_asked_for_goes_to_a_follow_up_and_the_pull_request_lands(self):
        self.finding(MEDIUM)
        self.go()
        # The pull request has not merged yet: nothing is filed until it does.
        self.assertEqual(self.gh.filed, [])
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)
        filed = self.gh.filed[0]
        self.assertEqual((filed["title"], filed["labels"]),
                         ("Settle the review findings left from PR #50", ["loom:triage"]))
        self.assertIn("- medium | _robustness_ | `missing-error-handling` | quick win in `bus/a.py`:"
                      " https://example.invalid/pr/50#r1", filed["body"])
        self.assertTrue(all(ord(ch) < 128 for ch in filed["body"]))
        replies = [w for w in self.gh.writes if w[0] == "REPLY"]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0][2].startswith("Deferred: "))
        self.assertIn(("RESOLVE", "PRRT_1", None), self.gh.writes)
        self.assertEqual((self.merged, self.fixed), ([(50, (REPO, 5))], []))

    def test_a_new_medium_finding_on_the_first_round_is_deferred_not_sent_back(self):
        self.finding(MEDIUM, asked=(ASKED_OTHER,))
        self.go()
        self.assertEqual((self.gh.filed, self.merged, self.fixed), ([], [(50, (REPO, 5))], []))
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)

    def test_a_high_finding_on_a_logic_pull_request_still_goes_back(self):
        self.gh.pulls[50]["labels"] = [{"name": "class:logic"}]
        self.finding(HIGH, asked=())
        self.go()
        self.assertEqual((self.gh.filed, self.merged, len(self.fixed)), ([], [], 1))

    def test_a_revert_defers_a_medium_finding_without_a_fix_round(self):
        self.gh.issues[5]["labels"].append({"name": "loom:revert"})
        self.finding(MEDIUM, asked=())
        self.go()
        self.assertEqual((self.gh.filed, self.merged, self.fixed), ([], [(50, (REPO, 5))], []))
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)

    def test_a_revert_still_goes_back_for_a_high_finding(self):
        self.gh.issues[5]["labels"].append({"name": "loom:revert"})
        self.finding(HIGH, asked=())
        self.go()
        self.assertEqual((self.gh.filed, self.merged, len(self.fixed)), ([], [], 1))

    def test_after_two_fix_rounds_a_new_medium_finding_is_deferred(self):
        self.finding(MEDIUM, asked=(ASKED_OTHER, ASKED_OTHER))
        self.go()
        self.assertEqual((self.gh.filed, self.merged), ([], [(50, (REPO, 5))]))
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)

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
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)

    def test_an_open_follow_up_from_an_earlier_pass_is_reused(self):
        self.gh.filed.append({"number": 777, "title": "Deferred review findings from %s#50" % REPO,
                              "user": ME})
        self.finding(MEDIUM)
        self.go()
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_a_docs_pull_request_defers_a_medium_finding_at_once(self):
        self.gh.pulls[50]["labels"] = [{"name": "class:docs"}]
        self.finding(MEDIUM, asked=())
        self.go()
        self.assertEqual((self.gh.filed, self.merged), ([], [(50, (REPO, 5))]))
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)
        self.assertEqual(self.fixed, [])

    def test_a_tests_pull_request_defers_a_low_finding_at_once(self):
        self.gh.pulls[50]["labels"] = [{"name": "class:tests"}]
        self.finding("low | _style_ | `nit`", asked=())
        self.go()
        self.assertEqual((self.gh.filed, self.merged), ([], [(50, (REPO, 5))]))
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)

    def test_a_logic_pull_request_defers_a_minor_finding_without_a_fix_round(self):
        # 2026-10-14: medium findings no longer cost a fix round on any class.
        self.gh.pulls[50]["labels"] = [{"name": "class:logic"}]
        self.finding(MEDIUM, asked=())
        self.go()
        self.assertEqual((self.gh.filed, self.merged, self.fixed), ([], [(50, (REPO, 5))], []))
        self.settle()
        self.assertEqual(len(self.gh.filed), 1)

    def test_nothing_is_filed_while_the_pull_request_is_open(self):
        self.finding(MEDIUM)
        self.go()
        self.go()
        self.assertEqual((self.gh.filed, len(self.merged)), ([], 2))

    def test_nothing_is_filed_when_the_pull_request_closed_unmerged(self):
        self.finding(MEDIUM)
        self.go()
        self.settle(merged=False)
        self.assertEqual(self.gh.filed, [])

    def test_a_finding_the_merged_head_outdated_is_not_filed(self):
        self.finding(MEDIUM)
        self.go()
        self.settle(outdated=True)
        self.assertEqual(self.gh.filed, [])
        self.assertTrue(any(factory.DEFERRED_FILED_MARKER % 50 in c["body"]
                            for c in self.gh.comments[5]))

    def test_the_follow_up_is_filed_once(self):
        self.finding(MEDIUM)
        self.go()
        self.settle()
        self.go()
        self.assertEqual(len(self.gh.filed), 1)

    def test_closing_a_pull_request_unmerged_closes_its_open_follow_up(self):
        self.gh.pull(60, 5, state="closed")
        self.gh.pull(61, 5, state="closed", merged_at=iso(0))
        self.gh.pull(62, 5)
        for n in (60, 61, 62):
            self.gh.filed.append({"number": 700 + n, "state": "open", "user": ME, "body": "x",
                                  "title": "Settle the review findings left from PR #%d" % n})
        self.go()
        by = {f["number"]: f.get("state") for f in self.gh.filed}
        self.assertEqual(by, {760: "closed", 761: "open", 762: "open"})
        self.assertEqual(self.gh.filed[0]["state_reason"], "not_planned")
        self.assertIn("closed without merging", self.gh.filed[0]["body"])


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

    def test_a_docs_pull_request_that_gained_a_sensitive_file_is_left_for_the_brain(self):
        # 2026-10-27: opened as class:docs, a fix round added code; the label stayed docs.
        self.gh.contents[".loom/sensitive-paths"] = "bus/brain.py\n"
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=("class:docs",), files=("docs/a.md", "bus/brain.py"))
        self.go()
        self.assertEqual(self.merged, [])
        names = [x["name"] for x in self.gh.pulls[50]["labels"]]
        self.assertEqual([n for n in names if n.startswith("class:")], ["class:sensitive"])
        self.assertIn("needs-brain", names)

    def test_a_docs_pull_request_that_gained_code_is_relabeled_logic(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=("class:docs",), files=("docs/a.md", "bus/a.py", "tests/test_a.py"))
        self.go()
        names = [x["name"] for x in self.gh.pulls[50]["labels"]]
        self.assertEqual([n for n in names if n.startswith("class:")], ["class:logic"])

    def test_a_class_is_never_lowered_from_the_files(self):
        # No sensitive-paths file read means no sensitive class from the files;
        # the label the lane set stands.
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=("class:sensitive", "needs-brain"))
        self.go()
        names = [x["name"] for x in self.gh.pulls[50]["labels"]]
        self.assertIn("class:sensitive", names)
        self.assertNotIn("class:logic", names)

    def test_a_sensitive_pull_request_already_marked_is_left_alone(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=("class:sensitive", "needs-brain"))
        self.go()
        self.assertEqual([w for w in self.gh.writes if w[1].endswith("/labels")], [])
        self.assertEqual(self.merged, [])


class SendBackNeedsAFinding(Base):
    """loom#1389: a lane pull request goes back only with a new finding."""

    def test_a_ticket_held_by_needs_brain_is_not_sent_back(self):
        self.gh.ticket(5, "pr")
        self.gh.issues[5]["labels"].append({"name": "needs-brain"})
        self.gh.pull(50, 5, green=False)
        f = self.go()
        self.assertEqual((self.fixed, self.moved, self.merged), ([], [], []))
        self.assertEqual(f.actions, 0)

    def test_a_ticket_held_by_needs_brain_is_not_sent_back_for_threads_or_a_conflict(self):
        self.gh.ticket(5, "pr")
        self.gh.issues[5]["labels"].append({"name": "needs-brain"})
        self.gh.pull(50, 5, resolved=False, mergeable=False)
        self.go()
        self.assertEqual(self.fixed, [])

    def test_a_failure_among_same_named_runs_sends_the_pull_request_back(self):
        # Three workflows each have a job of this name; the failed one is not
        # the newest by name (2026-10-21, a pull request in the deploy repo).
        name = "trust gate (collaborator-only on self-hosted)"
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=True)
        self.gh.runs["sha50"] += [
            {"id": 1, "name": name, "status": "completed", "conclusion": "success",
             "started_at": iso(100), "check_suite": {"id": 1}},
            {"id": 2, "name": name, "status": "completed", "conclusion": "failure",
             "started_at": iso(101), "check_suite": {"id": 2}},
            {"id": 3, "name": name, "status": "completed", "conclusion": "success",
             "started_at": iso(102), "check_suite": {"id": 3}}]
        self.go()
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])

    def test_pr_faults_reads_the_reduced_runs_beside_the_rollup(self):
        green = {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                 "reviewThreads": {"nodes": []}}
        failed = [{"name": "a", "conclusion": "failure"}]
        self.assertEqual(factory.pr_faults(green, {"mergeable": True}), [])
        self.assertEqual(factory.pr_faults(green, {"mergeable": True}, [{"name": "a", "conclusion": "success"}]), [])
        self.assertEqual(factory.pr_faults(green, {"mergeable": True}, failed), ["failing checks"])

    def test_a_send_back_brain_fix_refused_is_not_counted(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        refused = []
        def fix(gh, repo, number, ticket, notes, lane=None, model=None, authors=None):
            refused.append(number)
            return False
        f = factory.Factory(self.gh, [REPO], NOW, trusted=["Alice-Example"], fix=fix,
                            merge=lambda *a: None, move=lambda *a, **k: None)
        f.run()
        self.assertEqual(refused, [50])
        self.assertEqual(f.actions, 0)


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

    def test_a_queued_ticket_that_waits_says_why_in_waits(self):
        # The factory's reason for leaving a ticket where it is reached only
        # the log; nothing saw the same reason repeat for hours (2026-10-21).
        self.queued(7, body="Model: claude-opus-5-5\nDifficulty: hard\n\n## Why\nx\n")
        self.queued(8)
        self.queued(9, body="Not before: 2099-01-01T00:00:00Z\nDifficulty: medium\n\n## Why\nx\n")
        self.queued(10, body="Model: <b>`x`\nDifficulty: medium\n\n## Why\nx\n")
        worker = self.assign(available=[])
        self.assertEqual(worker.waits, {
            "%s#7" % REPO: "no free lane takes model claude-opus-5-5",
            "%s#8" % REPO: "no free lane in tier medium",
            "%s#10" % REPO: "no free lane takes model (its Model: line names no model id)"})

    def test_a_queued_ticket_held_by_after_gets_no_lane_and_the_next_one_does(self):
        # The factory briefed a queued ticket whose After: ticket had not
        # merged; the station never claimed it, and the lane it reserved sat
        # idle for hours with that one ticket counted against it (2026-10-22).
        self.gh.ticket(5, "pr")
        self.queued(7, body="After: #5\nDifficulty: medium\n\n## Why\nx\n", age=7200)
        self.queued(8, age=0)
        worker = self.assign(available=["codex"])
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")
        self.assertEqual(worker.waits["%s#7" % REPO], "waits for #5 to merge (After:)")
        self.assertEqual((factory._state(self.gh.issues[8]), self.lane(8)), ("briefed", "codex"))

    def test_a_queued_ticket_whose_after_ticket_merged_gets_a_lane(self):
        self.gh.ticket(5, "merged")
        self.queued(7, body="After: #5\nDifficulty: medium\n\n## Why\nx\n")
        self.assign(available=["codex"])
        self.assertEqual((factory._state(self.gh.issues[7]), self.lane(7)), ("briefed", "codex"))

    def first_label_ticket(self, number, label="loom:regression", model="gpt-6-luna"):
        self.queued(number, body="Model: %s\nDifficulty: medium\n\n## Why\nx\n" % model)
        self.gh.issues[number]["labels"].append({"name": label})

    def busy(self, number, lane):
        self.gh.ticket(number, "running", lane=lane)

    def test_a_first_label_model_ticket_is_briefed_onto_a_busy_up_lane(self):
        # loom#1300, 2026-10-21: both lanes of the model always had another
        # fix round waiting, so a pinned regression ticket was never briefed.
        self.first_label_ticket(7)
        self.busy(8, "codex")
        self.busy(9, "codex-2")
        self.assign(available=["codex", "codex-2"])
        self.assertEqual(self.lane(7), "codex")
        self.assertEqual(factory._state(self.gh.issues[7]), "briefed")

    def test_a_first_label_model_ticket_skips_a_lane_limited_for_the_model(self):
        self.first_label_ticket(7)
        self.busy(8, "codex")
        self.busy(9, "codex-2")
        worker = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"],
                                 available=["codex", "codex-2"],
                                 model_limits={"codex": {"gpt-6-luna": NOW + 3600}})
        worker.assign_queued()
        self.assertEqual(self.lane(7), "codex-2")

    def test_a_first_label_model_ticket_skips_lanes_where_the_model_is_benched(self):
        # loom#969: a model benched for repeated failure is not picked, and a
        # bench of another provider's model does not touch these lanes.
        self.first_label_ticket(7)
        self.busy(8, "codex")
        self.busy(9, "codex-2")
        worker = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"],
                                 available=["codex", "codex-2"],
                                 benched=[("codex", "gpt-6-luna")])
        worker.assign_queued()
        self.assertEqual(worker.waits["%s#7" % REPO], "no free lane takes model gpt-6-luna")
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")
        worker = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"],
                                 available=["codex", "codex-2"],
                                 benched=[("codex-2", "gpt-6-luna")])
        worker.assign_queued()
        self.assertEqual(self.lane(7), "codex")

    def test_a_first_label_model_ticket_waits_when_its_lanes_are_limited_or_down(self):
        self.first_label_ticket(7)
        self.busy(8, "codex")
        self.busy(9, "codex-2")
        for kwargs in ({"limited": ["codex", "codex-2"], "available": ["codex", "codex-2"]},
                       {"available": []}):
            worker = self.assign(**kwargs)
            self.assertEqual(worker.waits["%s#7" % REPO], "no free lane takes model gpt-6-luna")
            self.assertEqual(factory._state(self.gh.issues[7]), "queued")

    def test_a_model_ticket_without_a_first_label_waits_for_a_free_lane(self):
        self.queued(7, body="Model: gpt-6-luna\nDifficulty: medium\n\n## Why\nx\n")
        self.busy(8, "codex")
        self.busy(9, "codex-2")
        worker = self.assign(available=["codex", "codex-2"])
        self.assertEqual(worker.waits["%s#7" % REPO], "no free lane takes model gpt-6-luna")
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")

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

    def test_opus_pin_never_goes_to_agy_when_every_anthropic_lane_is_out(self):
        # Until 2026-10-21 it moved to agy on gemini-4-argon-high (EQUIVALENTS);
        # agy now runs gemini-3.8-flash only, no Opus equivalent.
        self.gh.private = False
        self.queued(7, body=self.OPUS)
        self.gh.comments[7] = [{"body": factory.RUNG_MARKER % 3, "user": ME}]
        self.assign(limited=self.OUT)
        self.assertEqual((factory._state(self.gh.issues[7]), self.lane(7), self.model(7)),
                         ("queued", "", "claude-opus-5-5"))

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
        # A free agy never takes it: agy runs gemini-3.8-flash only.
        self.gh.issues[30]["labels"][1] = {"name": "loom:merged"}
        self.assign()
        self.assertEqual((self.lane(7), self.model(7)), ("", "claude-opus-5-5"))

    def test_future_not_before_stays_queued(self):
        self.queued(7, body="Not before: 2099-01-01T00:00:00Z\nDifficulty: medium\n\n## Why\nx\n")
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

    def test_a_queued_ticket_with_no_difficulty_takes_a_medium_lane(self):
        # A queued ticket whose body had no Difficulty line was in no tier, so
        # no lane could ever take it; one waited a day (2026-10-21).
        for body in ("Priority: P2\n\n## Why\nx\n", "Difficulty: Medium-ish\n\n## Why\nx\n"):
            with self.subTest(body=body):
                self.queued(7, body=body)
                self.assign(available=["codex"])
                self.assertEqual(self.lane(7), "codex")

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
    def test_a_ticket_in_the_hand_off_pool_is_never_rebriefed(self):
        # Its fix rounds stopped on the same failures: the second operator's
        # session has it, so the factory leaves it failed.
        self.gh.ticket(6, "failed", lane="spark")
        self.gh.issues[6]["labels"].append({"name": factory.HANDOFF_LABEL})
        self.gh.pull(60, 6, runs=(("unittest", "in_progress"),))
        self.go()
        self.assertEqual(self.moved, [])
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.lane(6), "spark")

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

    def ended(self, number, text, lane="zai"):
        """The ticket's claim on `lane` and the ending that closed it."""
        at = iso(3600 - 60 * len(self.gh.comments[number]))
        self.gh.comments[number] += [
            {"body": "Claimed for lane %s (body sha256 0123456789ab)" % lane, "user": ME,
             "created_at": at},
            {"body": text, "user": ME, "created_at": at}]

    def requeued(self, number):
        labels = [x["name"] for x in self.gh.issues[number]["labels"]]
        return ("loom:queued" in labels and "loom:failed" not in labels
                and not [x for x in labels if x.startswith("lane:")])

    def notes(self, number):
        return [c["body"] for c in status.expand(self.gh.comments[number], [ME["login"]])]

    def test_a_failed_ticket_does_not_climb_for_a_transient_limit_or_factory_ending(self):
        # 43 of 227 climbs since 2026-10-14 followed such an ending (loom#1395).
        for n, text in ((6, "Lane zai failed: [transient] could not mint a GitHub App token. "
                            "Not retried: 2 of 2 automatic retries used."),
                        (7, "Lane zai failed: [limit] agent exited 3: provider limit reached"),
                        (8, "Lane zai failed: [factory] held: validation incident"),
                        (9, "Lane zai failed: [auth] the key was refused"),
                        (10, "Lane zai stopped: [transient] agent exited 1")):
            self.gh.ticket(n, "failed", lane="zai")
            self.ended(n, text)
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))
        for n in range(6, 11):
            self.assertTrue(self.requeued(n), n)
            self.assertIsNone(self.model(n))  # its Model: line is untouched
            self.assertFalse([c for c in self.gh.comments[n] if factory.RUNG_RE.search(c["body"])])
            self.assertTrue([b for b in self.notes(n) if b.startswith("Not a failure")], n)

    def test_a_failed_ticket_still_climbs_for_a_work_ending(self):
        for n, text in ((6, "Lane zai failed: [work] validation failed: the check exited 2"),
                        (7, "Lane zai failed: agent exited 1")):
            self.gh.ticket(n, "failed", lane="zai")
            self.ended(n, text)
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude"), (7, "briefed", "claude")])

    def test_only_the_newest_ending_decides_whether_a_failed_ticket_climbs(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.ended(6, "Lane zai failed: [transient] agent exited 1")
        self.ended(6, "Lane zai failed: [work] validation failed: the check exited 2")
        self.gh.ticket(7, "failed", lane="zai")
        self.ended(7, "Lane zai failed: [work] validation failed: the check exited 2")
        self.ended(7, "Lane zai stopped: [limit] agent exited 3: provider limit reached")
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude")])
        self.assertTrue(self.requeued(7))

    def test_a_failed_ticket_with_a_pull_request_and_a_transient_ending_goes_back_to_pr(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.gh.pull(60, 6, runs=(("unittest", "in_progress"),))
        self.ended(6, "Lane zai failed: [transient] agent exited 1")
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))
        labels = [x["name"] for x in self.gh.issues[6]["labels"]]
        self.assertIn("loom:pr", labels)
        self.assertNotIn("loom:failed", labels)
        self.assertIn("lane:zai", labels)

    def test_a_transient_ending_does_not_free_a_ticket_left_past_the_ladder(self):
        # _give_up leaves such a ticket failed; moving it back would only
        # have the next pass fail it again.
        self.gh.ticket(6, "failed", lane="zai")
        self.ended(6, "Lane zai failed: [transient] agent exited 1")
        self.gh.comments[6].append({"body": factory.GAVE_UP + " (x; ran on none).", "user": ME})
        self.go()
        self.assertFalse(self.requeued(6))
        self.assertIn({"name": "loom:failed"}, self.gh.issues[6]["labels"])

    def test_a_runaway_ticket_with_a_transient_ending_stays_held(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.gh.issues[6]["labels"].append({"name": "loom:runaway"})
        self.ended(6, "Lane zai failed: [transient] agent exited 1")
        self.go()
        self.assertFalse(self.requeued(6))

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

    def test_a_change_a_submodule_needed_is_held_for_the_brain_not_climbed(self):
        # The runner refuses edits inside a submodule (loom#1316): the change
        # belongs in the submodule's own repo, and a stronger model cannot push
        # it either. Three tickets needed such a split on 2026-10-21.
        self.gh.ticket(6, "failed", lane="zai")
        self.gh.comments[6] += [self.ZAI_CLAIM, {"body": (
            "Lane zai failed: [work] the change edits files inside submodule mod-overseer "
            "(mod-overseer/src/mod_overseer.cpp), which a push of this repo does not carry; "
            "that repo needs the change first. nothing pushed"), "user": ME}]
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))
        self.assertIn({"name": "needs-brain"}, self.gh.issues[6]["labels"])
        note = self.gh.comments[6][-1]["body"]
        self.assertIn("submodule mod-overseer", note)
        self.assertIn("split", note)
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

    def test_a_failed_ticket_past_the_last_rung_closes_its_lane_pull_request(self):
        # A ticket that ran every rung is left loom:failed for the brain, but
        # its open lane pull request is closed so the lane does not stall on it.
        self.gh.ticket(7, "failed")
        self.gh.comments[7] += ALL_RUNGS
        self.gh.pull(70, 7, resolved=False)
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))
        self.assertEqual(len(self.closed), 1)
        self.assertEqual(self.closed[0][0], 70)
        self.assertIn("past the last rung of the model ladder", self.closed[0][1])
        self.assertIn({"name": "loom:failed"}, self.gh.issues[7]["labels"])
        # One email, not two: the reason goes into the pull request's body
        # with the close, and it says what happened rather than asking someone
        # to close what the station just closed (2026-10-21).
        self.assertEqual(self.gh.comments.get(70, []), [])
        self.assertEqual(self.gh.pulls[70]["state"], "closed")
        self.assertIn("past the last rung of the model ladder", self.gh.pulls[70]["body"])
        self.assertNotIn("close this pull request", self.gh.pulls[70]["body"])

    def test_a_handed_off_ticket_past_the_ladder_closes_its_lane_pull_request(self):
        # loom#2027: the hand-off pool skips the failed step, so its pull
        # request stayed open for days and kept a lane-pr-stalled finding open.
        self.gh.ticket(7, "failed")
        self.gh.issues[7]["labels"].append({"name": factory.HANDOFF_LABEL})
        self.gh.comments[7] += ALL_RUNGS + [
            {"body": factory.GAVE_UP + " (x; ran on none).", "user": ME}]
        self.gh.pull(70, 7, resolved=False)
        self.go()
        self.assertEqual([c[0] for c in self.closed], [70])
        note = self.closed[0][1]
        self.assertIn("#7", note)
        self.assertIn("past the last rung of the model ladder", note)
        self.assertIn("hand-off pool (%s)" % factory.HANDOFF_LABEL, note)
        self.assertEqual(self.gh.pulls[70]["state"], "closed")
        self.assertEqual(self.gh.comments.get(70, []), [])
        self.assertEqual((self.moved, self.fixed, self.merged), ([], [], []))
        self.assertIn({"name": "loom:failed"}, self.gh.issues[7]["labels"])
        self.go()  # once: nothing more on the next pass
        self.assertEqual(len(self.closed), 1)

    def test_a_ticket_rewritten_as_a_newer_ticket_closes_its_lane_pull_request(self):
        self.gh.ticket(7, "pr")
        self.gh.pull(70, 7)
        self.gh.ticket(9, "queued", body="Difficulty: medium\n\n## Why\nx\n\n"
                                         "Rewritten from issue 7.\n")
        self.go()
        self.assertEqual(len(self.closed), 1)
        number, note = self.closed[0]
        self.assertEqual(number, 70)
        self.assertIn("superseded", note)
        self.assertIn("#7 was rewritten as ticket #9", note)
        self.assertEqual(self.merged, [])
        self.assertEqual(self.gh.pulls[70]["state"], "closed")

    def test_a_ticket_labelled_rewritten_closes_its_pull_request_naming_the_new_ticket(self):
        self.gh.ticket(7, "pr")
        self.gh.issues[7]["labels"].append({"name": "loom:rewritten"})
        self.gh.comments[7].append({"body": brain.TRIAGE_MARKER + "\nRewritten as ticket #12, "
                                            "which refs this issue.", "user": ME})
        self.gh.pull(70, 7)
        self.go()
        self.assertEqual([c[0] for c in self.closed], [70])
        self.assertIn("rewritten as ticket #12", self.closed[0][1])

    def test_a_rewritten_failed_ticket_reads_its_comments_once_for_both_checks(self):
        self.gh.ticket(7, "failed")
        self.gh.issues[7]["labels"].append({"name": "loom:rewritten"})
        self.gh.comments[7] += ALL_RUNGS + [
            {"body": factory.GAVE_UP + " (x; ran on none).", "user": ME}]
        self.gh.pull(70, 7)
        f = factory.Factory(self.gh, [REPO], NOW, trusted=["Alice-Example"])
        with mock.patch.object(f, "_comments", wraps=f._comments) as read:
            note = f._ended_note(REPO, self.gh.issues[7], {})
        self.assertIn("past the last rung", note)
        self.assertEqual(read.call_count, 1)

    def test_a_strangers_rewritten_comment_closes_nothing(self):
        self.gh.ticket(7, "pr")
        self.gh.issues[7]["labels"].append({"name": "loom:rewritten"})
        self.gh.comments[7].append({"body": "Rewritten as ticket #12, which refs this issue.",
                                    "user": STRANGER})
        self.gh.pull(70, 7)
        self.go()
        self.assertEqual(self.closed, [])
        self.assertEqual(self.gh.pulls[70]["state"], "open")

    def test_a_lane_pull_request_whose_ticket_is_in_a_fix_round_stays_open(self):
        # A failed ticket with rungs left goes back for a fix round on its
        # open pull request; nothing closes it.
        self.gh.ticket(7, "failed", lane="zai")
        self.gh.comments[7].append(self.ZAI_CLAIM)
        self.gh.pull(70, 7, resolved=False)
        self.go()
        self.assertEqual(self.closed, [])
        self.assertEqual(self.gh.pulls[70]["state"], "open")
        self.assertEqual([f[:2] for f in self.fixed], [(70, (REPO, 7))])
        # And on its next pass, at loom:pr with the round queued.
        self.go()
        self.assertEqual(self.closed, [])
        self.assertEqual(self.gh.pulls[70]["state"], "open")

    def test_a_failed_ticket_climbed_to_the_last_ladder_rung_alerts(self):
        said = []
        with mock.patch.object(factory, "_say", said.append):
            self.gh.ticket(8, "failed", lane="codex-2",
                           body="Model: gpt-6-luna\nDifficulty: medium\n\n## Why\nx\n")
            self.gh.comments[8] += [{"body": factory.RUNG_MARKER % 2, "user": ME}]
            self.go()
        self.assertEqual(self.moved, [(8, "briefed", "claude")])
        self.assertTrue(any("last rung of the model ladder" in s for s in said), said)

    def stalled(self, number, lane="codex", body="Difficulty: medium\n\n## Why\nx\n"):
        self.gh.ticket(number, "briefed", lane=lane, body=body)
        self.gh.events[number] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                                   "created_at": iso(factory.BRIEFED_STALL + 600)}]

    def one_lane_stall(self, number=7, known_waits=None):
        """A rung 1 ticket stalled on claude, the rung's only lane; the lines said."""
        said = []
        with mock.patch.object(factory, "_say", said.append):
            f = self.go(known_waits=known_waits)
        return f, [s for s in said if "stalled on lane" in s]

    def test_a_stall_with_no_other_lane_is_said_once_not_every_pass(self):
        # 716 identical lines in two days (loom#1280).
        self.stalled(7, lane="claude",
                     body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        f, said = self.one_lane_stall()
        self.assertEqual(len(said), 1, said)
        self.assertEqual(list(f.waits), ["%s#7" % REPO])
        again, said = self.one_lane_stall(known_waits=f.waits)
        self.assertEqual(said, [])
        self.assertEqual(again.waits, f.waits)

    def test_a_stall_with_a_new_reason_is_said_again(self):
        self.stalled(7, lane="claude",
                     body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        _, said = self.one_lane_stall(known_waits={"%s#7" % REPO: "some older reason"})
        self.assertEqual(len(said), 1, said)

    def test_a_briefed_ticket_nobody_claims_moves_within_its_tier(self):
        self.stalled(7)
        self.go()
        self.assertEqual((self.lane(7), self.model(7)), ("spark", None))
        self.assertNotIn("loom:rung", "".join(c["body"] for c in self.gh.comments[7]))

    def test_a_stalled_rung_two_ticket_moves_to_the_other_rung_two_lane(self):
        self.stalled(7, body="Model: gpt-6-luna\nDifficulty: medium\n\n## Why\nx\n")
        self.go()
        self.assertEqual((self.lane(7), self.model(7)), ("codex-2", "gpt-6-luna"))
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

    def test_a_stall_never_moves_to_a_lane_with_as_long_a_wait(self):
        # loom#410, #977 and #1073 bounced codex-2 -> codex -> codex-2 for six
        # hours, unclaimed, each move restarting the stall wait (2026-10-21).
        body = "Model: gpt-6-luna\nDifficulty: medium\n\n## Why\nx\n"
        self.stalled(7, body=body)
        self.gh.ticket(8, "briefed", lane="codex", body=body)
        for number in (9, 10):
            self.gh.ticket(number, "briefed", lane="codex-2", body=body)
        self.go()
        self.assertEqual(self.lane(7), "codex")
        self.assertEqual(self.gh.comments[7], [])

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

    def tier_busy_on(self, lane, lifts_in):
        """A medium ticket briefed on `lane`, at a limit that lifts in
        `lifts_in` seconds, while every other lane of its tier is up and busy."""
        self.gh.ticket(7, "briefed", lane=lane, body="Difficulty: medium\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(60)}]
        busy = [x for x in factory.lanes_of("medium", True) if x != lane]
        for n, other in enumerate(busy, 20):
            self.gh.ticket(n, "briefed", lane=other)
        self.go(limited=[lane], available=busy, limit_until={lane: NOW + lifts_in})

    def test_a_ticket_on_a_lane_limited_for_days_goes_back_to_the_queue(self):
        # Two medium tickets sat 18 hours briefed to lanes at a monthly limit
        # while the queue took every lane of their tier as it freed
        # (loom#1224, 2026-10-20).
        self.tier_busy_on("opencode-go-2", 11 * 86400)
        self.assertIn("loom:queued", {x["name"] for x in self.gh.issues[7]["labels"]})
        self.assertEqual({x["name"] for x in self.gh.issues[7]["labels"]} & {"loom:briefed", "lane:opencode-go-2"}, set())
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("Not a failure (lane opencode-go-2 is at a limit until", note)
        self.assertIn("goes back to the queue", note)

    def whole_tier_limited(self, lifts, labels=()):
        """A medium ticket briefed on codex while every medium lane is at a
        limit that lifts at the seconds from now `lifts` names (a lane per
        entry, or one value for all)."""
        self.gh.ticket(7, "briefed", lane="codex", body="Difficulty: medium\n\n## Why\nx\n")
        self.gh.issues[7]["labels"].extend({"name": x} for x in labels)
        until = {lane: NOW + (lifts[lane] if isinstance(lifts, dict) else lifts)
                 for lane in factory.TIERS["medium"]}
        self.go(limited=factory.TIERS["medium"], limit_until=until)

    def test_a_ticket_whose_whole_tier_is_limited_for_days_moves_to_a_lane_up(self):
        # loom#1224: seven tickets sat briefed to agy, whose plan resets in days.
        self.whole_tier_limited(5 * 86400)
        self.assertEqual(self.lane(7), "claude")
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("Not a failure (lane codex is at a limit until", note)
        self.assertIn("fallback: no medium lane is up", note)

    def test_a_ticket_whose_whole_tier_frees_up_within_two_hours_keeps_waiting(self):
        self.whole_tier_limited(30 * 60)
        self.assertEqual(self.lane(7), "codex")

    def test_one_tier_lane_freeing_up_soon_keeps_the_ticket_waiting(self):
        lifts = {lane: 5 * 86400 for lane in factory.TIERS["medium"]}
        lifts["codex-2"] = 30 * 60
        self.whole_tier_limited(lifts)
        # Its own lane is out for days, so it goes back to the queue for the
        # lane that frees up (loom#1226); it does not leave its tier.
        self.assertIn("loom:queued", {x["name"] for x in self.gh.issues[7]["labels"]})
        self.assertNotEqual(self.lane(7), "claude")

    def test_a_ticket_held_for_the_brain_or_a_runaway_is_not_moved(self):
        for label in ("needs-brain", factory.RUNAWAY):
            with self.subTest(label=label):
                self.setUp()
                self.whole_tier_limited(5 * 86400, labels=(label,))
                self.assertEqual(self.lane(7), "codex")
                self.assertEqual(self.gh.comments.get(7, []), [])
                self.setUp()
                self.gh.ticket(7, "briefed", lane="opencode-go-2",
                               body="Difficulty: medium\n\n## Why\nx\n")
                self.gh.issues[7]["labels"].append({"name": label})
                busy = [x for x in factory.lanes_of("medium", True) if x != "opencode-go-2"]
                for n, other in enumerate(busy, 20):
                    self.gh.ticket(n, "briefed", lane=other)
                self.go(limited=["opencode-go-2"], available=busy,
                        limit_until={"opencode-go-2": NOW + 11 * 86400})
                self.assertEqual(self.lane(7), "opencode-go-2")
                self.assertNotIn("loom:queued", {x["name"] for x in self.gh.issues[7]["labels"]})

    def test_a_ticket_whose_lane_frees_up_soon_keeps_waiting_for_it(self):
        self.tier_busy_on("opencode-go-2", 30 * 60)
        self.assertEqual(self.lane(7), "opencode-go-2")
        self.assertIn("loom:briefed", {x["name"] for x in self.gh.issues[7]["labels"]})

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

    def test_a_slot_on_cluster_runners_never_takes_a_private_repos_ticket(self):
        # A runner's inputs and logs live in the public repo's Actions, so the
        # dispatch refuses a private repo; the factory gave free-gemma private
        # tickets anyway, each costing a claim and a park (2026-10-27).
        with mock.patch.dict(os.environ, {"LOOM_RUNNER_SLOTS": "free-gemma"}):
            self.assertNotIn("free-gemma", factory.lanes_of("medium", True))
            self.assertIn("free-gemma", factory.lanes_of("medium", False))
            self.assertTrue(factory.public_only("free-gemma"))
        self.assertIn("free-gemma", factory.lanes_of("medium", True))
        self.assertFalse(factory.public_only("free-gemma"))

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
        body = "Model: claude-sonnet-5-5\nDifficulty: hard\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.go(model_limits={"claude": {"claude-sonnet-5-5": NOW + 3600}})
        self.assertEqual((self.lane(7), self.model(7)), ("claude", "claude-sonnet-5-5"))

    def test_a_rung_ticket_whose_only_lane_is_limited_for_a_day_climbs(self):
        self.gh.private = False
        body = "Model: claude-sonnet-5-5\nDifficulty: hard\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        with mock.patch("bus.factory._say") as say:
            self.go(model_limits={"claude": {"claude-sonnet-5-5": NOW + 86400}})
        self.assertNotEqual(self.lane(7), "claude")
        self.assertIn(self.model(7), [m for _, m in factory.CLIMB[1:]])
        note = self.gh.comments[7][-1]["body"]
        self.assertIn("Not a failure (lane claude is at a limit for over 2 hours)", note)
        self.assertTrue(any(c.args[0].startswith("climbed %s#7 off lane claude" % REPO)
                            for c in say.call_args_list), say.call_args_list)
        # The rung it left was not failed on, so it stays untried.
        self.assertEqual(self.moved, [])

    def test_a_rung_ticket_whose_only_lane_is_out_of_credit_for_a_day_climbs(self):
        # A whole-lane limit (out of credit, no limited_models) never counted
        # as a long wait: nine tickets waited on codex and codex-2 for a day
        # while claude and the spark lanes had room (2026-10-21).
        self.gh.private = False
        body = "Model: claude-sonnet-5-5\nDifficulty: hard\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.go(limited=["claude"], limit_until={"claude": NOW + 86400})
        self.assertNotEqual(self.lane(7), "claude")
        self.assertIn("Not a failure (lane claude is at a limit for over 2 hours)",
                      self.gh.comments[7][-1]["body"])

    def test_a_rung_ticket_whose_only_lane_frees_within_two_hours_waits(self):
        self.gh.private = False
        body = "Model: claude-sonnet-5-5\nDifficulty: hard\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.go(limited=["claude"], limit_until={"claude": NOW + 3600})
        self.assertEqual((self.lane(7), self.model(7)), ("claude", "claude-sonnet-5-5"))

    def test_a_paid_rung_limited_longer_than_two_hours_uses_the_strongest_free_lane(self):
        self.gh.private = False
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.go(model_limits={lane: {"claude-sonnet-5-5": NOW + 3 * 3600}
                              for lane in factory.LADDER[0][0]})
        self.assertEqual(self.lane(7), "free-ollama")
        self.assertIsNone(self.model(7))
        self.assertIn("Not a failure", self.gh.comments[7][-1]["body"])

    def test_a_paid_rung_freeing_within_two_hours_is_still_waited_for(self):
        self.gh.private = False
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.go(model_limits={lane: {"claude-sonnet-5-5": NOW + 90 * 60}
                              for lane in factory.LADDER[0][0]})
        self.assertEqual((self.lane(7), self.model(7)), ("claude", "claude-sonnet-5-5"))

    def test_a_long_paid_wait_skips_free_lanes_the_ticket_already_tried(self):
        self.gh.private = False
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.gh.comments[7].append({"body": "Claimed for lane free-ollama (body sha256 abcdef123456)",
                                    "user": ME})
        self.go(model_limits={lane: {"claude-sonnet-5-5": NOW + 3 * 3600}
                              for lane in factory.LADDER[0][0]})
        self.assertEqual(self.lane(7), "free-gemma")

    def test_a_ladder_ticket_at_a_limit_stays_on_its_rung(self):
        body = "Model: claude-opus-5-5\nDifficulty: easy\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="claude", body=body)
        self.go(limited=["claude"])
        self.assertEqual((self.lane(7), self.model(7)), ("anthropic", "claude-opus-5-5"))

    def test_a_model_agy_lacks_does_not_move_to_agy_at_a_limit(self):
        self.gh.private = False
        body = "Model: gpt-6-luna\nDifficulty: easy\n\n## Why\nx\n"
        self.gh.ticket(7, "briefed", lane="spark", body=body)
        self.go(limited=["spark"], available=["agy"])
        self.assertEqual((self.lane(7), self.model(7)), ("spark", "gpt-6-luna"))

    def test_a_stalled_ticket_with_a_model_agy_lacks_does_not_move_to_agy(self):
        self.gh.private = False
        body = "Model: gpt-6-luna\nDifficulty: easy\n\n## Why\nx\n"
        self.stalled(7, lane="spark", body=body)
        self.go(available=["agy"])
        self.assertEqual((self.lane(7), self.model(7)), ("spark", "gpt-6-luna"))

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
        self.assertTrue(all(self.model(n) == "gpt-6-luna" for n in moved))

    def escalated_after_sol(self, number, state):
        """A ticket whose first attempts ran on gpt-6-luna (its Model: line, two
        codex claims, no rung marker) and that the brain escalated to rung 1."""
        self.gh.ticket(number, state, lane="claude",
                       body="Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n")
        sol_claim = {"body": "Claimed for lane codex (body sha256 0123456789ab)", "user": ME}
        self.gh.comments[number] += [sol_claim, sol_claim, {
            "body": "<!-- loom:escalate v1 -->\nEscalated to rung 1 of 3: claude-sonnet-5-5 "
                    "on lane claude.\nReason: 2 fix rounds (attempts 2, tried gpt-6-luna).\n"
                    "Rung: 1 claude-sonnet-5-5 claude", "user": ME}]

    @lane_models(codex="gpt-6-luna")
    def test_a_ticket_that_ran_on_sol_is_never_spread_back_to_sol(self):
        # 2026-10-16: three attempts on gpt-6-luna with no rung marker, escalated
        # to rung 1, then spread off the busy claude lane to gpt-6-luna again.
        for n in range(1, 4):
            self.gh.ticket(n, "briefed", lane="claude")
        self.escalated_after_sol(7, "briefed")
        self.go()
        self.assertNotIn(self.lane(7), ("codex", "codex-2"))
        self.assertNotEqual(self.model(7), "gpt-6-luna")
        self.assertIn((self.lane(7), self.model(7)),
                      [("claude", "claude-sonnet-5-5")]
                      + [(lane, "claude-opus-5-5") for lane in factory.ANTHROPIC])
        self.go(limited=factory.ANTHROPIC)  # rung 3 full: it waits on its rung
        self.assertNotEqual(self.model(7), "gpt-6-luna")

    @lane_models(codex="gpt-6-luna")
    def test_a_ticket_that_ran_on_sol_climbs_past_sol_when_it_fails(self):
        self.escalated_after_sol(7, "failed")
        self.gh.comments[7].append(CLAIM)  # it ran on rung 1 and failed there
        self.go()
        self.assertEqual(self.model(7), "claude-opus-5-5")
        self.assertIn("rung 3", self.gh.comments[7][-1]["body"])

    @lane_models(claude="claude-sonnet-5-5", codex="gpt-6-luna")
    def test_ran_models_reads_claims_the_model_line_and_markers(self):
        sol = {"body": "Claimed for lane codex (body sha256 0123456789ab)"}
        opus = {"body": "Model: claude-opus-5-5\n\n## Why\nx\n"}
        self.assertEqual(factory.ran_models(opus, [CLAIM]), {"claude-opus-5-5"})
        # With no Model: line, a claim ran on its lane's configured model.
        self.assertEqual(factory.ran_rungs({"body": "x"}, [sol, CLAIM]), {1, 2})
        marked = [sol, {"body": factory.RUNG_MARKER % 3}, CLAIM]
        self.assertEqual(factory.ran_models(opus, marked), {"gpt-6-luna", "claude-opus-5-5"})
        # A rung 0 marker starts the ladder again; an unclaimed ticket ran on nothing.
        self.assertEqual(factory.ran_models(opus, [sol, {"body": factory.RUNG_MARKER % 0}]), set())
        self.assertEqual(factory.ran_models(opus, []), set())

    def test_a_first_brief_is_never_spread(self):
        for n in range(1, 6):
            self.gh.ticket(n, "briefed", lane="claude")
        self.go()
        self.assertEqual([self.lane(n) for n in range(1, 6)], ["claude"] * 5)

    def test_a_busy_claude_ticket_is_not_spread_to_opus_on_claude_while_anthropic_is_out(self):
        # The anthropic slots out of credit hold no tickets, so rung 3 looked
        # free, and a ticket waiting on a busy claude lane was "spread" to
        # Opus on the same claude lane (2026-10-19).
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        for n in range(1, 6):
            self.gh.ticket(n, "briefed", lane="claude", body=body)
            self.gh.comments[n].append({"body": factory.RUNG_MARKER % 1, "user": ME})
        for n, lane in ((11, "codex"), (12, "codex"), (13, "codex-2"), (14, "codex-2")):
            self.gh.ticket(n, "briefed", lane=lane)
        self.go(limited=factory.ANTHROPIC)
        for n in range(1, 6):
            self.assertNotEqual((self.lane(n), self.model(n)), ("claude", "claude-opus-5-5"))
            self.assertFalse(any(factory.RUNG_MARKER % 3 in c["body"]
                                 for c in self.gh.comments[n]))

    def test_a_one_lane_rung_sends_its_overflow_to_a_busy_rung_with_a_shorter_wait(self):
        # 13 tickets waited on the one claude lane (two for 10 hours) while the
        # other rungs held two or three each and so never counted as free
        # (loom#1280, 2026-10-21).
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        for n in range(1, 9):
            self.gh.ticket(n, "briefed", lane="claude", body=body)
            self.gh.comments[n].append({"body": factory.RUNG_MARKER % 1, "user": ME})
        for n, lane in ((11, "codex"), (12, "codex"), (13, "codex"),
                        (14, "codex-2"), (15, "codex-2"), (16, "codex-2")):
            self.gh.ticket(n, "briefed", lane=lane)
        self.go(limited=factory.ANTHROPIC)
        stayed = [n for n in range(1, 9) if self.lane(n) == "claude"]
        self.assertLess(len(stayed), 8)
        self.assertLessEqual(len(stayed), factory.ONE_LANE_MAX + 2)
        moved = [n for n in range(1, 9) if self.lane(n) != "claude"]
        self.assertTrue(all(self.model(n) == "gpt-6-luna" for n in moved))
        # No lane it moved to ends up longer than the one it left.
        loads = [sum(1 for n in list(range(1, 9)) + list(range(11, 17)) if self.lane(n) == lane)
                 for lane in ("claude", "codex", "codex-2")]
        self.assertLessEqual(max(loads) - min(loads), 2)

    def test_a_one_lane_rung_at_its_limit_keeps_tickets_when_no_rung_is_shorter(self):
        body = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
        for n in range(1, 6):
            self.gh.ticket(n, "briefed", lane="claude", body=body)
            self.gh.comments[n].append({"body": factory.RUNG_MARKER % 1, "user": ME})
        for n in range(11, 19):  # codex and codex-2 hold 4 each: none holds 3 fewer
            self.gh.ticket(n, "briefed", lane="codex" if n % 2 else "codex-2")
        self.go(limited=factory.ANTHROPIC)
        self.assertEqual([self.lane(n) for n in range(1, 6)], ["claude"] * 5)

    def test_two_busy_rungs_do_not_ping_pong_a_ticket(self):
        # One ticket flipped between rung 2 on codex-2 and rung 3 on claude 8
        # times in 90 minutes, as each rung looked free by a lane that was
        # out, and was never claimed (2026-10-20).
        self.gh.ticket(1, "briefed", lane="claude",
                       body="Model: claude-opus-5-5\nDifficulty: medium\n\n## Why\nx\n")
        self.gh.comments[1].append({"body": factory.RUNG_MARKER % 3, "user": ME})
        for n in range(2, 8):
            self.gh.ticket(n, "briefed", lane="claude")  # claude holds 7
        for n in range(8, 13):
            self.gh.ticket(n, "briefed", lane="codex-2")  # codex-2 holds 5
        before = len(self.gh.comments[1])
        for _ in range(2):
            self.go(limited=("codex",) + factory.ANTHROPIC)
            self.assertEqual((self.lane(1), self.model(1)), ("claude", "claude-opus-5-5"))
        self.assertFalse(any(factory.RUNG_RE.search(c["body"])
                             for c in self.gh.comments[1][before:]))

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

    def test_a_ticket_moved_off_a_silent_lane_drops_our_claim_and_keeps_theirs(self):
        # loom#1393: the claim marks a run, and the run ended with the lane.
        busy = [x for x in factory.lanes_of("medium", True) if x != "codex"]
        for n, lane in enumerate(busy, 20):
            self.gh.ticket(n, "briefed", lane=lane)
        for free in (False, True):
            with self.subTest(free_lane=free):
                self.silent_ticket()
                self.gh.issues[7]["labels"] += [{"name": "claim:alice-example"},
                                                {"name": "claim:other-op"}]
                with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "alice-example"}):
                    if free:
                        self.go(silent={"codex": 31 * 60})
                    else:
                        self.go(silent={"codex": 31 * 60}, available=busy)
                self.assertNotIn("loom:running", self.labels())
                self.assertNotIn("claim:alice-example", self.labels())
                self.assertIn("claim:other-op", self.labels())


class Helpers(unittest.TestCase):
    def test_a_finding_reposted_under_a_new_thread_id_still_counts_as_stuck(self):
        # The review bot may post the same finding again as a new thread, a
        # few lines lower after the round's edits: comparing ids alone read
        # that as progress.
        last = {"body": "%s\nPull request: %s#7, branch loom/12-x\n\n"
                        "### Thread PRRT_old (comment 5) at web/staged.js:54\n\n"
                        "    grug-tribe: high | _security_ | `unvalidated-external-input` | quick"
                        % (FIX_MARKER, REPO), "user": ME}
        new = {"id": "PRRT_new", "path": "web/staged.js", "line": 59,
               "root": {"nodes": [{"body": "high | _security_ | `unvalidated-external-input` |"}]}}
        other = dict(new, line=59 + 40)
        self.assertTrue(factory.stuck([last], ["PRRT_new"], False, 0.0, NOW, threads=[new]))
        self.assertFalse(factory.stuck([last], ["PRRT_new"], False, 0.0, NOW, threads=[other]))

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
        # Past Opus no stronger model is left: luna and Sonnet are weaker.
        self.assertEqual(factory.next_rung({3}, {}), 0)
        self.assertEqual(factory.next_rung({1, 2, 3}, {}), 0)
        # Every untried rung busy: wait on the least loaded one.
        all_busy = dict(busy, agy=2, **{lane: 2 for lane in factory.ANTHROPIC})
        self.assertEqual(factory.next_rung({1}, all_busy), 2)
        self.assertEqual(factory.next_rung(set(), dict(all_busy, claude=30)), 2)
        # A free agy serves no rung: agy runs gemini-3.8-flash only.
        self.assertEqual(factory.next_rung({1}, dict(all_busy, agy=0)), 2)
        # Only the lanes that may serve a rung now count: rung 3 is not free
        # when only the busy claude lane serves it, however idle the rest.
        # With every up rung busy it waits on the least loaded, rung 1, not on
        # Opus on the same claude lane; a free codex lane takes rung 2.
        up = {1: ("claude",), 2: ("codex", "codex-2"), 3: ("claude",)}
        self.assertEqual(factory.next_rung(set(), busy, lanes=up), 1)
        self.assertEqual(factory.next_rung(set(), dict(busy, codex=1), lanes=up), 2)
        # A rung with no lane serving it now is passed over.
        self.assertEqual(factory.next_rung(set(), {"claude": 2},
                                           lanes={1: ("claude",), 2: (), 3: ("anthropic",)}), 3)

    def test_a_pull_request_for_another_repos_ticket_has_no_ticket_and_no_log_line(self):
        # 2026-10-20: an image-pin pull request ships another repo's ticket by
        # design, and logged a refusal for it on every pass.
        f = factory.Factory(FakeGitHub(), [REPO], NOW)
        other = {"number": 50, "body": "## Why\nx\n\nRefs example-org/other-repo#5\n",  # leak-guard-allow
                 "head": {"ref": "pin-image"}}
        with mock.patch("bus.factory._say") as say:
            self.assertIsNone(f._association(REPO, other))
        say.assert_not_called()

    def test_conflicting_ticket_references_still_log_their_line(self):
        f = factory.Factory(FakeGitHub(), [REPO], NOW)
        both = {"number": 50, "body": "Refs %s#5\nRefs %s#6\n" % (REPO, REPO),
                "head": {"ref": "pin-image"}}
        with mock.patch("bus.factory._say") as say:
            self.assertIsNone(f._association(REPO, both))
        say.assert_called_once_with(
            "no ticket for %s#50: pull request has conflicting ticket references" % REPO)

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

    def test_a_check_judged_by_its_newest_run_not_a_stale_failure(self):
        gh = FakeGitHub()
        seen = []
        runs = [{"id": 1, "name": "leak-scan", "status": "completed", "conclusion": "failure",
                 "started_at": "2030-01-01T00:29:00Z"},
                {"id": 3, "name": "leak-scan", "status": "completed", "conclusion": "success",
                 "started_at": "2030-01-01T01:56:00Z"},
                {"id": 2, "name": "leak-scan", "status": "completed", "conclusion": "success",
                 "started_at": "2030-01-01T01:27:00Z"}]

        def rest(method, path, body=None, etag=None, ok=()):
            seen.append(path)
            return Response(200, {"check_runs": runs})
        gh.rest = rest
        f = factory.Factory(gh, [REPO], NOW, trusted=["alice-example"])
        self.assertEqual([r["id"] for r in f._check_runs(REPO, "sha")], [3])
        self.assertIn("filter=all", seen[0])

    def test_the_station_setting_turns_it_off(self):
        self.assertFalse(station.Settings.from_env({"LOOM_FACTORY": "0"}).factory_on)
        on = station.Settings.from_env({})
        self.assertTrue(on.factory_on)
        self.assertEqual(on.review_check, factory.DEFAULT_REVIEW_CHECK)
        self.assertEqual(on.no_test_repos, [])
        self.assertEqual(station.Settings.from_env(
            {"LOOM_NO_TEST_REPOS": "a/b c/d"}).no_test_repos, ["a/b", "c/d"])

    def test_the_station_builds_the_scope_check_only_from_an_https_route(self):
        def scope(env):
            with tempfile.TemporaryDirectory() as home:
                settings = station.Settings.from_env(dict(env, LOOM_REPOS=REPO, LOOM_HOME=home))
                st = station.Station(FakeGitHub(), settings, now=NOW)
                with mock.patch.object(station.Station, "_heartbeat_ok", return_value=True):
                    return st._factory().scope
        self.assertIsNone(scope({}))
        self.assertIsNone(scope({"LOOM_SCOPE_CHECK_URL": "http://gw.invalid/v1"}))
        self.assertTrue(callable(scope({"LOOM_SCOPE_CHECK_URL": "https://gw.invalid/free-public/gemini/v1"})))

    def test_the_station_wires_the_luna_review_writer_into_the_factory(self):
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], ["alice-example"], [], factory_on=True)
            st = station.Station(FakeGitHub(), settings, now=NOW)
            self.assertEqual(st._factory().review_brief, st._write_review_brief)


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
                         model_limits=None, silent=None, down=(), limit_until=None,
                         benched=(), known_waits=None, plan_check=None, demoted=None,
                         no_lane_since=None, review_brief=None):
                self.silent_running, self.silent, self.waits = {}, silent, {}
                self.settled = set()
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
            "up the model ladder to rung 2, gpt-6-luna on lane codex.",
            "Claimed for lane codex (body sha256 0123456789ab)")
        self.assertEqual(factory.dead_lane(comments), "codex")

    def test_a_retry_a_hold_and_a_stop_end_their_attempts_with_their_class(self):
        # Since status events (loom#866) these endings do not start "Lane X
        # failed:", and the runaway detector counted each claim as work.
        comments = self.comments(
            "Claimed for lane codex (body sha256 0123456789ab)",
            "Automatic retry 1 of 2 (transient): moved from lane codex to lane "
            "codex-2. Lane codex failed: [transient] agent exited 1",
            "Claimed for lane codex-2 (body sha256 0123456789ab)",
            "Lane codex-2 held: setup failed: could not check out the default branch; "
            "factory incident quadseven/loom#99; the agent did not run. The ticket keeps "
            "its rung and returns to the queue when the factory incident closes.",
            "Claimed for lane codex (body sha256 0123456789ab)",
            "Lane codex stopped: [limit] provider limit reached",
            "Claimed for lane codex (body sha256 0123456789ab)",
            "Lane codex stopped: [auth] the provider refused the key",
            "Claimed for lane codex (body sha256 0123456789ab)")
        reasons = [a[3] for a in factory.attempts(comments)]
        self.assertEqual(reasons[0], "[transient] agent exited 1")
        self.assertTrue(reasons[1].startswith("[factory] setup failed:"), reasons[1])
        self.assertEqual(reasons[2:], ["[limit] provider limit reached",
                                       "[auth] the provider refused the key", ""])

    def test_a_stopped_line_without_a_class_is_not_an_ending(self):
        comments = self.comments(
            "Claimed for lane codex (body sha256 0123456789ab)",
            "Lane codex stopped: [work] something else")
        self.assertEqual([a[3] for a in factory.attempts(comments)], [""])


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


class ForbiddenModels(unittest.TestCase):
    """The operator's rule of 2026-10-21: codex runs gpt-6-luna only and agy
    the gemini-3.8-flash family only. A ticket's status read "Escalated to
    rung 2 of 4: gpt-6-sol on lane codex", and tickets carried Model: lines
    naming gpt-6-sol and gemini-4-argon-high."""

    FORBIDDEN = ("gpt-6-sol", "gemini-4-argon")

    def assert_allowed(self, model, where):
        self.assertFalse(model.startswith(self.FORBIDDEN), "%s names %s" % (where, model))

    def test_no_rung_pin_equivalent_or_strong_model_is_forbidden(self):
        for _, model in factory.CLIMB:
            self.assert_allowed(model, "factory.CLIMB")
        for lane, model in factory.HARD_MODELS.items():
            self.assert_allowed(model, "factory.HARD_MODELS[%s]" % lane)
        for pin, (_, model, _) in factory.EQUIVALENTS.items():
            self.assert_allowed(model, "factory.EQUIVALENTS[%s]" % pin)
        for model in factory.STRONG | station.STRONG_MODELS:
            self.assert_allowed(model, "the strong models")
        self.assertEqual(dict(factory.LADDER[1:2]), {("codex", "codex-2"): "gpt-6-luna"})

    def test_a_go_model_off_the_allow_list_reads_as_the_stand_in(self):
        # Kimi K2.7 Code (1,350 requests per 5 h) spent the Go allowance in
        # hours on 2026-10-23; the allow-list is free or 10,000+ models only.
        self.assertIn(tickets.OPENCODE_GO_STAND_IN, tickets.OPENCODE_GO_MODELS)
        for named in ("opencode-go/kimi-k2.7-code", "opencode-go/kimi-k3.1-pro",
                      "opencode-go/gemini-4-argon-contributor", "opencode-go/some-new-model"):
            headers, _ = tickets.parse("Model: %s\nDifficulty: easy\n\n## Why\nx\n" % named)
            self.assertEqual(headers["Model"], tickets.OPENCODE_GO_STAND_IN, named)
            self.assertFalse(station.lane_takes_model("opencode-go-2", named), named)
        for model in tickets.OPENCODE_GO_MODELS:
            self.assertEqual(tickets.allowed_model(model), model)
            self.assertTrue(station.lane_takes_model("opencode-go", model), model)
        for _, model in factory.CLIMB:
            self.assertFalse(model.startswith("opencode-go/") and model not in tickets.OPENCODE_GO_MODELS)

    def test_the_forbidden_list_covers_both_families(self):
        self.assertEqual(tuple(p for p, _ in tickets.FORBIDDEN_MODELS), self.FORBIDDEN)

    def test_a_ticket_naming_a_forbidden_model_reads_as_its_allowed_stand_in(self):
        for named, runs in (("gpt-6-sol", "gpt-6-luna"), ("gpt-6-sol-high", "gpt-6-luna"),
                            ("gemini-4-argon-high", "gemini-3.8-flash-high"),
                            ("gpt-6-luna", "gpt-6-luna"), ("claude-opus-5-5", "claude-opus-5-5")):
            headers, _ = tickets.parse("Model: %s\nDifficulty: hard\n\n## Why\nx\n" % named)
            self.assertEqual(headers["Model"], runs, named)

    def test_the_brain_and_the_factory_write_the_stand_in(self):
        body = "Difficulty: hard\n\n## Why\nx\n"
        self.assertIn("Model: gpt-6-luna\n", brain._with_model(body, "gpt-6-sol"))
        self.assertIn("Model: gemini-3.8-flash-high\n", factory.with_model(body, "gemini-4-argon-high"))

    def test_agy_takes_only_its_flash_models(self):
        self.assertTrue(station.lane_takes_model("agy", "gemini-3.8-flash-high"))
        for model in ("gemini-4-argon-high", "claude-opus-5-5", "gpt-oss-120b"):
            self.assertFalse(station.lane_takes_model("agy", model), model)


class NoLane(Base):
    """Tickets no lane takes are rerouted, then handed off (loom#1831)."""
    SONNET = "Model: claude-sonnet-5-5\nDifficulty: medium\n\n## Why\nx\n"
    OPUS = "Model: claude-opus-5-5\nDifficulty: medium\n\n## Why\nx\n"

    def queued(self, number, body):
        self.gh.ticket(number, "queued", body=body)
        self.gh.issues[number]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"}]
        self.gh.issues[number]["created_at"] = iso(0)

    def assign(self, waited, available, **kwargs):
        key = "%s#7" % REPO
        worker = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"],
                                 available=available, no_lane_since={key: NOW - waited},
                                 **kwargs)
        worker.assign_queued()
        return worker

    def notes(self, number):
        return "".join(c["body"] for c in self.gh.comments.get(number, []))

    def test_the_reroute_hours_come_from_the_environment(self):
        self.assertEqual(factory.no_lane_reroute({}), 6 * 3600)
        self.assertEqual(factory.no_lane_reroute({"LOOM_NO_LANE_REROUTE": "2"}), 2 * 3600)
        self.assertEqual(factory.no_lane_reroute({"LOOM_NO_LANE_REROUTE": "0.5"}), 1800)
        for bad in ("", "x", "0", "-3", "nan", "inf"):
            self.assertEqual(factory.no_lane_reroute({"LOOM_NO_LANE_REROUTE": bad}), 6 * 3600, bad)
        # Never past the hand-off: by 24 hours at the latest it leaves.
        self.assertEqual(factory.no_lane_reroute({"LOOM_NO_LANE_REROUTE": "48"}), 24 * 3600)

    def test_no_lane_reasons_are_told_from_other_waits(self):
        for reason in ("no free lane takes model claude-sonnet-5-5", "no free lane in tier medium",
                       "stalled on lane claude: no other lane of its rung is available",
                       "on lane codex, at a limit: no free lane in its tier"):
            self.assertTrue(factory.no_lane(reason), reason)
        for reason in ("waits for #5 to merge (After:)", "waits for a person (loom:human)",
                       "stalled on lane codex: another lane of its tier waits as long"):
            self.assertFalse(factory.no_lane(reason), reason)

    def test_a_queued_ticket_no_lane_takes_for_six_hours_moves_to_another_rung(self):
        # 2026-10-25: 89 tickets waited on "no free lane takes model
        # claude-sonnet-5-5", some since 10-23, while codex had room.
        self.queued(7, self.SONNET)
        self.gh.ticket(8, "running", lane="claude")
        worker = self.assign(7 * 3600, ["claude", "codex"])
        self.assertEqual((factory._state(self.gh.issues[7]), self.lane(7), self.model(7)),
                         ("briefed", "codex", "gpt-6-luna"))
        self.assertIn(factory.RUNG_MARKER % 2, self.notes(7))
        self.assertIn("rerouted this ticket to rung 2", self.notes(7))
        self.assertNotIn("%s#7" % REPO, worker.waits)
        self.assertIn("%s#7" % REPO, worker.settled)

    def test_a_queued_ticket_waiting_under_six_hours_stays_where_it_is(self):
        self.queued(7, self.SONNET)
        self.gh.ticket(8, "running", lane="claude")
        worker = self.assign(5 * 3600, ["claude", "codex"])
        self.assertEqual((factory._state(self.gh.issues[7]), self.model(7)),
                         ("queued", "claude-sonnet-5-5"))
        self.assertEqual(worker.waits["%s#7" % REPO], "no free lane takes model claude-sonnet-5-5")
        self.assertNotIn("%s#7" % REPO, worker.settled)

    def test_a_reroute_never_leaves_the_tickets_tier(self):
        # Easy work never runs on a ladder lane (TIERS): no reroute, it waits.
        self.queued(7, "Model: claude-sonnet-5-5\nDifficulty: easy\n\n## Why\nx\n")
        self.gh.ticket(8, "running", lane="claude")
        self.assign(7 * 3600, ["claude", "codex"])
        self.assertEqual((factory._state(self.gh.issues[7]), self.model(7)),
                         ("queued", "claude-sonnet-5-5"))

    def test_a_stalled_briefed_ticket_is_rerouted_after_six_hours(self):
        self.gh.private = False
        self.stalled(7)
        f = self.go(available=["claude", "codex"], no_lane_since={"%s#7" % REPO: NOW - 7 * 3600})
        self.assertEqual((self.lane(7), self.model(7)), ("codex", "gpt-6-luna"))
        self.assertIn("%s#7" % REPO, f.settled)

    def stalled(self, number):
        self.gh.ticket(number, "briefed", lane="claude", body=self.SONNET)
        self.gh.events[number] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                                   "created_at": iso(factory.BRIEFED_STALL + 600)}]

    def test_a_ticket_no_lane_takes_for_a_day_goes_to_the_hand_off_pool_once(self):
        # Opus is the strong rung: no reroute is allowed below it.
        self.queued(7, self.OPUS)
        worker = self.assign(25 * 3600, [])
        names = [x["name"] for x in self.gh.issues[7]["labels"]]
        self.assertIn(factory.HANDOFF_LABEL, names)
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")
        self.assertFalse([x for x in names if x.startswith("lane:")])
        self.assertIn("hand-off pool", self.notes(7))
        self.assertIn("%s#7" % REPO, worker.settled)
        self.assertFalse(brain._in_flight(self.gh.issues[7]))
        comments = [dict(c) for c in self.gh.comments[7]]
        # The next pass leaves it alone: no second note, no lane, no wait.
        again = self.assign(26 * 3600, ["claude"])
        self.assertEqual(self.gh.comments[7], comments)
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")
        self.assertNotIn("%s#7" % REPO, again.waits)

    def test_a_ticket_with_no_reroute_waits_until_its_day_is_out(self):
        self.queued(7, self.OPUS)
        worker = self.assign(23 * 3600, [])
        self.assertNotIn(factory.HANDOFF_LABEL, [x["name"] for x in self.gh.issues[7]["labels"]])
        self.assertIn("%s#7" % REPO, worker.waits)

    def test_a_ticket_a_lane_takes_settles_its_wait(self):
        self.queued(7, self.SONNET)
        worker = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"],
                                 available=["claude"])
        worker.assign_queued()
        self.assertEqual(self.lane(7), "claude")
        self.assertIn("%s#7" % REPO, worker.settled)

    def test_the_hand_off_pool_leaves_the_cap_but_a_pr_ticket_still_counts(self):
        def ticket(state, *extra):
            return {"state": "open", "labels": [{"name": x} for x in
                                                ("loom:ticket", "loom:" + state) + extra]}
        self.assertTrue(brain._in_flight(ticket("queued")))
        self.assertFalse(brain._in_flight(ticket("queued", factory.HANDOFF_LABEL)))
        self.assertFalse(brain._in_flight(ticket("briefed", factory.HANDOFF_LABEL)))
        self.assertTrue(brain._in_flight(ticket("pr")))
        self.assertTrue(brain._in_flight(ticket("pr", factory.HANDOFF_LABEL)))


def conflict_round_comment(number, ticket):
    """A loom:fix comment sending a pull request back for its conflict, in
    the shape brain.fix writes (the header names the pull request, and the
    body carries the conflict line)."""
    return {"body": FIX_MARKER + "\nPull request: %s#%d, branch loom/%d-some-title\n" % (REPO, number, ticket)
            + brain.BRAIN_FIX_MARKER + "\n\n" + factory.FIX_NOTES
            + "\n\n- The branch conflicts with the default branch\n",
            "user": ME}


class BranchHygiene(Base):
    """The standing branch-hygiene pass (loom#2135): every open lane pull
    request is kept current or sent back for its conflict over its lifetime,
    not only at merge time. The setups keep the judge path out of the way
    (its checks still running, so it waits) unless the test says otherwise,
    so what moves is the hygiene pass."""

    WAITING = (("unittest", "in_progress"), (factory.DEFAULT_REVIEW_CHECK, "in_progress"))

    def updates(self):
        return [w[1] for w in self.gh.writes if w[1].endswith("/update-branch")]

    def test_a_behind_pull_request_is_brought_up_to_date_and_a_current_one_left_alone(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200, runs=self.WAITING)
        self.gh.behind[50] = 3
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6, created=3600, runs=self.WAITING)
        self.go()
        self.assertEqual(self.updates(), ["/repos/%s/pulls/50/update-branch" % REPO])
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_a_dirty_pull_request_gets_a_fix_round_naming_it(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=False, runs=self.WAITING)
        f = self.go()
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertIn("conflict", f.notes)
        self.assertEqual(self.merged, [])

    def test_a_dirty_pull_request_with_a_round_open_gets_no_second_round(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=False, runs=self.WAITING)
        self.gh.comments[5].append(conflict_round_comment(50, 5))
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.merged, [])

    def test_no_update_while_another_pull_request_holds_the_landing_lock(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=3600, labels=("loom:landing",), runs=self.WAITING)
        self.gh.events[50] = [landing_event(600)]
        self.gh.behind[50] = 2
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6, created=7200, runs=self.WAITING)
        self.gh.behind[60] = 2
        self.go()
        self.assertEqual(self.updates(), [])
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_updates_are_bounded_per_repo_per_pass_oldest_first(self):
        for ticket, number, created in ((5, 50, 10800), (6, 60, 7200), (7, 70, 3600)):
            self.gh.ticket(ticket, "pr")
            self.gh.pull(number, ticket, created=created, runs=self.WAITING)
            self.gh.behind[number] = 1
        self.go()
        self.assertEqual(self.updates(), ["/repos/%s/pulls/50/update-branch" % REPO,
                                          "/repos/%s/pulls/60/update-branch" % REPO])

    def test_a_sensitive_pull_request_is_kept_current_and_never_merged(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, labels=("class:sensitive",), runs=self.WAITING)
        self.gh.behind[50] = 4
        self.go()
        self.assertEqual(self.updates(), ["/repos/%s/pulls/50/update-branch" % REPO])
        self.assertEqual(self.merged, [])

    def test_a_dirty_sensitive_pull_request_gets_a_fix_round_and_is_never_merged(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=False, labels=("class:sensitive",), runs=self.WAITING)
        self.go()
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.merged, [])

    def test_a_conflict_on_an_operator_held_ticket_still_gets_its_round(self):
        # loom#1599's stall: the ticket sits at loom:human (its merge waits
        # for the operator), and the held send-back path never assigns the
        # conflict. Hygiene maintains the branch anyway: a fix round is not
        # a merge, and the hold stays on the ticket.
        self.gh.ticket(5, "pr")
        self.gh.issues[5]["labels"].append({"name": "loom:human"})
        self.gh.pull(50, 5, mergeable=False, runs=self.WAITING)
        self.go()
        self.assertEqual([x[:2] for x in self.fixed], [(50, (REPO, 5))])
        self.assertEqual(self.merged, [])

    def test_a_conflict_on_a_needs_brain_ticket_waits_for_the_brain(self):
        self.gh.ticket(5, "pr")
        self.gh.issues[5]["labels"].append({"name": "needs-brain"})
        self.gh.pull(50, 5, mergeable=False, runs=self.WAITING)
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.merged, [])

    def test_a_dirty_pull_request_whose_ticket_a_lane_already_holds_is_left_alone(self):
        self.gh.ticket(5, "queued")
        self.gh.pull(50, 5, mergeable=False, runs=self.WAITING)
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.merged, [])


class LunaReview(Base):
    """The Codex Luna review fallback (loom#2142): when the review check is
    degraded (its model route is exhausted) and the review bot's re-ask has
    already failed on the same head, the factory dispatches one review brief
    to the Luna lane slot instead of a fix round no lane can close, and the
    gate reads the lane's verdict for that head like the review bot's own."""

    DEGRADED = {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed",
                "conclusion": "failure",
                "output": {"title": "WARN Grug eyes clouded",
                           "summary": "Degraded review (all_failed): not a verdict. "
                                      "Comment /grug improve to re-run once the backend recovers."},
                "details_url": "https://github.com/%s/actions/runs/80/job/1" % REPO}

    def degraded_pr(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [dict(self.DEGRADED)]

    def writer(self):
        calls = []

        def write(repo, pr_number, sha, issue):
            calls.append((repo, pr_number, sha, int(issue["number"])))
            return True
        return calls, write

    def seed(self, body, ago=0, user=None):
        self.gh.comments.setdefault(50, []).append(
            {"body": body, "user": user or ME, "created_at": iso(ago)})

    def test_parse_luna_verdict_reads_the_markings_line(self):
        body = (factory.LUNA_REVIEW_MARKER % "sha50" +
                "\nElder markings - 2 blocking, 5 total\n\nFindings follow.")
        self.assertEqual(factory.parse_luna_verdict(body, "sha50"), (2, 5))
        self.assertIsNone(factory.parse_luna_verdict(body, "sha99"))
        self.assertIsNone(factory.parse_luna_verdict(
            "Elder markings - 0 blocking, 0 total", "sha50"))
        self.assertIsNone(factory.parse_luna_verdict(
            factory.LUNA_REVIEW_MARKER % "sha50" + "\nno markings here", "sha50"))

    def test_a_degraded_review_still_degraded_after_the_reask_dispatches_a_luna_review(self):
        self.degraded_pr()
        calls, write = self.writer()
        self.go(review_brief=write)
        self.assertEqual(calls, [])  # the first pass only re-asks the review bot
        self.go(review_brief=write)
        self.assertEqual(calls, [(REPO, 50, "sha50", 5)])
        bodies = [str(c.get("body") or "") for c in self.gh.comments[50]]
        self.assertTrue(any(factory.LUNA_REQUEST_MARKER % "sha50" in body for body in bodies))
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.merged, [])

    def test_without_a_review_writer_a_second_degraded_review_is_sent_back_as_before(self):
        self.degraded_pr()
        self.go()
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_a_luna_request_already_in_flight_is_not_dispatched_again(self):
        self.degraded_pr()
        calls, write = self.writer()
        self.go(review_brief=write)
        self.go(review_brief=write)
        self.go(review_brief=write)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.fixed, [])

    def test_a_luna_request_older_than_the_retry_window_is_dispatched_again(self):
        self.degraded_pr()
        self.seed(factory.REVIEW_AGAIN_MARKER % "sha50" + "\n/grug improve")
        self.seed(factory.LUNA_REQUEST_MARKER % "sha50", ago=2 * factory.LUNA_RETRY_AFTER)
        calls, write = self.writer()
        self.go(review_brief=write)
        self.assertEqual(len(calls), 1)

    def test_a_clean_luna_verdict_lets_the_pull_request_land(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.runs["sha50"] = [dict(self.DEGRADED)]
        self.seed(factory.LUNA_REVIEW_MARKER % "sha50" +
                  "\nElder markings - 0 blocking, 2 total\n\nThe change is sound.")
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual(self.fixed, [])

    def test_a_blocking_luna_verdict_does_not_excuse_the_check(self):
        self.degraded_pr()
        self.seed(factory.REVIEW_AGAIN_MARKER % "sha50" + "\n/grug improve")
        self.seed(factory.LUNA_REVIEW_MARKER % "sha50" +
                  "\nElder markings - 2 blocking, 3 total\n\nTwo findings block.")
        calls, write = self.writer()
        self.go(review_brief=write)
        self.assertEqual(calls, [])  # a verdict exists: nothing new to dispatch
        self.assertEqual(self.merged, [])
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_an_untrusted_luna_verdict_is_ignored(self):
        self.degraded_pr()
        self.seed(factory.REVIEW_AGAIN_MARKER % "sha50" + "\n/grug improve")
        self.seed(factory.LUNA_REVIEW_MARKER % "sha50" +
                  "\nElder markings - 0 blocking, 0 total",
                  user={"login": "mallory-example"})
        calls, write = self.writer()
        self.go(review_brief=write)
        self.assertEqual(len(calls), 1)  # no trusted verdict: the lane is asked
        self.assertEqual(self.merged, [])

    def test_a_luna_verdict_for_another_head_is_ignored(self):
        self.degraded_pr()
        self.seed(factory.REVIEW_AGAIN_MARKER % "sha50" + "\n/grug improve")
        self.seed(factory.LUNA_REVIEW_MARKER % "shaOTHER" +
                  "\nElder markings - 0 blocking, 1 total")
        calls, write = self.writer()
        self.go(review_brief=write)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.merged, [])

    def requests(self):
        return [c for c in self.gh.comments.get(50, [])
                if factory.LUNA_REQUEST_MARKER % "sha50" in str(c.get("body") or "")]

    def dispatches(self, count):
        """Run the factory once to re-ask, then `count` passes a retry window
        apart; returns the writer's calls."""
        self.degraded_pr()
        calls, write = self.writer()
        self.go(review_brief=write)
        for n in range(count):
            self.go(review_brief=write, now=NOW + n * (factory.LUNA_RETRY_AFTER + 1))
        return calls

    def test_two_dispatches_for_one_head_leave_one_comment_edited(self):
        calls = self.dispatches(2)
        self.assertEqual(len(calls), 2)
        found = self.requests()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["body"].count("Dispatch "), 2)

    def test_three_dispatches_without_a_verdict_file_one_factory_ticket_and_stop(self):
        calls = self.dispatches(3)
        self.assertEqual(len(calls), 3)
        later = self.writer()
        for n in (4, 6):
            self.go(review_brief=later[1], now=NOW + n * (factory.LUNA_RETRY_AFTER + 1))
        self.assertEqual(later[0], [])
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.gh.filed), 1)
        self.assertIn("loom:factory", self.gh.filed[0]["labels"])
        self.assertIn("sha50", self.gh.filed[0]["title"])
        self.assertEqual(self.fixed, [])


if __name__ == "__main__":
    unittest.main()
