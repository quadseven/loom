import base64
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import hunt_plan, lane, station  # noqa: E402

# The lane's own App login, trusted as the station's LOOM_BRIEF_AUTHORS is
# (docs/MUSE.md): every comment the fake posts is written as it.
APP = "example-app[bot]"
OUTSIDER = "outsider-example"


def setUpModule():
    patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "operator-example " + APP})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)

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
        self.closed = []
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
                {"id": 100 + len(self.comments[n]), "body": body["body"], "created_at": "now",
                 "user": {"login": APP}})
        m = re.search(r"/issues/comments/(\d+)$", path)
        if m and method == "PATCH":
            for found in self.comments.values():
                for comment in found:
                    if comment["id"] == int(m.group(1)):
                        comment["body"] = body["body"]
        if path.endswith("/contents/.loom/sensitive-paths"):
            return type("Response", (), {"data": {"content": base64.b64encode(b"secret/*").decode()}})()
        if method == "GET" and "/pulls?state=closed" in path:
            return type("Response", (), {"data": list(self.closed)})()
        if path.endswith("/pulls") and method == "POST":
            return type("Response", (), {"data": {"number": 9, "html_url": "https://example.com/pr/9"}})()
        return type("Response", (), {"data": {}})()

    def app_login(self):
        return APP

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

    def stranded(self, closed):
        # A ticket held by the lane whose work merged from a branch that is not
        # the ticket branch (loom#412 merged by #413, loom#414 by #415).
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        gh.closed = closed
        return gh

    def merged(self, body, ref="fix/other-branch", number=13, merged=True):
        return {"number": number, "state": "closed", "body": body,
                "merged_at": "2026-10-20T10:00:00Z" if merged else None,
                "head": {"ref": ref}}

    def test_a_merged_pull_request_on_another_branch_naming_the_ticket_advances_it(self):
        # The full form is built, not written, so the diff holds no cross-repo ref.
        for line in ("Refs #4", "Refs " + "example/project" + "#4", "Refs #4 and more words"):
            gh = self.stranded([self.merged("## Why\nx\n\n%s\n" % line)])
            active = lane._recover_and_count(gh, ["example/project"], "muse", 1001)
            self.assertEqual(active, 0, line)
            self.assertIn("loom:pr", labels(gh), line)
            self.assertNotIn("loom:running", labels(gh), line)
            self.assertFalse(any(x["body"].startswith("[transient]") for x in gh.comments[4]))

    def test_a_stranded_ticket_is_advanced_not_handed_back_when_its_claim_expired(self):
        gh = self.stranded([self.merged("Refs #4")])
        lane._recover_and_count(gh, ["example/project"], "muse", 1000 + lane.PR_AGE + 1)
        self.assertIn("loom:pr", labels(gh))
        self.assertNotIn("loom:queued", labels(gh))

    def test_a_mention_or_another_ticket_or_an_unmerged_pull_does_not_claim_the_ticket(self):
        for pull in (self.merged("This follows up #4 but is its own change"),
                     self.merged("See ticket #4.\nRefs #44\nRefs #5"),
                     self.merged("Refs " + "other/project" + "#4"),
                     self.merged("Refs #4", merged=False),
                     self.merged("not Refs #4 at the start of a line")):
            gh = self.stranded([pull])
            active = lane._recover_and_count(gh, ["example/project"], "muse", 1001)
            self.assertEqual(active, 1, pull["body"])
            self.assertIn("loom:running", labels(gh), pull["body"])
            self.assertNotIn("loom:pr", labels(gh), pull["body"])

    def test_the_branch_match_stays_primary_and_reads_no_closed_pulls(self):
        gh = self.stranded([])
        gh.pulls = [{"number": 9, "head": {"ref": "loom/4-add-a-feature"}}]
        lane._recover_and_count(gh, ["example/project"], "muse", 1001)
        self.assertIn("loom:pr", labels(gh))
        self.assertFalse([c for c in gh.calls if "state=closed" in c[1]])

    def test_a_heartbeat_keeps_a_claim_and_a_claim_with_no_pr_expires_after_pr_age(self):
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        lane.heartbeat(gh, "example/project", 4, now=1000 + lane.CLAIM_AGE - 10)
        self.assertEqual(lane._recover_and_count(
            gh, ["example/project"], "muse", 1000 + lane.CLAIM_AGE + 10), 1)
        lane._recover_and_count(gh, ["example/project"], "muse", 1000 + lane.PR_AGE + 1)
        self.assertIn("loom:queued", labels(gh))

    def test_heartbeats_after_a_claim_post_nothing_and_edit_the_claim_comment(self):
        # 2026-10-20: a new remote-claim comment every beat, six in 31 minutes
        # on one ticket, each one emailed to its author.
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        for at in (1500, 2000, 2500):
            lane.heartbeat(gh, "example/project", 4, now=at)
        self.assertEqual(len(gh.comments[4]), 1)
        posts = [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/issues/4/comments")]
        patches = [c for c in gh.calls if c[0] == "PATCH" and "/issues/comments/" in c[1]]
        self.assertEqual((len(posts), len(patches)), (1, 3))
        self.assertEqual(lane._claims(gh.comments[4], "muse"), [(0, 2500.0, 1000.0)])
        # The claim expires from the newest beat, and the first claim time
        # still bounds how long a claim without a pull request may last.
        repos = ["example/project"]
        self.assertEqual(lane._recover_and_count(gh, repos, "muse", 2500 + lane.CLAIM_AGE - 1), 1)
        lane._recover_and_count(gh, repos, "muse", 2500 + lane.CLAIM_AGE + 1)
        self.assertIn("loom:queued", labels(gh))

    def test_a_heartbeat_edit_keeps_the_first_claim_time_for_the_pr_age_bound(self):
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        lane.heartbeat(gh, "example/project", 4, now=1000 + lane.CLAIM_AGE - 10)
        lane.heartbeat(gh, "example/project", 4, now=1000 + lane.PR_AGE - 10)
        self.assertEqual(len(gh.comments[4]), 1)
        lane._recover_and_count(gh, ["example/project"], "muse", 1000 + lane.PR_AGE + 1)
        self.assertIn("loom:queued", labels(gh))

    def test_a_new_claim_after_a_hand_back_is_not_aged_by_the_old_one(self):
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        lane._recover_and_count(gh, ["example/project"], "muse", 1000 + lane.PR_AGE + 1)
        self.assertIn("loom:queued", labels(gh))
        gh.issues[4]["labels"].append({"name": "lane:muse"})  # the brain re-assigns the lane
        later = 1000 + lane.PR_AGE + 100
        self.assertIsNotNone(lane.claim(gh, ["example/project"], now=later))
        self.assertEqual(lane._recover_and_count(gh, ["example/project"], "muse", later + 60), 1)
        self.assertNotIn("loom:queued", labels(gh))

    def test_three_heartbeats_with_no_claim_comment_post_one_and_edit_it_twice(self):
        gh = FakeGitHub()
        gh.issues[4]["labels"] = [{"name": "loom:running"}, {"name": "lane:muse"}]
        for at in (1000, 1100, 1200):
            lane.heartbeat(gh, "example/project", 4, now=at)
        # Three beats on one claim: one comment posted, then edited twice.
        self.assertEqual(len(gh.comments[4]), 1)
        self.assertEqual([c[0] for c in gh.calls if "/comments" in c[1] and c[0] in ("POST", "PATCH")],
                         ["POST", "PATCH", "PATCH"])
        self.assertEqual(gh.comments[4][0]["body"], "<!-- loom:remote-claim v1 muse 4 1200.000 1000.000 -->")
        gh.comments[4][0]["body"] = "<!-- loom:remote-claim v1 muse 4 1000.000 -->"
        # A comment from before the edit-in-place form is read as a claim and
        # edited into the new form on the next beat.
        self.assertEqual(lane._claims(gh.comments[4], "muse"), [(0, 1000.0, 1000.0)])
        lane.heartbeat(gh, "example/project", 4, now=2000)
        self.assertEqual(len(gh.comments[4]), 1)
        self.assertEqual(gh.comments[4][0]["body"], "<!-- loom:remote-claim v1 muse 4 2000.000 1000.000 -->")

    def test_the_lane_heartbeat_edits_one_comment(self):
        gh = FakeGitHub()
        lane.lane_heartbeat(gh, "example/project", now=1000)
        lane.lane_heartbeat(gh, "example/project", now=2000)
        self.assertEqual(len(gh.comments[9]), 1)
        self.assertIn(" 2000.000 ", gh.comments[9][0]["body"])

    def test_the_lane_heartbeat_edits_its_comment_when_the_env_never_named_its_app(self):
        # loom#2008: a lane launched without LOOM_BRIEF_AUTHORS naming its
        # App login posted a fresh status comment every tick (58 duplicates
        # in 7 hours, each emailed to the status issue's author). The lane's
        # own comments count as its own, whoever the environment named.
        gh = FakeGitHub()
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "operator-example"}):
            lane.lane_heartbeat(gh, "example/project", now=1000)
            lane.lane_heartbeat(gh, "example/project", now=2000)
        self.assertEqual(len(gh.comments[9]), 1)
        self.assertIn(" 2000.000 ", gh.comments[9][0]["body"])

    def test_heartbeats_edit_the_claim_when_the_env_never_named_its_app(self):
        # loom#2008, the claim path: same environment, a held ticket's beats
        # edit the one claim comment instead of posting one per beat.
        gh = FakeGitHub()
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "operator-example"}):
            lane.claim(gh, ["example/project"], now=1000)
            for at in (1500, 2000):
                lane.heartbeat(gh, "example/project", 4, now=at)
        self.assertEqual(len(gh.comments[4]), 1)
        self.assertEqual(lane._claims(gh.comments[4], "muse"), [(0, 2000.0, 1000.0)])

    def test_the_lane_heartbeat_edits_its_comment_when_the_env_is_unset(self):
        # loom#2008 with LOOM_BRIEF_AUTHORS absent entirely, not just
        # missing the App login.
        gh = FakeGitHub()
        with mock.patch.dict(os.environ):
            os.environ.pop("LOOM_BRIEF_AUTHORS", None)
            lane.lane_heartbeat(gh, "example/project", now=1000)
            lane.lane_heartbeat(gh, "example/project", now=2000)
        self.assertEqual(len(gh.comments[9]), 1)
        self.assertIn(" 2000.000 ", gh.comments[9][0]["body"])

    def test_a_failed_app_login_read_falls_back_to_the_env_authors(self):
        # GitHub.app_login raises GitHubError when GET /app fails; the beat
        # still lands on the env-trusted comment instead of crashing.
        gh = FakeGitHub()
        gh.app_login = mock.Mock(side_effect=lane.GitHubError("no app"))
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

    def test_claim_skips_queued_ticket_whose_branch_has_a_pull_request(self):
        gh = FakeGitHub()
        gh.pulls = [{"number": 99, "state": "open"}]
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            got = lane.claim(gh, ["example/project"], now=1000)
        self.assertIsNone(got)
        self.assertIn("loom:queued", labels(gh))
        self.assertNotIn("loom:running", labels(gh))
        self.assertIn("scanned 1 repos, found 1 candidates", buf.getvalue())
        self.assertIn("#4 skipped: branch loom/4-add-a-feature already has pull request #99", buf.getvalue())

    def test_claim_with_no_repos_exits_2(self):
        with mock.patch.dict(os.environ, {"LOOM_REPOS": ""}):
            buf = io.StringIO()
            with mock.patch("sys.stderr", buf):
                code = lane.main(["claim", "--lane", "muse", "--repos", ""])
            self.assertEqual(code, 2)
            self.assertIn("error: claim requires --repos or LOOM_REPOS", buf.getvalue())

    def test_claim_prints_stderr_line_per_run(self):
        gh = FakeGitHub()
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            got = lane.claim(gh, ["example/project"], now=1000)
        self.assertIsNotNone(got)
        self.assertIn("scanned 1 repos, found 1 candidates", buf.getvalue())

    def test_claim_command_prints_json_on_claim(self):
        gh = FakeGitHub()
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("bus.lane.auth_from_env", return_value="dummy"):
            with mock.patch("bus.lane.GitHub", return_value=gh):
                with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                    code = lane.main(["claim", "--lane", "muse", "--repos", "example/project"])
        self.assertEqual(code, 0)
        self.assertIn("scanned 1 repos, found 1 candidates", err.getvalue())
        data = json.loads(out.getvalue())
        self.assertEqual(data["number"], 4)


