"""A runaway ticket waiting on an operator ask is listed apart from review."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402

REPO = "example-owner/example-repo"


def runaway(number: int, title: str, ask: bool) -> dict[str, object]:
    names = ["loom:runaway", "loom:failed"] + (["needs:human"] if ask else [])
    return {"number": number, "title": title, "state": "open",
            "labels": [{"name": n} for n in names]}


class RunawayOperatorAskTest(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (("owned_elsewhere", None), ):
            patcher = mock.patch.object(brain, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_local_report_lists_ask_after_review(self) -> None:
        lines = brain.runaway_lines([runaway(7, "Asked", True),
                                     runaway(8, "Review", False)])
        self.assertEqual(lines, ["### Runaway tickets", "- #8 Review",
                                 "- waiting on the operator: #7 Asked"])

    def test_cross_repo_status_lists_ask_after_review(self) -> None:
        for name, value in (("priority_lines", []), ("waiting_lines", []),
                            ("_health_lines", []), ("validation_gap_lines", []),
                            ("_lane_status_line", "ready")):
            patcher = mock.patch.object(brain, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        lines, _ = brain._status_sections(
            object(), [REPO], {REPO: [runaway(7, "Asked", True),
                                      runaway(8, "Review", False)]}, {},
            "example-owner/lanes", [],
            datetime(2030, 1, 1, tzinfo=timezone.utc), {})
        heading = lines.index("## Runaway tickets")
        self.assertEqual(lines[heading + 1:heading + 3], [
            "- %s#8 Review" % REPO,
            "- waiting on the operator: %s#7 Asked" % REPO])


if __name__ == "__main__":
    unittest.main()
