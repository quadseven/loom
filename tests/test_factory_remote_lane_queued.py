"""A factory move onto a remote pull lane leaves the ticket queued, where that
lane claims it; a move onto a local lane leaves it briefed."""

import unittest

from shadow_env import setUpModule, tearDownModule  # noqa: F401

from bus import brain, factory
from test_bus_factory import NOW, REPO, FakeGitHub


class RemoteLaneQueued(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.moved = []

        def move(gh, repo, number, state, lane=None, note=None, model=None):
            self.moved.append((number, state, lane))
            gh.issues[number]["labels"] = [
                {"name": x} for x in brain._moved_labels(
                    [y["name"] for y in gh.issues[number]["labels"]], state, lane)]

        self.gh.ticket(6, "failed", lane="zai")
        self.f = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"], move=move)

    def names(self):
        return {x["name"] for x in self.gh.issues[6]["labels"]}

    def test_a_move_to_lane_muse_leaves_the_ticket_queued(self):
        self.f.brief_on(REPO, 6, "muse")
        self.assertEqual(self.moved, [(6, "queued", "muse")])
        self.assertIn("loom:queued", self.names())
        self.assertIn("lane:muse", self.names())
        self.assertNotIn("loom:briefed", self.names())

    def test_a_move_to_a_local_lane_leaves_the_ticket_briefed(self):
        self.f.brief_on(REPO, 6, "claude")
        self.assertEqual(self.moved, [(6, "briefed", "claude")])
        self.assertIn("loom:briefed", self.names())
        self.assertIn("lane:claude", self.names())

    def test_lane_state_names_the_state_a_lane_claims(self):
        self.assertEqual(factory.lane_state("muse"), "queued")
        self.assertEqual(factory.lane_state("claude"), "briefed")


if __name__ == "__main__":
    unittest.main()