class MuseSlotsTests(unittest.TestCase):
    """Four Muse lanes (operator, 2026-10-25: "Yes I want muse lanes... Like 4")."""

    def test_a_fourth_claim_is_taken_and_a_fifth_is_refused(self):
        gh = FakeGitHub()
        with mock.patch.object(lane, "_recover_and_count", return_value=3):
            self.assertEqual(lane.claim(gh, ["example/project"], now=1000)["number"], 4)
        gh = FakeGitHub()
        with mock.patch.object(lane, "_recover_and_count", return_value=4):
            self.assertIsNone(lane.claim(gh, ["example/project"], now=1000))

    def test_a_ticket_with_a_fresh_claim_is_never_claimed_twice_while_slots_are_free(self):
        gh = FakeGitHub()
        self.assertEqual(lane.claim(gh, ["example/project"], now=1000)["number"], 4)
        # A hand-back race leaves the ticket queued while its claim is still fresh.
        gh.issues[4]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"},
                                  {"name": "lane:muse"}]
        with mock.patch.object(lane, "_recover_and_count", return_value=1):
            self.assertIsNone(lane.claim(gh, ["example/project"], now=1001))
        self.assertEqual(sum("loom:claim" in c["body"] or "claim" in c["body"].lower()
                             for c in gh.comments[4]), 1)


