from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, scorecard  # noqa: E402


def status_row(lane: str, demoted: str) -> str:
    counts = " | ".join("0" for _ in scorecard.COLUMNS)
    return "| %s | model | %s | %s |" % (lane, counts, demoted)


class EscalateDemotedLaneTests(unittest.TestCase):
    def test_demoted_lane_is_excluded_even_when_status_says_yes(self):
        body = "\n".join([
            "| claude | yes | - |",
            "| codex | yes | - |",
            status_row("claude", "yes"),
            status_row("codex", "no"),
        ])
        with patch.object(brain, "_lane_status", return_value=body), \
                patch.dict(os.environ, {"LOOM_HOME": ""}):
            available = brain._available_lanes(object(), "example-owner/example-repo")

        self.assertNotIn("claude", available)

    def test_next_untried_rung_uses_codex_when_claude_is_demoted(self):
        body = "\n".join([
            "| claude | yes | - |",
            "| codex | yes | - |",
            status_row("claude", "yes"),
            status_row("codex", "no"),
        ])
        with patch.object(brain, "_lane_status", return_value=body), \
                patch.dict(os.environ, {"LOOM_HOME": ""}):
            available = brain._available_lanes(object(), "example-owner/example-repo")

        choice, _ = brain._rung_by_load(
            [1, 2], lambda rung: tuple(
                lane for lane in (("claude",) if rung == 1 else ("codex",))
                if lane in available), {})
        self.assertEqual((2, "codex"), choice)


if __name__ == "__main__":
    unittest.main()
