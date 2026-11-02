"""Brief rendering: what a lane is told to do."""

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dispatch"))

import dispatcher  # noqa: E402

TICKET = """# T042: Fix the thing

Repo: example/repo
Difficulty: medium

## Why
The thing is broken.
"""


class RenderBriefTest(unittest.TestCase):
    def render(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ticket = root / "T042-fix-the-thing.md"
            ticket.write_text(TICKET)
            with mock.patch.object(dispatcher, "BRIEFS", root / "briefs"):
                return dispatcher.render_brief(ticket, "codex").read_text()

    def test_brief_forbids_the_lane_from_merging(self):
        brief = self.render().lower()
        self.assertIn("never merge", brief)
        self.assertNotIn("merged only when", brief)

    def test_brief_carries_repo_and_loom_branch(self):
        brief = self.render()
        self.assertIn("Repo: example/repo\n", brief)
        self.assertIn("Branch: loom/T042-fix-the-thing\n", brief)


class PickLaneTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        (self.state / "usage").mkdir()
        self._patch = mock.patch.object(dispatcher, "STATE", self.state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def beat(self, lane, available=True, age=0):
        (self.state / "usage" / f"{lane}.json").write_text(json.dumps(
            {"lane": lane, "at": int(time.time()) - age, "available": available}))

    def test_a_lane_with_no_heartbeat_gets_no_ticket(self):
        self.assertIsNone(dispatcher.pick_lane("easy", {}))

    def test_a_fresh_available_lane_is_picked(self):
        self.beat("codex")
        self.assertEqual(dispatcher.pick_lane("medium", {}), "codex")

    def test_an_unavailable_or_stale_lane_is_skipped(self):
        self.beat("anthropic", available=False)
        self.beat("claude", age=3600)
        self.beat("codex")
        self.assertEqual(dispatcher.pick_lane("medium", {}), "codex")

    def test_a_corrupt_heartbeat_is_skipped_and_logged(self):
        (self.state / "usage" / "anthropic.json").write_text('{"lane": "anthr')
        self.beat("claude")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(dispatcher.pick_lane("medium", {}), "claude")
        self.assertIn("anthropic", out.getvalue())
        self.assertIn("unreadable heartbeat", out.getvalue())

    def test_a_missing_heartbeat_is_skipped_quietly(self):
        self.beat("claude")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(dispatcher.pick_lane("medium", {}), "claude")
        self.assertEqual(out.getvalue(), "")

    def test_every_routed_lane_has_a_worker(self):
        routed = {lane for lanes in dispatcher.DIFFICULTY_LANE.values() for lane in lanes}
        self.assertEqual(routed, {"anthropic", "claude", "codex", "openrouter", "opencode-go"})


if __name__ == "__main__":
    unittest.main()
