"""A lane slot with no runnable model keeps its hold until it ends (loom#3226).

A slot that picks its model (run_lane.sh pick_held) stops a run at once with
"[limit] limit: no model can run: ..." when every model is paused or the paid
plan is ahead of its pace, and its heartbeat says unavailable until
PICK_UNTIL. Lane opencode-go-3 claimed one ticket eleven times in eight
hours that way: the hold did not reach the station's claim, so the same slot
claimed again, and the ticket ended at loom:failed instead of back in the
queue. The seams are the station pass (test_bus_station.Base) and the lane
heartbeat the station reads (bus.lane_state, through Station.limited_lanes
and Station.limit_until).
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))

from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_bus_station import Base, BODY_WITH_CRITERIA, REPO, GitHub, station  # noqa: E402
from bus import status, tickets  # noqa: E402

SLOT = "opencode-go-3"
NO_MODEL = ("limit: no model can run: paid models stop: monthly usage 68 is "
            "ahead of its 65 pace; opencode-go/beam-free paused: the CLI no "
            "longer offers it")
GO_BODY = BODY_WITH_CRITERIA.replace("Model: model-x", "Model: opencode-go/mimo-v2.6-flash")


class PickHeldClaimsTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.default_body = BODY_WITH_CRITERIA

    def held_beat(self, age, until_from_now):
        """The slot's heartbeat after a pick_held stop: unavailable, at a
        limit, with the hold's end as retry_at, written `age` seconds ago."""
        d = os.path.join(self.home, "state", "usage")
        os.makedirs(d, exist_ok=True)
        now = int(time.time())
        beat = {"lane": SLOT, "at": now - age, "available": False,
                "reason": NO_MODEL + "; unavailable until x",
                "retry_at": now + until_from_now}
        with open(os.path.join(d, SLOT + ".json"), "w") as fh:
            json.dump(beat, fh)
        return beat["retry_at"]

    def stopped_ticket(self, number=12):
        """A running ticket on the slot whose brief the lane parked with a
        no-model-can-run limit stop, reported by two station passes."""
        self.fake.add(number, state="running", lane=SLOT, body=GO_BODY)
        self.fake.comments[99] = []
        name = tickets.ticket_name(REPO, number, 1)
        self.write(self.inbox(SLOT, "failed", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[number], SLOT, 1))
        self.write(os.path.join(self.home, "logs", SLOT, name + ".log"),
                   "[t] %s start, lane %s\n[t] %s failed: [limit] %s\n"
                   % (name, SLOT, name, NO_MODEL))
        self.run_pass()
        self.run_pass()

    def test_the_station_holds_the_slot_until_the_pick_hold_ends(self):
        # The beat that wrote the hold is older than the freshness window by
        # the time a later pass claims: a pace hold lasts far longer than
        # HEARTBEAT_MAX_AGE, so freshness must not end it.
        until = self.held_beat(age=station.HEARTBEAT_MAX_AGE + 3600, until_from_now=3600)
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.assertEqual(runner.limited_lanes(), [SLOT])
        self.assertEqual(runner.limit_until(), {SLOT: float(until)})
        self.assertFalse(runner._heartbeat_ok(SLOT))
        # And a ticket briefed for that slot is not claimed for it in a pass.
        self.fake.add(13, lane=SLOT, body=GO_BODY)
        self.run_pass()
        self.assertEqual(self.listing(SLOT), [])
        self.assertNotIn("loom:running", self.fake.names(13))
        self.assertFalse([c for c in self.fake.comments[13]
                          if "Claimed for lane %s" % SLOT in str(c.get("body") or "")])

    def test_a_no_model_stop_returns_the_ticket_to_the_queue_without_a_lane(self):
        self.stopped_ticket()
        names = self.fake.names(12)
        self.assertIn("loom:queued", names)
        self.assertNotIn("loom:briefed", names)
        self.assertNotIn("loom:running", names)
        self.assertNotIn("loom:failed", names)
        self.assertNotIn("lane:" + SLOT, names)
        self.assertFalse([n for n in names if n.startswith("lane:")], names)
        events = [text for c in self.fake.comments[12] if status.is_status(c["body"])
                  for _, text in status.parse(c["body"])]
        self.assertTrue(any(text.startswith("Lane %s stopped: [limit] " % SLOT)
                            and "no model can run" in text for text in events), events)


if __name__ == "__main__":
    unittest.main()
