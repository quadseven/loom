"""A ticket whose fix merged from a branch outside loom/ moves to merged.

Ticket 2622 sat at loom:queued on lane codex for 3 days although pull
request 2680 (branch muse/2622-..., body Refs quadseven/loom#2622) had built
it and merged; tickets 2357 and 2358 had the same fault the same day. The
station delivered queued and briefed tickets but kept their lane label, and
never delivered a running, answered or failed one (loom#2938).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import station  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_bus_station import REPO, Base  # noqa: E402


class MergedNonLaneBranchTest(Base):
    def merged(self, number=5, author="alice-example", branch="muse/5-x", refs=None):
        refs = refs if refs is not None else "%s#%d" % (REPO, number)
        return {"number": 99, "state": "closed", "merged_at": "2030-01-02T00:00:00Z",
                "merge_commit_sha": "c" * 40, "user": {"login": author},
                "body": "Why.\n\nRefs %s\n" % refs,
                "head": {"ref": branch, "repo": {"full_name": REPO}}}

    def reconcile(self):
        station.Station(GitHub("token", transport=self.fake),
                        self.settings).reconcile_pull_requests()

    def test_a_trusted_non_lane_merge_moves_a_queued_ticket_to_merged_without_its_lane(self):
        self.fake.add(5, state="queued")
        self.fake.pulls = [self.merged()]
        self.reconcile()
        names = self.fake.names(5)
        self.assertIn("loom:merged", names)
        self.assertNotIn("loom:queued", names)
        self.assertFalse(any(n.startswith("lane:") for n in names), names)
        note = self.fake.comments[5][-1]["body"]
        self.assertIn("Pull request #99 merged while this ticket was at loom:queued", note)
        self.assertIn("branch muse/5-x, outside loom/ (merge commit %s)" % ("c" * 12), note)

    def test_an_untrusted_login_moves_nothing(self):
        self.fake.add(5, state="queued")
        self.fake.pulls = [self.merged(author="stranger")]
        self.reconcile()
        self.assertIn("loom:queued", self.fake.names(5))
        self.assertNotIn("loom:merged", self.fake.names(5))

    def test_a_refs_line_naming_another_repo_moves_no_ticket_here(self):
        self.fake.add(5, state="queued")
        self.fake.pulls = [self.merged(refs="other-owner/other-repo#5")]  # leak-guard-allow
        self.reconcile()
        self.assertIn("loom:queued", self.fake.names(5))
        self.assertNotIn("loom:merged", self.fake.names(5))

    def test_a_running_answered_or_failed_ticket_moves_too(self):
        for state in ("running", "answered", "failed"):
            with self.subTest(state=state):
                self.fake.add(5, state=state)
                self.fake.pulls = [self.merged()]
                self.reconcile()
                names = self.fake.names(5)
                self.assertIn("loom:merged", names)
                self.assertNotIn("loom:" + state, names)
                self.assertFalse(any(n.startswith("lane:") for n in names), names)

    def test_a_lane_branch_merge_leaves_a_running_ticket_to_its_run(self):
        # A loom/ pull request belongs to a lane run, which reports its own
        # outcome; only a branch outside loom/ delivers a running ticket here.
        self.fake.add(5, state="running")
        self.fake.pulls = [self.merged(branch="loom/5-fix-the-thing")]
        self.reconcile()
        self.assertIn("loom:running", self.fake.names(5))
        self.assertNotIn("loom:merged", self.fake.names(5))


if __name__ == "__main__":
    unittest.main()