class OutsiderCommentTests(unittest.TestCase):
    """Anyone can comment on a public repo (loom#1956): an outsider's copy of
    a claim or heartbeat marker is data, never the lane's."""

    def outsider(self, gh, number, body):
        gh.comments.setdefault(number, []).append(
            {"id": 900 + len(gh.comments[number]), "body": body, "created_at": "now",
             "user": {"login": OUTSIDER}})

    def test_a_forged_claim_does_not_hold_a_queued_ticket(self):
        gh = FakeGitHub()
        self.outsider(gh, 4, "<!-- loom:remote-claim v1 muse 4 999.000 -->")
        got = lane.claim(gh, ["example/project"], now=1000)
        self.assertIsNotNone(got)
        self.assertEqual(4, got["number"])

    def test_a_forged_fresh_claim_does_not_keep_a_stale_ticket_running(self):
        gh = FakeGitHub()
        lane.claim(gh, ["example/project"], now=1000)
        later = 1000 + lane.CLAIM_AGE + 1
        self.outsider(gh, 4, "<!-- loom:remote-claim v1 muse 4 %.3f -->" % later)
        lane._recover_and_count(gh, ["example/project"], "muse", later)
        self.assertIn("loom:queued", labels(gh))

    def test_a_heartbeat_never_edits_a_forged_claim(self):
        gh = FakeGitHub()
        gh.issues[4]["labels"] = [{"name": "loom:running"}, {"name": "lane:muse"}]
        self.outsider(gh, 4, "<!-- loom:remote-claim v1 muse 4 999.000 -->")
        lane.heartbeat(gh, "example/project", 4, now=1000)
        self.assertFalse(any(m == "PATCH" for m, _, _ in gh.calls))
        self.assertEqual("<!-- loom:remote-claim v1 muse 4 1000.000 -->", gh.comments[4][-1]["body"])


