"""Tests for ticket move label rules (bus/ticket_move.py)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import ticket_move  # noqa: E402


class TicketMoveNextLabelsTests(unittest.TestCase):
    """Tests for bus.ticket_move.next_labels."""

    def test_move_out_of_running_drops_own_claim_and_keeps_another_logins(self):
        have = [
            "loom:ticket",
            "loom:running",
            "lane:codex",
            "claim:alice-example",
            "claim:other-op",
        ]
        result = ticket_move.next_labels(
            have, "queued", own_logins=("alice-example",)
        )
        self.assertNotIn("claim:alice-example", result)
        self.assertIn("claim:other-op", result)
        self.assertNotIn("loom:running", result)
        self.assertIn("loom:queued", result)
        self.assertIn("lane:codex", result)

    def test_move_not_out_of_running_keeps_own_claim(self):
        have = ["loom:ticket", "loom:queued", "claim:alice-example"]
        result = ticket_move.next_labels(
            have, "briefed", lane="codex", own_logins=("alice-example",)
        )
        self.assertIn("claim:alice-example", result)
        self.assertIn("loom:briefed", result)
        self.assertIn("lane:codex", result)

    def test_move_within_running_keeps_own_claim(self):
        have = ["loom:ticket", "loom:running", "claim:alice-example"]
        result = ticket_move.next_labels(
            have, "running", own_logins=("alice-example",)
        )
        self.assertIn("claim:alice-example", result)
        self.assertIn("loom:running", result)

    def test_move_clears_wait_labels_and_adds_new_wait_label(self):
        have = ["loom:ticket", "loom:running", "wait:gate", "wait:incident"]
        result = ticket_move.next_labels(have, "queued", wait="model-limit")
        self.assertNotIn("wait:gate", result)
        self.assertNotIn("wait:incident", result)
        self.assertIn("wait:model-limit", result)

    def test_move_with_wait_already_prefixed(self):
        have = ["loom:ticket", "loom:running", "wait:gate"]
        result = ticket_move.next_labels(have, "queued", wait="wait:model-limit")
        self.assertNotIn("wait:gate", result)
        self.assertIn("wait:model-limit", result)
        self.assertNotIn("wait:wait:model-limit", result)

    def test_move_clears_needs_brain(self):
        have = ["loom:ticket", "loom:pr", "loom:ready", "needs-brain", "wait:gate"]
        result = ticket_move.next_labels(have, "queued")
        self.assertNotIn("needs-brain", result)
        self.assertNotIn("wait:gate", result)
        self.assertNotIn("loom:pr", result)
        self.assertNotIn("loom:ready", result)
        self.assertIn("loom:queued", result)

    def test_lane_change_removes_old_lane_and_adds_new_lane(self):
        have = ["loom:ticket", "loom:briefed", "lane:codex"]
        result = ticket_move.next_labels(have, "briefed", lane="claude")
        self.assertNotIn("lane:codex", result)
        self.assertIn("lane:claude", result)

    def test_lane_change_with_lane_prefixed(self):
        have = ["loom:ticket", "loom:briefed", "lane:codex"]
        result = ticket_move.next_labels(have, "briefed", lane="lane:claude")
        self.assertNotIn("lane:codex", result)
        self.assertIn("lane:claude", result)
        self.assertNotIn("lane:lane:claude", result)

    def test_lane_none_preserves_existing_lane(self):
        have = ["loom:ticket", "loom:running", "lane:codex"]
        result = ticket_move.next_labels(have, "queued", lane=None)
        self.assertIn("lane:codex", result)

    def test_move_clears_every_state_label(self):
        have = [
            "loom:ticket",
            "loom:merged",
            "loom:ready",
            "loom:closed-unmerged",
            "lane:claude",
        ]
        result = ticket_move.next_labels(have, "shipped")
        self.assertEqual(
            result, ["loom:ticket", "lane:claude", "loom:shipped"]
        )

    def test_state_accepts_loom_prefix_or_bare_name(self):
        have = ["loom:ticket", "loom:queued"]
        from_bare = ticket_move.next_labels(have, "briefed", lane="codex")
        from_prefixed = ticket_move.next_labels(have, "loom:briefed", lane="codex")
        self.assertEqual(from_bare, ["loom:ticket", "loom:briefed", "lane:codex"])
        self.assertEqual(from_prefixed, ["loom:ticket", "loom:briefed", "lane:codex"])

    def test_strip_prefix_leaves_text_without_the_prefix_unchanged(self):
        # A name that does not start with the prefix comes back as it was,
        # including one that holds the prefix word later in the string.
        for name in ("codex", "claude-2", "my-lane:x", "x-wait:gate", "", "lane"):
            self.assertEqual(name, ticket_move._strip_prefix(name, "lane:"), name)
            self.assertEqual(name, ticket_move._strip_prefix(name, "wait:"), name)
        self.assertEqual("codex", ticket_move._strip_prefix("lane:codex", "lane:"))
        # Only one leading prefix goes.
        self.assertEqual("lane:codex", ticket_move._strip_prefix("lane:lane:codex", "lane:"))

    def test_a_lane_or_wait_holding_the_prefix_word_mid_name_is_kept_whole(self):
        have = ["loom:ticket", "loom:running"]
        result = ticket_move.next_labels(have, "queued", lane="my-lane:x",
                                         wait="x-wait:gate")
        self.assertIn("lane:my-lane:x", result)
        self.assertIn("wait:x-wait:gate", result)

    def test_needs_brain_is_kept_when_the_caller_does_not_clear_it(self):
        have = ["loom:ticket", "loom:running", "needs-brain", "wait:gate"]
        result = ticket_move.next_labels(have, "failed", clear_needs_brain=False)
        self.assertEqual(["loom:ticket", "needs-brain", "loom:failed"], result)


if __name__ == "__main__":
    unittest.main()
