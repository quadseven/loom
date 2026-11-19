"""bus/station.py _probe_lane: a base-check probe needs no strong model, so
the Anthropic-credit lanes (claude and the anthropic slots) are picked only
when no other eligible local lane is free; spending the scarce top-rung lane
claude on a base check delays the fix rounds only it can serve (loom#3207).
"""

import unittest
from unittest import mock

from bus import station


def probe_station():
    """A Station with every inbox empty and every repo public, so only the
    lane order decides which free lane _probe_lane returns."""
    st = station.Station.__new__(station.Station)
    st._inbox_empty = lambda lane: True
    st._public = lambda repo: True
    return st


class ProbeLaneOrderTest(unittest.TestCase):
    def setUp(self):
        # No cluster runner slots in the test environment, whatever the
        # host's LOOM_RUNNER_SLOTS says: the runner-slot filter is not the
        # order under test.
        self._slots = mock.patch.object(station.runners, "runner_slots",
                                        return_value=[])
        self._slots.start()
        self.addCleanup(self._slots.stop)
        self.st = probe_station()

    def test_spark_before_claude(self):
        self.assertEqual(
            self.st._probe_lane(["claude", "spark"], "example-owner/loom"),
            "spark")

    def test_codex_slot_before_anthropic_slot(self):
        self.assertEqual(
            self.st._probe_lane(["anthropic-2", "codex-3"], "example-owner/loom"),
            "codex-3")

    def test_free_lane_before_claude_and_anthropic_for_public_repo(self):
        self.assertEqual(
            self.st._probe_lane(["claude", "anthropic", "free-ollama"],
                                "example-owner/loom"),
            "free-ollama")

    def test_anthropic_credit_lane_still_picked_when_it_is_the_only_kind_free(self):
        self.assertEqual(
            self.st._probe_lane(["claude", "anthropic-2"], "example-owner/loom"),
            "claude")
        self.assertEqual(
            self.st._probe_lane(["anthropic-2"], "example-owner/loom"),
            "anthropic-2")


if __name__ == "__main__":
    unittest.main()
