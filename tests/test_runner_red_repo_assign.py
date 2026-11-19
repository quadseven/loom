"""A queued ticket is not given to a runner slot whose base-red record
stops its repo (loom#3211).

The cluster runner's dispatch stop (bus/runners.py _base_red) refuses a
brief for a repo the slot's runner cannot check, but nothing before
dispatch read that record: the factory gave the slot the ticket, the
station claimed it, and the ticket sat claimed until dispatch refused it
and parked it. The factory now asks the same read (Runners.stops) when it
picks a lane for a queued ticket and skips a stopped slot, so the next
free lane of the tier takes the ticket.

Fixture rows come from the code that writes them: each base_red record
below is built by Runners._note_base from a validation row of the shape
the runner records (as in tests/test_runner_cannot_run_stop.py), exit 127
for a check that could not start (a tool missing from the runner image).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bus import factory, runners  # noqa: E402
from bus.github import Response  # noqa: E402
from test_bus_factory import ACCEPTANCE, Base, REPO, iso  # noqa: E402

SLOT = "free-gemma"
OTHER = "free-kilo"
TICKET = "example-org.example-repo-1"
NOW = 1_800_000_000.0


class Heads:
    """Answers a repo head read with a fixed revision."""

    def __init__(self, sha):
        self.sha = sha

    def rest(self, method, path, body=None, etag=None, ok=()):
        if method == "GET" and "/commits/" in path:
            return Response(200, {"sha": self.sha})
        raise AssertionError("unexpected request %s %s" % (method, path))


class StopsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runner = runners.Runners(Heads("aaa"), self._tmp.name, "example-org/loom",
                                      [SLOT], {}, lambda repo: True, lambda line: None,
                                      now=NOW)
        self.ledger = {"runs": {}, "holds": {}, "base_red": {},
                       "images": {SLOT: {"id": "image-a", "at": NOW}}}

    def note(self, *exits, base=""):
        rows = "".join(json.dumps({"phase": "baseline", "exit": code, "revision": "aaa",
                                   "ok": False,
                                   "detail": "missing ruff" if code == 127 else ""})
                       + "\n" for code in exits)
        files = {"logs/%s/%s.validation.jsonl" % (SLOT, TICKET): rows.encode()}
        self.runner._note_base(self.ledger,
                               {"slot": SLOT, "ticket": TICKET, "repo": REPO, "base": base},
                               files)
        self.runner.save(self.ledger)

    def test_a_cannot_run_record_stops_the_slot_for_the_repo(self):
        self.note(127)
        self.assertTrue(self.runner.stops(SLOT, REPO, ""))

    def test_no_record_stops_nothing(self):
        self.assertFalse(self.runner.stops(SLOT, REPO, ""))

    def test_one_red_run_stops_nothing(self):
        self.note(1)
        self.assertFalse(self.runner.stops(SLOT, REPO, ""))

    def test_two_red_runs_stop_the_slot(self):
        self.note(1, 1)
        self.assertTrue(self.runner.stops(SLOT, REPO, ""))

    def test_a_stop_names_its_own_slot_repo_and_base(self):
        self.note(127)
        self.assertFalse(self.runner.stops(OTHER, REPO, ""))
        self.assertFalse(self.runner.stops(SLOT, "example-org/other", ""))
        self.assertFalse(self.runner.stops(SLOT, REPO, "release"))

    def test_a_stop_on_a_named_base_holds_only_that_base(self):
        self.note(127, base="release")
        self.assertTrue(self.runner.stops(SLOT, REPO, "release"))
        self.assertFalse(self.runner.stops(SLOT, REPO, ""))


class AssignTest(Base):
    def queued(self, number, body="Difficulty: medium\n\n## Why\nx\n"):
        self.gh.ticket(number, "queued", body=body + ACCEPTANCE)
        self.gh.issues[number]["labels"] = [
            {"name": "loom:ticket"}, {"name": "loom:queued"}]
        self.gh.issues[number]["created_at"] = iso(0)

    def stops_with(self, *exits):
        """A Runners.stops read over a ledger holding these base rows."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = runners.Runners(Heads("aaa"), tmp.name, "example-org/loom",
                                 [SLOT], {}, lambda repo: True, lambda line: None,
                                 now=NOW)
        ledger = {"runs": {}, "holds": {}, "base_red": {},
                  "images": {SLOT: {"id": "image-a", "at": NOW}}}
        if exits:
            rows = "".join(json.dumps({"phase": "baseline", "exit": code,
                                       "revision": "aaa", "ok": False,
                                       "detail": "missing ruff"}) + "\n"
                           for code in exits)
            files = {"logs/%s/%s.validation.jsonl" % (SLOT, TICKET): rows.encode()}
            runner._note_base(ledger, {"slot": SLOT, "ticket": TICKET, "repo": REPO}, files)
            runner.save(ledger)
        return runner.stops

    def assign(self, stops):
        self.gh.private = False
        worker = factory.Factory(self.gh, [REPO], NOW, trusted=["alice-example"],
                                 available=[SLOT, OTHER], runner_stops=stops)
        worker.assign_queued()
        return worker

    def test_a_queued_ticket_skips_a_slot_stopped_for_its_repo(self):
        self.queued(7)
        self.assign(self.stops_with(127))
        self.assertEqual(factory._state(self.gh.issues[7]), "briefed")
        self.assertEqual(self.lane(7), OTHER)

    def test_a_queued_ticket_takes_the_slot_when_nothing_stops_it(self):
        self.queued(7)
        self.assign(self.stops_with())
        self.assertEqual(factory._state(self.gh.issues[7]), "briefed")
        self.assertEqual(self.lane(7), SLOT)

if __name__ == "__main__":
    unittest.main()
