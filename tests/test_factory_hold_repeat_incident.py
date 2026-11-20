"""A cluster runner's setup failure that names an open factory incident holds
its ticket for that incident even when no other lane can take the retry."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bus import station  # noqa: E402
from bus.github import GitHub  # noqa: E402
import test_bus_station as base  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = base.REPO
SETUP = ("[factory] setup failed: base check exited 1 on the base at 2b30a63e; "
         "factory incident %s#99; the agent did not run" % REPO)


class RepeatIncidentHoldTest(base.FactoryIncidentTest):
    def ledger(self):
        path = os.path.join(self.home, "state", "station", "factory_held.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_setup_failure_naming_an_open_incident_is_held_for_it(self):
        self.incident(closed_at="")
        self.fake.factory_incidents[0]["state"] = "open"
        self.fake.issues[99] = dict(self.fake.factory_incidents[0])
        with mock.patch.dict(os.environ, {"LOOM_RUNNER_SLOTS": "free-gemma"}):
            self.heartbeat("free-gemma")
            self.failed(lane="free-gemma", reason=SETUP)
            with mock.patch.object(station.Station, "_retry",
                                   return_value="no other eligible lane is available"):
                self.run_pass()
        self.assertEqual(self.ledger().get("%s#12" % REPO, {}).get("incident"),
                         "%s#99" % REPO)
        self.assertIn("wait:incident", self.fake.names(12))
        self.assertNotIn("loom:failed", self.fake.names(12))
        # The incident closes: the release re-queues the ticket on its rung.
        self.incident()
        self.fake.issues[99] = dict(self.fake.factory_incidents[0])
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            station.Station(GitHub("token", transport=self.fake),
                            self.settings).release_factory_held()
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertNotIn("wait:incident", self.fake.names(12))
        self.assertNotIn("%s#12" % REPO, self.ledger())
        # A [work] failure puts nothing in the ledger.
        self.fake.comments[13] = []
        self.failed(number=13, lane="free-gemma",
                    reason="[work] agent made no commit on loom/13-x")
        with mock.patch.dict(os.environ, {"LOOM_RUNNER_SLOTS": "free-gemma"}):
            self.run_pass()
        self.assertNotIn("%s#13" % REPO, self.ledger())


for _name in [n for n in dir(RepeatIncidentHoldTest) if n.startswith("test_")
              and n != "test_a_setup_failure_naming_an_open_incident_is_held_for_it"]:
    setattr(RepeatIncidentHoldTest, _name, None)

if __name__ == "__main__":
    unittest.main()