class DefaultDifficultyTests(unittest.TestCase):
    def test_a_ticket_with_no_difficulty_line_is_claimed_at_the_factorys_default_tier(self):
        # The factory reserves a ticket with no Difficulty: line for Muse at its
        # default tier (medium); the claim must read it the same way, or Muse is
        # handed tickets it never takes (2026-10-26).
        gh = FakeGitHub(body=BODY.replace("Difficulty: medium\n\n", "Priority: P2\n\n"))
        got = lane.claim(gh, ["example/project"], now=1000)
        self.assertIsNotNone(got)
        self.assertEqual(got["number"], 4)

    def test_a_hard_ticket_is_still_not_a_muse_ticket(self):
        gh = FakeGitHub(body=BODY.replace("Difficulty: medium", "Difficulty: hard"))
        self.assertIsNone(lane.claim(gh, ["example/project"], now=1000))


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


class FakeGitDatabase:
    """A GitHub API fake that speaks the git-database endpoints push_branch
    needs: ref reads, blob/tree/commit creation, ref creation."""

    def __init__(self, tree_sha="t" * 40, commit_sha="c" * 40, trees=None,
                 commits=None):
        self.tree_sha = tree_sha
        self.commit_sha = commit_sha
        # Optional per-call answers, consumed in order, for multi-commit pushes.
        self.trees = list(trees or [])
        self.commits = list(commits or [])
        self.refs = {}  # branch -> sha
        self.calls = []
        self.bodies = []

    def _blob_sha(self, content_b64):
        import base64
        import hashlib
        content = base64.b64decode(content_b64)
        # Git's own object id, not a security use: the fake must answer the
        # same blob SHA git computes locally.
        return hashlib.sha1(b"blob %d\0" % len(content) + content,
                            usedforsecurity=False).hexdigest()

    def rest(self, method, path, body=None, **kwargs):
        from bus.github import GitHubError
        from urllib.parse import unquote
        self.calls.append((method, path))
        self.bodies.append((method, path, body))
        if method == "GET" and "/git/refs/heads/" in path:
            branch = unquote(path.split("/git/refs/heads/", 1)[1])
            if branch in self.refs:
                return type("Response", (), {"data": {
                    "object": {"sha": self.refs[branch]}}})()
            raise GitHubError("GET %s -> 404" % path, 404, "")
        if method == "POST" and path.endswith("/git/blobs"):
            return type("Response", (), {"data": {
                "sha": self._blob_sha(body["content"])}})()
        if method == "POST" and path.endswith("/git/trees"):
            sha = self.trees.pop(0) if self.trees else self.tree_sha
            return type("Response", (), {"data": {"sha": sha}})()
        if method == "POST" and path.endswith("/git/commits"):
            sha = self.commits.pop(0) if self.commits else self.commit_sha
            return type("Response", (), {"data": {"sha": sha}})()
        if method == "POST" and path.endswith("/git/refs"):
            branch = body["ref"].split("refs/heads/", 1)[1]
            self.refs[branch] = body["sha"]
            return type("Response", (), {"data": {}})()
        if method == "PATCH" and "/git/refs/heads/" in path:
            branch = unquote(path.split("/git/refs/heads/", 1)[1])
            self.refs[branch] = body["sha"]
            return type("Response", (), {"data": {}})()
        raise AssertionError("unexpected API call: %s %s" % (method, path))


