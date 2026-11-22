"""A queued ticket that an open same-repo pull request names with a bare
Refs line gets no lane when the pull request's author is a brief author or a
trusted briefer (the pull request already carries the work)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bus import factory  # noqa: E402
import test_bus_factory as base  # noqa: E402

REPO = base.REPO
BRIEFER = {"login": "Fleet-Brain[bot]", "type": "Bot"}
STRANGER = {"login": "someone-else", "type": "User"}


class BrieferPullRequestTest(base.Base):
    def setUp(self):
        super().setUp()
        self.gh.ticket(7, "queued", body="Difficulty: medium\n\n## Why\nx\n" + base.ACCEPTANCE)
        self.gh.issues[7]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"}]
        self.gh.issues[7]["created_at"] = base.iso(0)

    def assign(self, user):
        self.gh.pull(50, 7, user=user, body="Why\n\nRefs #7\n", branch="work/7-x")
        worker = factory.Factory(self.gh, [REPO], base.NOW, trusted=["alice-example"],
                                 briefers=["fleet-brain[bot]"], available=["codex"])
        worker.assign_queued()
        return worker

    def test_a_trusted_briefer_pull_request_holds_the_ticket_and_names_itself(self):
        worker = self.assign(BRIEFER)
        issue = self.gh.issues[7]
        self.assertEqual((factory._state(issue), self.lane(7)), ("queued", ""))
        self.assertIn("#50", worker.waits["%s#7" % REPO])

    def test_a_brief_author_pull_request_moves_the_ticket_to_pr(self):
        self.assign({"login": "Alice-Example", "type": "User"})
        self.assertEqual(factory._state(self.gh.issues[7]), "pr")

    def test_a_pull_request_from_any_other_login_holds_nothing(self):
        worker = self.assign(STRANGER)
        self.assertEqual(factory._state(self.gh.issues[7]), "briefed")
        self.assertNotIn("%s#7" % REPO, worker.waits)


if __name__ == "__main__":
    unittest.main()
