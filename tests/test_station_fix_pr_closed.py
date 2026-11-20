"""A fix brief parked because its pull request closed unmerged is a stop."""
import os

from bus import station
from bus.github import GitHub
from tests.test_bus_station import AUTHORS, BODY_WITH_CRITERIA, REPO, Base

FIX = ("<!-- loom:fix v1 -->\nPull request: %s#7, branch loom/12-fix-the-thing\n\n"
       "The lint check fails.\n\n### Check: lint\n\n    E501\n" % REPO)
NAME = "example-owner.example-repo-12-r2"


class FixPullRequestClosedTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.default_body = BODY_WITH_CRITERIA
        self.fake.add(12, state="running")
        self.fake.issues[12]["labels"].append({"name": "lane:codex"})
        for n, body in enumerate((
                "Lane codex opened a pull request: https://example.com/pull/7", FIX,
                "Claimed for lane codex (body sha256 0123456789ab)"), 1):
            self.fake.comments[12].append({
                "id": n, "body": body, "user": {"login": AUTHORS[0]},
                "created_at": "2030-01-02T00:00:00Z",
                "updated_at": "2030-01-02T00:00:00Z"})
        gh = GitHub("token", transport=self.fake)
        self.runner = station.Station(gh, self.settings)

    def report(self, fix_pr, gone_pr):
        self.write(os.path.join(self.home, "logs", "codex", NAME + ".log"),
                   "[2030-01-02T00:01:00Z] %s failed: [work] pull request #%s is no "
                   "longer open on loom/x (CLOSED loom/x)\n" % (NAME, gone_pr))
        return self.runner._report_failure(
            REPO, 12, "codex", "Fix-PR: %s\n" % fix_pr, NAME)

    def test_a_closed_fix_pull_request_requeues_the_ticket_as_a_stop(self):
        self.assertTrue(self.report("7", "7"))
        names = self.fake.names(12)
        self.assertIn("loom:queued", names)
        self.assertNotIn("loom:failed", names)
        self.assertNotIn("loom:running", names)
        status = " ".join(str(c["body"]) for c in self.fake.comments[12])
        self.assertNotIn("failed:", status.split("Lane codex stopped")[-1])
        self.assertIn(
            "stopped: [transient] pull request #7 was closed unmerged", status)
        # The stale fix is retired: the next brief is a build brief.
        comments = self.runner._paginate("/repos/%s/issues/12/comments" % REPO)
        self.assertIsNone(self.runner._fix_for(REPO, comments))

    def test_a_closed_pull_request_that_is_not_the_briefs_fix_is_a_failure(self):
        self.report("7", "8")
        self.assertNotIn("loom:queued", self.fake.names(12))
