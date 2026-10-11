"""Brief rendering: what a lane is told to do."""

import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
