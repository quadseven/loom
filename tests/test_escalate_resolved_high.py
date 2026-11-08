from __future__ import annotations

import os
import sys
import unittest
import json
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import brain
from bus.github import GitHub
import test_bus_brain as fixtures


class EscalateResolvedHighTests(unittest.TestCase):
    def test_resolved_high_finding_without_a_later_commit_is_not_finished(self):
        routes = fixtures.EscalateTests().finished_pr_routes()
        graphql = json.loads(routes[("POST", "/graphql")][2])
        details = graphql["data"]["repository"]["pullRequest"]
        details["commits"] = {"nodes": [{"commit": {
            "committedDate": "2030-01-01T12:45:00Z",
            "parents": {"totalCount": 1},
            "statusCheckRollup": {"state": "SUCCESS"},
        }}]}
        details["reviewThreads"]["nodes"] = [{
            "id": "T1",
            "isResolved": True,
            "comments": {"nodes": [{
                "author": {"__typename": "Bot", "login": "grug-tribe"},
                "body": "high | _correctness_ | `null-deref` | quick win",
                "createdAt": "2030-01-01T13:00:00Z",
            }]},
        }]
        routes[("POST", "/graphql")] = fixtures.reply(200, graphql)
        fake = fixtures.Routed(routes)

        with patch.dict(os.environ, {
                "LOOM_REVIEW_CHECK": "",
                "LOOM_BRIEF_AUTHORS": "trusted " + fixtures.BRAIN,
                "LOOM_REPOS": "",
                # The lanes repo escalate reads is a setting (loom#2584);
                # the fixtures serve it under quadseven/loom.
                "LOOM_SELF_REPO": "quadseven/loom",
        }):
            brain._LANE_LOAD.clear()
            self.assertIsNone(brain._nothing_to_fix(
                GitHub("t", transport=fake), fixtures.REPO, 8))
            outcome = brain.escalate(GitHub("t", transport=fake), fixtures.REPO, 9)

        self.assertEqual(outcome, "rung 1 claude-sonnet-5-5 on claude")


if __name__ == "__main__":
    unittest.main()
