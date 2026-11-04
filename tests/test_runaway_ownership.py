"""Runaway reports keep tickets held by another operator apart."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402

REPO = "example-owner/example-repo"


def runaway(number: int, title: str) -> dict[str, object]:
    return {"number": number, "title": title, "state": "open",
            "labels": [{"name": "loom:runaway"}, {"name": "loom:failed"}]}


class RunawayOwnershipTest(unittest.TestCase):
    def test_local_report_separates_owned_runaway_ticket(self) -> None:
        held = runaway(7, "Held")
        free = runaway(8, "Free")
        with mock.patch.object(brain, "owned_elsewhere",
                               side_effect=lambda issue: "other-operator" if issue is held else None):
            lines = brain.runaway_lines([held, free])

        self.assertEqual(lines, ["### Runaway tickets", "- #8 Free",
                                 "- owned by other-operator: #7 Held"])

    def test_cross_repo_status_separates_owned_runaway_ticket(self) -> None:
        held = runaway(7, "Held")
        free = runaway(8, "Free")
        patches = [
            mock.patch.object(brain, "owned_elsewhere",
                              side_effect=lambda issue: "other-operator" if issue is held else None),
            mock.patch.object(brain, "priority_lines", return_value=[]),
            mock.patch.object(brain, "waiting_lines", return_value=[]),
            mock.patch.object(brain, "_health_lines", return_value=[]),
            mock.patch.object(brain, "validation_gap_lines", return_value=[]),
            mock.patch.object(brain, "_lane_status_line", return_value="ready"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        lines, unread = brain._status_sections(
            object(), [REPO], {REPO: [held, free]}, {}, "example-owner/lanes", [],
            datetime(2030, 1, 1, tzinfo=timezone.utc), {})

        self.assertEqual(unread, [])
        heading = lines.index("## Runaway tickets")
        self.assertEqual(lines[heading + 1:heading + 3], [
            "- %s Free" % ("%s#%d" % (REPO, 8)),
            "- owned by other-operator: %s Held" % ("%s#%d" % (REPO, 7)),
        ])


if __name__ == "__main__":
    unittest.main()
