"""Escalate leaves out lanes the scorecard has demoted (loom#2933)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, scorecard

REPO = "example-owner/example-repo"
AUTHORS = ["example-brain"]


class AvailableLanesDemotedTests(unittest.TestCase):
    def test_scorecard_table_in_lane_status_drops_demoted_lane(self):
        available = ("<!-- loom:lanes v1 -->\n| lane | available | heartbeat (UTC) |\n"
                     "|---|---|---|\n| claude | yes | - |\n| codex | yes | - |")
        tmp = tempfile.mkdtemp()
        card = scorecard.Scorecard(tmp, 1000.0)
        card.data["demoted"]["claude"] = {"at": 1.0, "why": "bad", "kind": "bad"}
        body = available + "\n" + "\n".join(card.markdown())

        class GH:
            pass

        gh = GH()
        with mock.patch.object(brain, "_lane_status", return_value=body):
            self.assertEqual(brain._available_lanes(gh, REPO, AUTHORS), ["codex"])

    def test_station_scorecard_file_drops_demoted_lane(self):
        home = tempfile.mkdtemp()
        state = os.path.join(home, "state", "station")
        os.makedirs(state)
        card = scorecard.Scorecard(state, 1000.0)
        card.data["demoted"]["claude"] = {"at": 1.0, "why": "bad", "kind": "bad"}
        card.save()
        available = ("<!-- loom:lanes v1 -->\n| lane | available | heartbeat (UTC) |\n"
                     "|---|---|---|\n| claude | yes | - |\n| codex | yes | - |")
        class GH:
            pass

        gh = GH()
        with mock.patch.dict(os.environ, {"LOOM_HOME": home}), \
                mock.patch.object(brain, "_lane_status", return_value=available):
            self.assertEqual(brain._available_lanes(gh, REPO, AUTHORS), ["codex"])