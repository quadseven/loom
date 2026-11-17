"""A cluster runner check that cannot start stops its repo on the slot after
one run (loom#3140).

An exit-127 base check is a tool the runner image lacks (`missing make`). It
records one failing row per run, not the two a red test records, and no retry
or new commit can add the tool. So one such report stops the repo on that
slot; a red exit outside 125 to 127 keeps the two-failure rule.
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

SLOT = "opencode-go-2"
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


class CannotRunStopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runner = runners.Runners(Heads("aaa"), self._tmp.name, "owner/loom", [SLOT], {},
                                      lambda repo: True, lambda line: None, now=NOW)
        self.ledger = {"runs": {}, "holds": {}, "base_red": {},
                       "images": {SLOT: {"id": "image-a", "at": NOW}}}

    def report(self, *exits):
        rows = "".join(json.dumps({"phase": "baseline", "exit": code, "revision": "aaa",
                                   "ok": False, "detail": "missing make" if code == 127 else ""})
                       + "\n" for code in exits)
        files = {"logs/%s/%s.validation.jsonl" % (SLOT, TICKET): rows.encode()}
        self.runner._note_base(self.ledger, {"slot": SLOT, "ticket": TICKET, "repo": REPO}, files)

    def test_one_report_of_a_check_that_could_not_start_stops_the_repo(self):
        self.report(127)
        red = self.runner._base_red(self.ledger, SLOT, REPO, "")
        self.assertIsNotNone(red, "one exit-127 run must stop the repo on the slot")
        self.assertTrue(red["cannot_run"])
        self.assertEqual(red["why"], "missing make")

    def test_every_exit_that_means_the_check_could_not_run_stops_after_one(self):
        for code in runners.CANNOT_RUN:
            with self.subTest(exit=code):
                self.ledger["base_red"] = {}
                self.report(code)
                self.assertIsNotNone(self.runner._base_red(self.ledger, SLOT, REPO, ""))

    def test_one_red_test_run_still_holds_nothing(self):
        self.report(1)
        self.assertIsNone(self.runner._base_red(self.ledger, SLOT, REPO, ""))

    def test_the_day_probe_still_lifts_a_one_run_stop(self):
        self.report(127)
        self.runner.now = NOW + runners.IMAGE_PROBE + 1
        self.assertIsNone(self.runner._base_red(self.ledger, SLOT, REPO, ""))


if __name__ == "__main__":
    unittest.main()
