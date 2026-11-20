"""A base check red on one cluster slot stops the repo on every cluster
slot, not one slot at a time (loom#3292).

The runner image is shared, so a base failure caused by the image, not the
repo, is learned once: when one slot's record stops a repo on a base at a
revision, _base_red returns that stop for any other slot asking about the
same repo, base and revision. The clearing rules stay the recording slot's:
a moved head clears it, and so does a day with no report from the slot
that recorded it.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import runners  # noqa: E402
from bus.github import Response  # noqa: E402

SLOT_A = "opencode-go-1"
SLOT_B = "opencode-go-2"
REPO = "owner/app"
TICKET = "owner.app-1"
NOW = 1_800_000_000.0


class Heads:
    """Answers a repo head read with a fixed revision."""

    def __init__(self, sha):
        self.sha = sha

    def rest(self, method, path, body=None, etag=None, ok=()):
        if method == "GET" and "/commits/" in path:
            return Response(200, {"sha": self.sha})
        raise AssertionError("unexpected request %s %s" % (method, path))


class SharedBaseRedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.heads = Heads("aaa")
        self.runner = runners.Runners(self.heads, self._tmp.name, "owner/loom",
                                      [SLOT_A, SLOT_B], {},
                                      lambda repo: True, lambda line: None, now=NOW)
        self.ledger = {"runs": {}, "holds": {}, "base_red": {},
                       "images": {SLOT_A: {"id": "image-a", "at": NOW},
                                  SLOT_B: {"id": "image-a", "at": NOW}}}

    def report(self, *exits):
        """Record base rows for REPO at revision aaa on slot A."""
        rows = "".join(json.dumps({"phase": "baseline", "exit": code, "revision": "aaa",
                                   "ok": False, "detail": "red check"})
                       + "\n" for code in exits)
        files = {"logs/%s/%s.validation.jsonl" % (SLOT_A, TICKET): rows.encode()}
        self.runner._note_base(self.ledger, {"slot": SLOT_A, "ticket": TICKET, "repo": REPO}, files)

    def test_two_red_rows_on_one_slot_stop_the_repo_on_another_slot(self):
        self.report(1, 1)
        red = self.runner._base_red(self.ledger, SLOT_B, REPO, "")
        self.assertIsNotNone(red, "a stop recorded on slot A must stop the repo on slot B")
        self.assertEqual(red["revision"], "aaa")

    def test_a_moved_head_clears_a_shared_stop(self):
        self.report(1, 1)
        self.heads.sha = "bbb"
        self.assertIsNone(self.runner._base_red(self.ledger, SLOT_B, REPO, ""))

    def test_one_red_row_on_one_slot_stops_nothing_on_another_slot(self):
        self.report(1)
        self.assertIsNone(self.runner._base_red(self.ledger, SLOT_B, REPO, ""))

    def test_a_day_with_no_report_from_the_recording_slot_clears_a_shared_stop(self):
        self.report(1, 1)
        self.runner.now = NOW + runners.IMAGE_PROBE + 1
        self.assertIsNone(self.runner._base_red(self.ledger, SLOT_B, REPO, ""))


if __name__ == "__main__":
    unittest.main()
