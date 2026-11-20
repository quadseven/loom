"""A stale brief whose merged pull request covers a ticket with every box
ticked moves the ticket to loom:merged and drops its lane, not an operator ask."""
from unittest import mock

from bus import brief_check, station
from bus.github import GitHub
from test_bus_station import Base, REPO
from shadow_env import setUpModule, tearDownModule  # noqa: F401

TICKED = ("## Acceptance criteria\n"
          "- [x] A test shows the change.\n"
          "- [x] Live: the log shows the new line.\n")
OPEN = ("## Acceptance criteria\n"
        "- [x] A test shows the change.\n"
        "- [ ] A second test shows the rest.\n")
VERDICT = brief_check.Verdict(brief_check.STALE, "pr-%s#77" % REPO,
                              "pull request %s#77, which names this ticket, merged" % REPO)


class TickedMerged(Base):
    def run_hold(self, body):
        self.fake.add(12, state="queued", lane="claude", body=body)
        self.fake.issues[12]["labels"].append({"name": "awaiting-live-check"})
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        notes = []
        with mock.patch.object(brief_check, "check", return_value=VERDICT), \
                mock.patch.object(runner, "_routine",
                                  side_effect=lambda r, n, t: notes.append(t)), \
                mock.patch.object(runner, "_apply_brief_hold",
                                  return_value=True) as ask:
            held = runner._brief_holds(REPO, self.fake.issues[12], [])
        return held, notes, ask

    def test_every_box_ticked_moves_to_merged_without_its_lane(self):
        held, notes, ask = self.run_hold(TICKED)
        self.assertTrue(held)
        ask.assert_not_called()
        names = self.fake.names(12)
        self.assertIn("loom:merged", names)
        self.assertNotIn("loom:queued", names)
        self.assertNotIn("lane:claude", names)
        self.assertTrue(any("#77" in n for n in notes))

    def test_the_move_keeps_awaiting_live_check(self):
        self.run_hold(TICKED)
        self.assertIn("awaiting-live-check", self.fake.names(12))

    def test_an_unticked_box_keeps_the_existing_hold(self):
        held, notes, ask = self.run_hold(OPEN)
        self.assertTrue(held)
        ask.assert_called_once()
        self.assertIn("lane:claude", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))
