"""Draft pull requests cannot be advertised as ready to merge."""
import unittest

from bus import brain, merge_verdict
from test_bus_brain import ready_node


class DraftReadyParity(unittest.TestCase):
    def test_draft_pull_request_is_blocked_for_the_lane(self):
        node = ready_node()
        node["isDraft"] = True
        verdict = merge_verdict.ready_verdict(node, "example-review-bot")
        self.assertFalse(verdict.ready)
        self.assertEqual(verdict.text, "blocked: draft")
        self.assertEqual(verdict.actors, (merge_verdict.LANE,))

    def test_ready_fields_requests_draft_state(self):
        self.assertRegex(brain.READY_FIELDS, r"\bisDraft\b")
