"""Muse's builder loop (docs/MUSE.md, Build): bus/muse_build.py picks one
buildable ticket, claims it as claim:muse, and opens the pull request as
the agent-meta-muse App."""
import io
import json
import os
import re
import sys
import unittest
import urllib.parse
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import muse_build, sweep  # noqa: E402
from bus.github import GitHubError, Response  # noqa: E402

MUSE = "agent-meta-muse[bot]"
REPOS = ["example/alpha", "example/beta"]
OPERATOR = "operator-example"
STRANGER = "stranger-example"


def setUpModule():
    # The briefing logins (docs/MUSE.md): only their tickets, or those of a
    # repo owner, member or collaborator, are built.
    patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": OPERATOR + " loom-example[bot]",
                                           "LOOM_TRUSTED_BRIEFERS": "briefer-example"})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def body(size="S", boxes=True):
    criteria = "- [ ] It works.\n- [ ] A test fails without it.\n" if boxes else "It works.\n"
    return ("## Why\nThe thing is broken for every reader.\n\n## Acceptance criteria\n"
            + criteria + "\nSize: %s\n\n## Out of scope\nOther things.\n" % size)


def issue(repo, number, title="Fix the thing", labels=(), created="2026-01-01T00:00:00Z",
          size="S", boxes=True, pull=False, author=OPERATOR, association="NONE"):
    found = {"number": number, "title": title, "body": body(size, boxes), "state": "open",
             "user": {"login": author}, "author_association": association,
             "created_at": created, "labels": [{"name": x} for x in labels],
             "repository_url": "https://api.github.com/repos/" + repo,
             "html_url": "https://github.com/%s/issues/%d" % (repo, number)}
    if pull:
        found["pull_request"] = {"url": "x"}
    return found


class FakeGitHub:
    """Answers the reads and writes bus/muse_build.py makes. `search` maps a
    substring of the query to the items it returns; `timelines` holds each
    issue's timeline; `race` names a claim label another holder adds right
    after ours lands."""

    def __init__(self, issues=(), login=MUSE, own_prs=(), timelines=None, race=None,
                 comments=None):
        self.issues = {(i["repository_url"].split("/repos/")[1], i["number"]): i for i in issues}
        self.login = login
        self.own_prs = list(own_prs)
        self.timelines = timelines or {}
        self.race = race
        self.comments = comments or {}
        self.calls = []
        self.labels_made = set()
        self.pr_branch = "muse/1-fix-the-thing"

    def app_login(self):
        return self.login

    def rest(self, method, path, body=None, ok=(), **kwargs):
        self.calls.append((method, path, body))
        if path.startswith("/search/issues"):
            return self._search(path)
        for verb, pattern, handler in self.ROUTES:
            m = re.fullmatch(pattern, path)
            if m and method == verb:
                return handler(self, m.group(1), *m.groups()[1:], body=body)
        return Response(200, {})

    def _search(self, path):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        q = query["q"][0]
        if "is:pr" in q:
            return Response(200, {"items": list(self.own_prs)})
        if 'label:"claim:muse"' in q:
            return Response(200, {"items": [i for i in self.issues.values()
                                            if "claim:muse" in [x["name"] for x in i["labels"]]]})
        page = int(query.get("page", ["1"])[0])
        items = sorted(self.issues.values(), key=lambda i: i["created_at"])
        return Response(200, {"items": items[(page - 1) * 100:page * 100]})

    def _get_issue(self, repo, number, body=None):
        return Response(200, self.issues[(repo, int(number))])

    def _get_label(self, repo, name, body=None):
        made = (repo, urllib.parse.unquote(name)) in self.labels_made
        return Response(200, {}) if made else Response(404, None)

    def _make_label(self, repo, body=None):
        self.labels_made.add((repo, body["name"]))
        return Response(201, {})

    def _add_labels(self, repo, number, body=None):
        found = self.issues[(repo, int(number))]
        found["labels"] += [{"name": x} for x in body["labels"]]
        if self.race:
            found["labels"].append({"name": self.race})
        return Response(200, found["labels"])

    def _drop_label(self, repo, number, name, body=None):
        found = self.issues[(repo, int(number))]
        gone = urllib.parse.unquote(name)
        found["labels"] = [x for x in found["labels"] if x["name"] != gone]
        return Response(200, [])

    def _comment(self, repo, number, body=None):
        self.comments.setdefault((repo, int(number)), []).append(
            {"id": 1, "body": body["body"], "user": {"login": self.login or "someone"}})
        return Response(201, {"id": 1})

    def _get_pull(self, repo, number, body=None):
        return Response(200, {"number": int(number), "head": {"ref": self.pr_branch}})

    def _open_pull(self, repo, body=None):
        return Response(201, {"number": 77, "html_url": "https://example.com/pull/77"})

    REPO = r"/repos/([^/]+/[^/]+)"
    ROUTES = (
        ("GET", REPO + r"/issues/(\d+)", _get_issue),
        ("GET", REPO + r"/labels/(.+)", _get_label),
        ("POST", REPO + r"/labels", _make_label),
        ("POST", REPO + r"/issues/(\d+)/labels", _add_labels),
        ("DELETE", REPO + r"/issues/(\d+)/labels/(.+)", _drop_label),
        ("POST", REPO + r"/issues/(\d+)/comments", _comment),
        ("GET", REPO + r"/pulls/(\d+)", _get_pull),
        ("POST", REPO + r"/pulls", _open_pull),
    )

    def paginate(self, path):
        self.calls.append(("GET", path, None))
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/(timeline|comments).*", path)
        if m:
            key = (m.group(1), int(m.group(2)))
            store = self.timelines if m.group(3) == "timeline" else self.comments
            return list(store.get(key, []))
        return []

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def open_pr_naming(number):
    return [{"event": "cross-referenced",
             "source": {"issue": {"number": number + 100, "state": "open",
                                  "pull_request": {"url": "x"}}}}]


