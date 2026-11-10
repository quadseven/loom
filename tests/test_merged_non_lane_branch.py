"""A merged pull request from a non-lane branch with a Refs line moves its
ticket to loom:merged when the author is trusted. Untrusted authors do not
move tickets (loom#2938)."""

import io
import os
import contextlib
import unittest
from unittest import mock

from bus import station, tickets
from bus.github import GitHub
from tests.test_bus_station import (AUTHORS, BODY_WITH_CRITERIA, REPO,
                                     Base, FakeGitHub, labeled)


def make_pr(number, author, branch, body, state="closed", merged_at=None):
    pr = {"number": number, "state": state, "user": {"login": author},
          "body": body, "head": {"ref": branch, "repo": {"full_name": REPO}}}
    if merged_at:
        pr["merged_at"] = merged_at
    return pr


PR_BODY_WITH_TICKED = ("## Why\nx\n\n"
                       "## Acceptance criteria\n\n"
                       "- [x] A test shows the thing is fixed.\n\n"
                       "Refs %s#%d\n")


class MergedNonLaneBranchTest(Base):
    def setUp(self):
        super().setUp()
        self.settings.factory_on = True

    def run_reconcile(self):
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        st.reconcile_pull_requests()

    def test_trusted_muse_branch_moves_queued_to_merged(self):
        """Trusted author on muse/ branch with Refs line moves ticket from
        loom:queued to loom:merged."""
        self.fake.add(123, state="queued", body=BODY_WITH_CRITERIA)
        self.fake.pulls = [make_pr(
            99, AUTHORS[0], "muse/feature-X",
            PR_BODY_WITH_TICKED % (REPO, 123),
            merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:merged", self.fake.names(123))
        self.assertNotIn("loom:queued", self.fake.names(123))
        self.assertNotIn("lane:", " ".join(self.fake.names(123)))
        self.assertNotIn("needs:human", " ".join(self.fake.names(123)))

    def test_trusted_fix_branch_moves_running_to_merged(self):
        """Trusted author on fix/ branch with Refs line moves ticket from
        loom:running to loom:merged."""
        # A running ticket has loom:pr label and a lane PR
        self.fake.add(456, state="running", lane="codex", body=BODY_WITH_CRITERIA)
        self.fake.issues[456]["labels"].append({"name": "loom:pr"})
        self.fake.pulls = [
            # The lane PR (open)
            make_pr(98, AUTHORS[0], "loom/456-fix", "Refs %s#456\n" % REPO, state="open"),
            # The non-lane PR that merged
            make_pr(99, AUTHORS[0], "fix/crash-123",
                    PR_BODY_WITH_TICKED % (REPO, 456),
                    merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:merged", self.fake.names(456))
        self.assertNotIn("loom:running", self.fake.names(456))
        self.assertNotIn("lane:", " ".join(self.fake.names(456)))
        self.assertNotIn("needs:human", " ".join(self.fake.names(456)))

    def test_untrusted_branch_does_not_move_pr_ticket(self):
        """Untrusted author on quadseven/ branch with Refs line does NOT move
        ticket from loom:pr."""
        self.fake.add(789, state="pr", lane="codex", body=BODY_WITH_CRITERIA)
        self.fake.pulls = [make_pr(
            99, "stranger", "quadseven/refactor",
            PR_BODY_WITH_TICKED % (REPO, 789),
            merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:pr", self.fake.names(789))
        self.assertNotIn("loom:merged", self.fake.names(789))
        self.assertIn("lane:codex", self.fake.names(789))

    def test_trusted_branch_moves_briefed_to_merged(self):
        """Trusted author on non-lane branch moves ticket from loom:briefed."""
        self.fake.add(234, state="briefed", lane="codex", body=BODY_WITH_CRITERIA)
        self.fake.pulls = [make_pr(
            99, AUTHORS[0], "muse/feature-Y",
            PR_BODY_WITH_TICKED % (REPO, 234),
            merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:merged", self.fake.names(234))
        self.assertNotIn("loom:briefed", self.fake.names(234))
        self.assertNotIn("lane:", " ".join(self.fake.names(234)))

    def test_trusted_branch_moves_answered_to_merged(self):
        """Trusted author on non-lane branch moves ticket from loom:answered."""
        # answered ticket has loom:pr label
        self.fake.add(345, state="answered", lane="codex", body=BODY_WITH_CRITERIA)
        self.fake.issues[345]["labels"].append({"name": "loom:pr"})
        self.fake.pulls = [
            make_pr(98, AUTHORS[0], "loom/345-fix", "Refs %s#345\n" % REPO, state="open"),
            make_pr(99, AUTHORS[0], "fix/hotfix-1",
                    PR_BODY_WITH_TICKED % (REPO, 345),
                    merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:merged", self.fake.names(345))
        self.assertNotIn("loom:answered", self.fake.names(345))
        self.assertNotIn("lane:", " ".join(self.fake.names(345)))

    def test_trusted_branch_moves_failed_to_merged(self):
        """Trusted author on non-lane branch moves ticket from loom:failed."""
        # failed ticket has loom:pr label
        self.fake.add(456, state="failed", lane="codex", body=BODY_WITH_CRITERIA)
        self.fake.issues[456]["labels"].append({"name": "loom:pr"})
        self.fake.pulls = [
            make_pr(98, AUTHORS[0], "loom/456-retry", "Refs %s#456\n" % REPO, state="open"),
            make_pr(99, AUTHORS[0], "muse/retry-1",
                    PR_BODY_WITH_TICKED % (REPO, 456),
                    merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:merged", self.fake.names(456))
        self.assertNotIn("loom:failed", self.fake.names(456))
        self.assertNotIn("lane:", " ".join(self.fake.names(456)))

    def test_untrusted_does_not_move_queued(self):
        """Untrusted author does not move ticket from loom:queued."""
        self.fake.add(567, state="queued", body=BODY_WITH_CRITERIA)
        self.fake.pulls = [make_pr(
            99, "stranger", "muse/feature-Z",
            PR_BODY_WITH_TICKED % (REPO, 567),
            merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:queued", self.fake.names(567))
        self.assertNotIn("loom:merged", self.fake.names(567))

    def test_untrusted_does_not_move_briefed(self):
        """Untrusted author does not move ticket from loom:briefed."""
        self.fake.add(678, state="briefed", lane="codex", body=BODY_WITH_CRITERIA)
        self.fake.pulls = [make_pr(
            99, "stranger", "fix/other-thing",
            "## Why\nx\n\nRefs #678\n",
            merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:briefed", self.fake.names(678))
        self.assertNotIn("loom:merged", self.fake.names(678))

    def test_untrusted_does_not_move_running(self):
        """Untrusted author does not move ticket from loom:running."""
        self.fake.add(789, state="running", lane="codex", body=BODY_WITH_CRITERIA)
        self.fake.issues[789]["labels"].append({"name": "loom:pr"})
        self.fake.pulls = [
            make_pr(98, AUTHORS[0], "loom/789-fix", "Refs %s#789\n" % REPO, state="open"),
            make_pr(99, "stranger", "quadseven/refactor-2",
                    "## Why\nx\n\nRefs %s#789\n" % REPO,
                    merged_at="2030-01-02T00:00:00Z")]
        self.run_reconcile()
        self.assertIn("loom:running", self.fake.names(789))
        self.assertNotIn("loom:merged", self.fake.names(789))


if __name__ == "__main__":
    unittest.main()