def _make_repo(tmp, extra_commit=False):
    import subprocess
    def git(*args, **kwargs):
        subprocess.run(["git", *args], capture_output=True, check=True, cwd=kwargs.get("cwd", tmp),
                       env={**__import__("os").environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    origin = __import__("os").path.join(tmp, "origin.git")
    git("init", "--bare", origin)
    work = __import__("os").path.join(tmp, "work")
    __import__("os").makedirs(work)
    git("init", "-b", "main", cwd=work)
    git("remote", "add", "origin", origin, cwd=work)
    with open(__import__("os").path.join(work, "file.txt"), "w") as f:
        f.write("base\n")
    git("add", "file.txt", cwd=work)
    git("commit", "-m", "base", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    git("checkout", "-b", "loom/4-add-a-feature", cwd=work)
    with open(__import__("os").path.join(work, "file.txt"), "w") as f:
        f.write("changed\n")
    git("add", "file.txt", cwd=work)
    git("commit", "-m", "change", cwd=work)
    if extra_commit:
        with open(__import__("os").path.join(work, "second.txt"), "w") as f:
            f.write("second\n")
        git("add", "second.txt", cwd=work)
        git("commit", "-m", "second change", cwd=work)
    return work


class PushBranchTests(unittest.TestCase):
    def test_push_creates_remote_branch_via_git_database_api(self):
        import os
        import subprocess
        import tempfile
        from bus import lane
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            tree_sha = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"], capture_output=True,
                text=True, check=True, cwd=repo).stdout.strip()
            gh = FakeGitDatabase(tree_sha=tree_sha)
            issue = {"number": 4, "title": "Add a feature"}
            gh_issue = FakeGitHub()
            # FakeGitHub serves the issue; FakeGitDatabase speaks git-database.
            # Combine: issue GETs go to FakeGitHub, git-database to FakeGitDatabase.
            class Combined:
                def rest(self, method, path, body=None, **kwargs):
                    if "/git/" in path:
                        return gh.rest(method, path, body, **kwargs)
                    return gh_issue.rest(method, path, body, **kwargs)
            old = os.getcwd()
            os.chdir(repo)
            try:
                sha = lane.push_branch(Combined(), "example/project", 4)
            finally:
                os.chdir(old)
            self.assertEqual(sha, gh.commit_sha)
            self.assertEqual(gh.refs["loom/4-add-a-feature"], gh.commit_sha)
            posts = [c for c in gh.calls if c[0] == "POST"]
            self.assertTrue(any(p[1].endswith("/git/blobs") for p in posts))
            self.assertTrue(any(p[1].endswith("/git/trees") for p in posts))
            self.assertTrue(any(p[1].endswith("/git/commits") for p in posts))
            self.assertTrue(any(p[1].endswith("/git/refs") for p in posts))

    def test_push_recreates_every_commit_of_a_multi_commit_branch_in_order(self):
        import os
        import subprocess
        import tempfile
        from bus import lane
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp, extra_commit=True)

            def rev(spec):
                return subprocess.run(
                    ["git", "rev-parse", spec], capture_output=True,
                    text=True, check=True, cwd=repo).stdout.strip()
            base = rev("main")
            local_trees = [rev("HEAD~1^{tree}"), rev("HEAD^{tree}")]
            made = ["a" * 40, "b" * 40]
            gh = FakeGitDatabase(trees=local_trees, commits=list(made))
            gh_issue = FakeGitHub()

            class Combined:
                def rest(self, method, path, body=None, **kwargs):
                    if "/git/" in path:
                        return gh.rest(method, path, body, **kwargs)
                    return gh_issue.rest(method, path, body, **kwargs)
            old = os.getcwd()
            os.chdir(repo)
            try:
                sha = lane.push_branch(Combined(), "example/project", 4)
            finally:
                os.chdir(old)
            commit_posts = [b for m, p, b in gh.bodies
                            if m == "POST" and p.endswith("/git/commits")]
            self.assertEqual(len(commit_posts), 2)
            # Oldest first, each tree matching the local commit's tree, and
            # each commit chained onto the one the API made before it.
            self.assertEqual([c["tree"] for c in commit_posts], local_trees)
            self.assertEqual([c["message"] for c in commit_posts],
                             ["change", "second change"])
            self.assertEqual(commit_posts[0]["parents"], [base])
            self.assertEqual(commit_posts[1]["parents"], [made[0]])
            self.assertEqual(sha, made[1])
            self.assertEqual(gh.refs["loom/4-add-a-feature"], made[1])

    def test_push_is_a_no_op_when_remote_is_up_to_date(self):
        import os
        import tempfile
        from bus import lane
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            gh = FakeGitDatabase()
            gh.refs["loom/4-add-a-feature"] = "d" * 40
            # Local HEAD differs from remote, so it is not up to date; use
            # the up-to-date path by pointing the ref at the local HEAD.
            import subprocess
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True,
                text=True, check=True, cwd=repo).stdout.strip()
            gh.refs["loom/4-add-a-feature"] = head
            gh_issue = FakeGitHub()
            class Combined:
                def rest(self, method, path, body=None, **kwargs):
                    if "/git/" in path:
                        return gh.rest(method, path, body, **kwargs)
                    return gh_issue.rest(method, path, body, **kwargs)
            old = os.getcwd()
            os.chdir(repo)
            try:
                sha = lane.push_branch(Combined(), "example/project", 4)
            finally:
                os.chdir(old)
            self.assertEqual(sha, head)
            posts = [c for c in gh.calls if c[0] == "POST"]
            self.assertEqual(posts, [])

    def test_push_refuses_when_current_branch_is_not_the_ticket_branch(self):
        import os
        import subprocess
        import tempfile
        from bus import lane
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            subprocess.run(["git", "checkout", "main"], capture_output=True,
                           check=True, cwd=repo)
            gh_issue = FakeGitHub()
            old = os.getcwd()
            os.chdir(repo)
            try:
                with self.assertRaises(ValueError):
                    lane.push_branch(gh_issue, "example/project", 4)
            finally:
                os.chdir(old)

    def _push_one_commit(self, change, remote_head=None, refs_post_status=None):
        """Make the branch's one commit with `change(git, work)` instead of
        the default edit, then push it. Returns (sha, the fake, the local
        commit SHA)."""
        import os
        import subprocess
        import tempfile
        from bus import lane
        from bus.github import GitHubError
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        with tempfile.TemporaryDirectory() as tmp:
            work = _make_repo(tmp)

            def git(*args):
                return subprocess.run(["git", *args], capture_output=True,
                                      text=True, check=True, cwd=work,
                                      env=env).stdout.strip()
            git("reset", "--hard", "main")
            change(git, work)
            git("commit", "-m", "the change")
            gh = FakeGitDatabase(tree_sha=git("rev-parse", "HEAD^{tree}"))
            if remote_head:
                # The remote branch already holds main's commit.
                gh.refs["loom/4-add-a-feature"] = git("rev-parse", "main")
            if refs_post_status:
                inner = gh.rest

                def refusing(method, path, body=None, **kwargs):
                    if method == "POST" and path.endswith("/git/refs"):
                        gh.calls.append((method, path))
                        raise GitHubError("POST %s -> %d" % (path, refs_post_status),
                                          refs_post_status, "")
                    return inner(method, path, body, **kwargs)
                gh.rest = refusing
            gh_issue = FakeGitHub()

            class Combined:
                def rest(self, method, path, body=None, **kwargs):
                    if "/git/" in path:
                        return gh.rest(method, path, body, **kwargs)
                    return gh_issue.rest(method, path, body, **kwargs)
            old = os.getcwd()
            os.chdir(work)
            try:
                sha = lane.push_branch(Combined(), "example/project", 4)
            finally:
                os.chdir(old)
            return sha, gh, git("rev-parse", "HEAD")

    @staticmethod
    def _tree_entries(gh):
        trees = [b for m, p, b in gh.bodies if m == "POST" and p.endswith("/git/trees")]
        return {entry["path"]: entry for entry in trees[0]["tree"]}

    def test_a_renamed_file_is_pushed_as_a_delete_and_a_new_blob(self):
        # git diff detects renames by default and prints R100<TAB>old<TAB>new;
        # read naively, the path was "old<TAB>new" and rev-parse failed.
        sha, gh, _ = self._push_one_commit(
            lambda git, work: git("mv", "file.txt", "moved.txt"))
        self.assertEqual(sha, gh.commit_sha)
        entries = self._tree_entries(gh)
        self.assertEqual(sorted(entries), ["file.txt", "moved.txt"])
        self.assertIsNone(entries["file.txt"]["sha"])
        self.assertEqual(entries["moved.txt"]["mode"], "100644")
        self.assertIsNotNone(entries["moved.txt"]["sha"])

    def test_a_file_with_a_non_ascii_name_is_pushed(self):
        # core.quotePath writes this name as "caf\303\251.txt" in a
        # line-based diff; the NUL-separated read gets the real path.
        name = "caf\u00e9.txt"

        def add(git, work):
            import os
            with open(os.path.join(work, name), "w") as f:
                f.write("new\n")
            git("add", name)
        sha, gh, _ = self._push_one_commit(add)
        self.assertEqual(sha, gh.commit_sha)
        self.assertIn(name, self._tree_entries(gh))

    def test_an_existing_remote_branch_is_moved_with_a_non_forced_patch(self):
        def edit(git, work):
            import os
            with open(os.path.join(work, "file.txt"), "w") as f:
                f.write("again\n")
            git("add", "file.txt")
        sha, gh, _ = self._push_one_commit(edit, remote_head=True)
        self.assertEqual(sha, gh.commit_sha)
        self.assertNotIn(("POST", "/repos/example/project/git/refs"), gh.calls)
        patches = [b for m, p, b in gh.bodies if m == "PATCH"]
        self.assertEqual(patches, [{"sha": gh.commit_sha, "force": False}])
        self.assertEqual(gh.refs["loom/4-add-a-feature"], gh.commit_sha)

    def test_a_refs_post_that_answers_422_falls_back_to_a_patch(self):
        # The branch appeared between the read and the create (another push
        # won the race): the ref is moved instead, still without force.
        def edit(git, work):
            import os
            with open(os.path.join(work, "file.txt"), "w") as f:
                f.write("again\n")
            git("add", "file.txt")
        sha, gh, _ = self._push_one_commit(edit, refs_post_status=422)
        self.assertEqual(sha, gh.commit_sha)
        self.assertIn(("POST", "/repos/example/project/git/refs"), gh.calls)
        patches = [b for m, p, b in gh.bodies if m == "PATCH"]
        self.assertEqual(patches, [{"sha": gh.commit_sha, "force": False}])

    def test_a_refs_post_that_fails_otherwise_is_raised(self):
        from bus.github import GitHubError
        def edit(git, work):
            import os
            with open(os.path.join(work, "file.txt"), "w") as f:
                f.write("again\n")
            git("add", "file.txt")
        with self.assertRaises(GitHubError):
            self._push_one_commit(edit, refs_post_status=500)


if __name__ == "__main__":
    unittest.main()