class PickTest(unittest.TestCase):
    def test_pick_takes_the_highest_priority_then_the_oldest_buildable_ticket(self):
        gh = FakeGitHub([issue("example/alpha", 1, created="2026-01-01T00:00:00Z", labels=["P2"]),
                         issue("example/beta", 2, created="2026-01-03T00:00:00Z", labels=["P1"]),
                         issue("example/alpha", 3, created="2026-01-02T00:00:00Z", labels=["P1"])])
        picked = muse_build.pick(gh, REPOS)
        self.assertEqual((picked.repo, picked.number), ("example/alpha", 3))

    def test_pick_skips_held_claimed_lane_labelled_oversized_and_unbuildable_tickets(self):
        gh = FakeGitHub([
            issue("example/alpha", 1, labels=["claim:ccqw"]),
            issue("example/alpha", 2, labels=["lane:codex"]),
            issue("example/alpha", 3, labels=["needs:human"]),
            issue("example/alpha", 4, labels=["epic"]),
            issue("example/alpha", 5, labels=["awaiting-live-check"]),
            issue("example/alpha", 6, size="L"),
            issue("example/alpha", 7, boxes=False),
            issue("example/other", 8),
            issue("example/alpha", 9, pull=True),
            issue("example/alpha", 10, labels=["loom:running"]),
            issue("example/alpha", 11, labels=["loom:queued", "loom:ticket"],
                  created="2026-02-01T00:00:00Z"),
        ])
        picked = muse_build.pick(gh, REPOS)
        self.assertEqual((picked.repo, picked.number), ("example/alpha", 11))

    def test_pick_skips_an_epic_or_goal_without_the_label(self):
        parent = issue("example/alpha", 2)
        parent["sub_issues_summary"] = {"total": 3, "completed": 1}
        gh = FakeGitHub([issue("example/alpha", 1, title="Epic: make the thing reliable"),
                         parent,
                         issue("example/alpha", 3, title="Goal: everything works"),
                         issue("example/alpha", 4, created="2026-02-01T00:00:00Z")])
        self.assertEqual(muse_build.pick(gh, REPOS).number, 4)

    def test_pick_skips_a_ticket_an_open_pull_request_names(self):
        gh = FakeGitHub([issue("example/alpha", 1), issue("example/alpha", 2,
                                                          created="2026-01-05T00:00:00Z")],
                        timelines={("example/alpha", 1): open_pr_naming(1)})
        picked = muse_build.pick(gh, REPOS)
        self.assertEqual(picked.number, 2)

    def test_pick_skips_a_strangers_ticket_on_a_public_repo(self):
        # Anyone can file an issue on a public repo; an autonomous builder
        # must not take its instructions from a stranger.
        gh = FakeGitHub([issue("example/alpha", 1, author=STRANGER),
                         issue("example/alpha", 2, author=STRANGER, association="CONTRIBUTOR"),
                         issue("example/alpha", 3, created="2026-02-01T00:00:00Z")])
        self.assertEqual(muse_build.pick(gh, REPOS).number, 3)

    def test_pick_trusts_owners_members_collaborators_and_briefing_logins(self):
        for author, association, labels in (
                ("someone-example", "OWNER", ()), ("someone-example", "MEMBER", ()),
                ("someone-example", "COLLABORATOR", ()), (OPERATOR, "NONE", ()),
                ("briefer-example", "NONE", ("loom:ticket",))):
            with self.subTest(author=author, association=association):
                gh = FakeGitHub([issue("example/alpha", 1, author=author,
                                       association=association, labels=labels)])
                self.assertEqual(muse_build.pick(gh, REPOS).number, 1)

    def test_a_trusted_briefer_counts_only_on_a_ticket(self):
        gh = FakeGitHub([issue("example/alpha", 1, author="briefer-example")])
        self.assertIsNone(muse_build.pick(gh, REPOS))

    def test_claim_refuses_a_strangers_ticket(self):
        gh = FakeGitHub([issue("example/alpha", 1, author=STRANGER)])
        with self.assertRaises(muse_build.ClaimLost):
            muse_build.claim(gh, "example/alpha", 1)
        self.assertEqual(gh.writes(), [])

    def test_pick_searches_public_repos_only(self):
        gh = FakeGitHub([issue("example/alpha", 1)])
        muse_build.pick(gh, REPOS)
        queries = [urllib.parse.parse_qs(urllib.parse.urlparse(p).query)["q"][0]
                   for m, p, _ in gh.calls if p.startswith("/search/issues")]
        self.assertTrue(queries)
        self.assertTrue(all("is:public" in q for q in queries), queries)

    def test_pick_reads_past_the_oldest_three_hundred(self):
        old = [issue("example/alpha", n, labels=["claim:dot"],
                     created="2026-01-01T00:%02d:%02dZ" % (n // 60, n % 60)) for n in range(1, 351)]
        gh = FakeGitHub(old + [issue("example/alpha", 999, created="2026-06-01T00:00:00Z")])
        self.assertEqual(muse_build.pick(gh, REPOS).number, 999)

    def test_pick_returns_none_when_nothing_is_buildable(self):
        gh = FakeGitHub([issue("example/alpha", 1, labels=["claim:dot"])])
        self.assertIsNone(muse_build.pick(gh, REPOS))


class NextTest(unittest.TestCase):
    def test_next_refuses_a_build_identity_other_than_agent_meta_muse(self):
        gh = FakeGitHub([issue("example/alpha", 1)], login="loom-brain[bot]")
        with self.assertRaises(muse_build.WrongIdentity) as caught:
            muse_build.next_work(gh, REPOS)
        self.assertIn("loom-brain[bot]", str(caught.exception))
        self.assertIn(MUSE, str(caught.exception))
        self.assertEqual(gh.writes(), [])

    def test_an_installation_token_is_known_by_its_viewer_login(self):
        # A minted agent-meta-muse installation token in LOOM_GITHUB_TOKEN has
        # no App JWT, so GET /app cannot name it; GraphQL's viewer does.
        gh = FakeGitHub([issue("example/alpha", 1)], login=None)
        gh.graphql = lambda query, variables=None: {"viewer": {"login": MUSE}}
        self.assertEqual(muse_build.check_identity(gh), MUSE)
        gh.graphql = lambda query, variables=None: {"viewer": {"login": "someone-example"}}
        with self.assertRaises(muse_build.WrongIdentity):
            muse_build.check_identity(gh)

    def test_next_finishes_its_own_open_pull_request_before_claiming(self):
        pr = {"number": 40, "title": "Fix it", "html_url": "https://github.com/example/alpha/pull/40",
              "repository_url": "https://api.github.com/repos/example/alpha", "pull_request": {}}
        gh = FakeGitHub([issue("example/alpha", 1)], own_prs=[pr])
        work = muse_build.next_work(gh, REPOS)
        self.assertEqual((work.kind, work.repo, work.number), ("pr", "example/alpha", 40))
        self.assertEqual(work.branch, "muse/1-fix-the-thing")
        self.assertEqual(work.ticket, 1)
        self.assertEqual(gh.writes(), [])

    def test_an_own_pull_request_outside_the_repos_is_not_the_work(self):
        pr = {"number": 40, "title": "Fix it", "html_url": "https://github.com/example/other/pull/40",
              "repository_url": "https://api.github.com/repos/example/other", "pull_request": {}}
        gh = FakeGitHub([issue("example/alpha", 1)], own_prs=[pr])
        work = muse_build.next_work(gh, REPOS)
        self.assertEqual((work.kind, work.number), ("new", 1))

    def test_a_held_ticket_whose_pull_request_merged_is_released_not_rebuilt(self):
        held = issue("example/beta", 5, labels=["claim:muse"])
        merged = [{"event": "cross-referenced",
                   "source": {"issue": {"number": 50, "state": "closed",
                                        "pull_request": {"merged_at": "2026-03-02T00:00:00Z"},
                                        "user": {"login": MUSE}}}}]
        gh = FakeGitHub([held, issue("example/alpha", 1, created="2026-04-01T00:00:00Z")],
                        timelines={("example/beta", 5): merged},
                        comments={("example/beta", 5): [
                            {"id": 9, "body": muse_build.CLAIM_MARKER + "\nClaimed.",
                             "user": {"login": MUSE}}]})
        work = muse_build.next_work(gh, REPOS)
        self.assertEqual((work.kind, work.repo, work.number), ("new", "example/alpha", 1))
        self.assertNotIn("claim:muse", [x["name"] for x in gh.issues[("example/beta", 5)]["labels"]])
        self.assertTrue(gh.comments[("example/beta", 5)][-1]["body"].startswith(
            muse_build.RELEASE_MARKER))

    def test_next_resumes_its_own_claim_before_claiming_a_new_one(self):
        held = issue("example/beta", 5, labels=["claim:muse"], created="2026-03-01T00:00:00Z")
        gh = FakeGitHub([issue("example/alpha", 1), held],
                        comments={("example/beta", 5): [
                            {"id": 9, "body": muse_build.CLAIM_MARKER + "\nClaimed.",
                             "user": {"login": MUSE}}]})
        work = muse_build.next_work(gh, REPOS)
        self.assertEqual((work.kind, work.repo, work.number), ("claim", "example/beta", 5))
        self.assertEqual(work.branch, "muse/5-fix-the-thing")
        self.assertEqual(gh.writes(), [])

    def test_next_claims_the_picked_ticket_with_the_label_and_a_marked_comment(self):
        gh = FakeGitHub([issue("example/alpha", 1)])
        work = muse_build.next_work(gh, REPOS)
        self.assertEqual((work.kind, work.repo, work.number), ("new", "example/alpha", 1))
        labels = [x["name"] for x in gh.issues[("example/alpha", 1)]["labels"]]
        self.assertIn("claim:muse", labels)
        posted = gh.comments[("example/alpha", 1)][-1]["body"]
        self.assertTrue(posted.startswith(muse_build.CLAIM_MARKER))
        self.assertIn("-- muse", posted)

    def test_the_marker_is_posted_before_the_label(self):
        gh = FakeGitHub([issue("example/alpha", 1)])
        muse_build.claim(gh, "example/alpha", 1)
        writes = [(m, p) for m, p, _ in gh.writes()]
        marker = writes.index(("POST", "/repos/example/alpha/issues/1/comments"))
        label = writes.index(("POST", "/repos/example/alpha/issues/1/labels"))
        self.assertLess(marker, label)

    def test_a_claim_backs_off_when_another_holder_lands_at_the_same_time(self):
        gh = FakeGitHub([issue("example/alpha", 1)], race="claim:dot")
        with self.assertRaises(muse_build.ClaimLost):
            muse_build.claim(gh, "example/alpha", 1)
        labels = [x["name"] for x in gh.issues[("example/alpha", 1)]["labels"]]
        self.assertNotIn("claim:muse", labels)
        self.assertIn("claim:dot", labels)
        self.assertTrue(gh.comments[("example/alpha", 1)][-1]["body"].startswith(
            muse_build.RELEASE_MARKER))

    def test_the_cli_prints_the_work_as_json_and_exits_1_when_there_is_none(self):
        gh = FakeGitHub([issue("example/alpha", 1)])
        out = io.StringIO()
        with mock.patch.object(muse_build, "_client", return_value=gh), redirect_stdout(out):
            code = muse_build.main(["next", "--repos", " ".join(REPOS)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["number"], 1)
        empty = FakeGitHub([issue("example/alpha", 1, labels=["claim:dot"])])
        out = io.StringIO()
        with mock.patch.object(muse_build, "_client", return_value=empty), redirect_stdout(out):
            code = muse_build.main(["next", "--repos", " ".join(REPOS)])
        self.assertEqual(code, 1)
        self.assertIn("nothing buildable", out.getvalue())


GOOD_BODY = ("## Why\nThe thing is broken for every reader today.\n\n## Summary\nFixes it.\n\n"
             "## Acceptance criteria\n- It works.\n- A test fails without it.\n- CI is green.\n\n"
             "## Test plan\n- Unit test.\n- Full suite.\n- CI.\n\n## Size\n\nS\n\n"
             "## Out of scope\nOther things.\n\nRefs %s#%d\n\n-- muse\n" % ("example/alpha", 1))


class PullRequestTest(unittest.TestCase):
    def test_a_body_without_the_refs_line_or_with_a_closing_keyword_is_refused(self):
        self.assertEqual(muse_build.body_problems(GOOD_BODY, "example/alpha", 1), [])
        self.assertTrue(muse_build.body_problems(GOOD_BODY.replace("Refs %s#%d" % ("example/alpha", 1), ""),
                                                 "example/alpha", 1))
        self.assertTrue(muse_build.body_problems(GOOD_BODY + "\nFixes #1\n", "example/alpha", 1))
        self.assertTrue(muse_build.body_problems(GOOD_BODY.replace("## Out of scope", "## Later"),
                                                 "example/alpha", 1))

    def test_the_pull_request_opens_from_the_muse_branch_as_agent_meta_muse(self):
        gh = FakeGitHub([issue("example/alpha", 1)])
        with mock.patch.object(muse_build, "_git", return_value="muse/1-fix-the-thing"):
            made = muse_build.open_pr(gh, "example/alpha", 1, "main", GOOD_BODY)
        self.assertEqual(made["number"], 77)
        posts = [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/pulls")]
        self.assertEqual(posts[0][2]["head"], "muse/1-fix-the-thing")
        self.assertEqual(posts[0][2]["base"], "main")

    def test_the_pull_request_is_refused_from_another_branch_or_identity(self):
        gh = FakeGitHub([issue("example/alpha", 1)])
        with mock.patch.object(muse_build, "_git", return_value="loom/1-fix-the-thing"):
            with self.assertRaises(ValueError):
                muse_build.open_pr(gh, "example/alpha", 1, "main", GOOD_BODY)
        other = FakeGitHub([issue("example/alpha", 1)], login="loom-lanes[bot]")
        with mock.patch.object(muse_build, "_git", return_value="muse/1-fix-the-thing"):
            with self.assertRaises(muse_build.WrongIdentity):
                muse_build.open_pr(other, "example/alpha", 1, "main", GOOD_BODY)


class MuseDocTest(unittest.TestCase):
    def section(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "MUSE.md"), encoding="utf-8") as f:
            doc = f.read()
        return doc, " ".join(doc.split("## Build: the :10 tick", 1)[1]
                             .split("\n## ", 1)[0].split())

    def test_the_build_tick_comes_before_the_brain_steps_and_names_every_command(self):
        doc, build = self.section()
        self.assertLess(doc.index("## Build: the :10 tick"), doc.index("## Tick\n"))
        for command in ("python3 -m bus.muse_build next", "python3 -m bus.muse_build push",
                        "python3 -m bus.muse_build pr", "python3 -m bus.muse_build release"):
            self.assertIn(command, build)
        self.assertIn("takes no brain lock", build)
        self.assertIn("3 merged pull requests a day", build)
        self.assertIn("agent-meta-muse", build)

    def test_the_lock_step_leaves_the_10_tick_to_building(self):
        doc, _ = self.section()
        step = " ".join(doc.split("\n1. Run `python3 -m bus lock", 1)[1].split("\n2. ", 1)[0].split())
        self.assertIn("Only the :40 tick runs these steps", step)


class SweepLeavesBuildClaimsTest(unittest.TestCase):
    def test_the_sweep_releases_a_dead_ticks_claim_but_keeps_a_builder_claim(self):
        class Gh:
            def __init__(self):
                self.deleted = []

            def paginate(self, path):
                if "/issues?state=open" in path:
                    return [{"number": 1}, {"number": 2}]
                if path.startswith("/repos/quadseven/loom/issues/2/comments"):
                    return [{"body": muse_build.CLAIM_MARKER + "\nClaimed.",
                             "user": {"login": MUSE}}]
                return []

            def rest(self, method, path, body=None, ok=()):
                if method == "DELETE":
                    self.deleted.append(path)
                return Response(200, {})

        gh = Gh()
        result = sweep.Result()
        sweep.release_leftovers(gh, ["quadseven/loom"], result)
        self.assertEqual(len(gh.deleted), 1)
        self.assertIn("/issues/1/labels/", gh.deleted[0])


if __name__ == "__main__":
    unittest.main()
