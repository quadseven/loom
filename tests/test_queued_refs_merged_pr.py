"""A queued ticket whose same-repo pull request already merged goes to
loom:merged instead of taking a lane."""
import unittest

from shadow_env import setUpModule, tearDownModule  # noqa: F401

from bus import factory
from test_bus_factory import ACCEPTANCE, NOW, REPO, Base


class MergedRefsPr(Base):
    def queued(self, number):
        self.gh.ticket(number, "queued", body="Difficulty: medium\n\n## Why\nx\n" + ACCEPTANCE)
        self.gh.issues[number]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"},
                                            {"name": "lane:codex"}, {"name": "wait:blocked"}]

    def closed_pull(self, number, body, merged, user=None):
        self.gh.pull(number, 7, state="closed", body=body, user=user or {"login": "alice-example"},
                     merged_at="2020-01-01T00:00:00Z" if merged else None)

    def assign(self, **kw):
        kw.setdefault("trusted", ["alice-example"])
        worker = factory.Factory(self.gh, [REPO], NOW, **kw)
        worker.assign_queued()
        return worker

    def labels(self, number):
        return factory._labels(self.gh.issues[number])

    def test_a_merged_trusted_pull_request_moves_the_ticket_to_merged(self):
        self.queued(7)
        self.closed_pull(20, "Refs #7\n", merged=True)
        self.assign()
        have = self.labels(7)
        self.assertIn("loom:merged", have)
        self.assertNotIn("loom:queued", have)
        self.assertFalse([x for x in have if x.startswith("lane:") or x == "wait:blocked"])

    def test_a_merged_briefer_pull_request_moves_the_ticket_to_merged(self):
        self.queued(7)
        self.closed_pull(20, "Refs #7\n", merged=True, user={"login": "briefer-example"})
        self.assign(briefers=["briefer-example"])
        have = self.labels(7)
        self.assertIn("loom:merged", have)
        self.assertNotIn("loom:queued", have)

    def test_a_merged_pull_request_from_a_login_in_neither_list_leaves_the_ticket_free(self):
        self.queued(7)
        self.closed_pull(20, "Refs #7\n", merged=True, user={"login": "someone-else"})
        self.assign(briefers=["briefer-example"])
        have = self.labels(7)
        self.assertNotIn("loom:merged", have)
        self.assertTrue([x for x in have if x.startswith("lane:")], have)

    def test_an_unmerged_untrusted_or_other_ticket_pull_request_changes_nothing(self):
        for number, body, merged, user in (
                (20, "Refs #7\n", False, None),
                (21, "Refs #7\n", True, {"login": "someone-else"}),
                (22, "Refs #8\n", True, None),
                (23, "Mentions #7 in passing\n", True, None)):
            self.closed_pull(number, body, merged, user)
        self.queued(7)
        self.assign()
        self.assertNotIn("loom:merged", self.labels(7))


if __name__ == "__main__":
    unittest.main()
