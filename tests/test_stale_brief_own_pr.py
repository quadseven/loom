"""merged_linked passes over a pull request the station already judged for the
ticket, or one opened from the ticket's own branch."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brief_check, status, tickets  # noqa: E402

REPO = "example-owner/example-app"
NUMBER = 3001
PULL = 3012
NOW = 2_000_000_000.0


def iso(ago: float) -> str:
    return datetime.fromtimestamp(NOW - ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Pulls:
    def __init__(self, branch: str) -> None:
        self.branch = branch

    def rest(self, method: str, path: str) -> SimpleNamespace:
        assert path == "/repos/%s/pulls/%d" % (REPO, PULL), path
        return SimpleNamespace(data={"merged_at": iso(3600), "title": "Change",
                                     "head": {"ref": self.branch}})


def issue(events: list[tuple[str, str]]) -> dict[str, object]:
    body = "## Why\nA change.\n\nhttps://github.com/%s/pull/%d\n" % (REPO, PULL)
    return {"number": NUMBER, "created_at": iso(86400),
            "body": status.with_body_events(body, events)}


def verdict(branch: str, events: list[tuple[str, str]]):
    return brief_check.merged_linked(
        Pulls(branch), REPO, issue(events), [], [], lambda merged: False)


class OwnPullTest(unittest.TestCase):
    def test_a_pull_request_the_requeue_judged_is_passed_over(self) -> None:
        note = tickets.UNBUILT_NOTE % (PULL, "boxes 2 and 3")
        self.assertIsNone(verdict("someone/other-branch", [(iso(1800), note)]))

    def test_the_tickets_own_branch_is_passed_over_and_another_is_not(self) -> None:
        self.assertIsNone(verdict("loom/%d-the-change-r3" % NUMBER, []))
        got = verdict("someone/other-branch", [])
        self.assertIsNotNone(got)
        self.assertEqual(got.kind, brief_check.STALE)


if __name__ == "__main__":
    unittest.main()
