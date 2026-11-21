"""A ticket the model ladder escalated waits for its rung: the pin fallback
never moves it onto a free lane of its tier (loom#3191)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import factory  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
import test_bus_factory  # noqa: E402


class PinFallbackLadderTest(test_bus_factory.PinFallback):
    def test_a_rung_comment_keeps_the_ticket_waiting_for_its_model(self):
        self.queued(7)
        self.gh.comments[7] = [{"body": "<!-- loom:rung 2 -->\nEscalated to rung 2.",
                              "user": self.BOT}]
        self.assign(31 * 60, ["spark"])
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")
        self.assertNotIn("loom:pin-fallback", self.notes(7))

    def test_an_escalate_marker_in_the_status_body_does_too(self):
        self.queued(7, body=self.SONNET + "\n<!-- loom:escalate v1 -->\nrung 2\n")
        self.assign(31 * 60, ["spark"])
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")
        self.assertNotIn("loom:pin-fallback", self.notes(7))

    def test_a_brain_written_pin_without_a_ladder_marker_still_falls_back(self):
        self.queued(7)
        self.assign(31 * 60, ["spark"])
        self.assertEqual(factory._state(self.gh.issues[7]), "briefed")


for _name in dir(test_bus_factory.PinFallback):
    if _name.startswith("test_"):
        setattr(PinFallbackLadderTest, _name, None)
