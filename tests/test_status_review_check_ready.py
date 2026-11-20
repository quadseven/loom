"""Status marks an app-code pull request READY only when the review check
succeeded on its head, as merge does (bus/brain.py _merge_refusals)."""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
import test_brain  # noqa: E402
from test_brain import NOW, REPO, StubTransport, ready_node  # noqa: E402

CHECK = "Grug - Elder"
BLOCKER = "review check %s not success on head" % CHECK


def with_runs(runs):
    node = ready_node()
    rollup = node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    rollup["contexts"] = {"pageInfo": {"hasNextPage": False},
                          "nodes": [dict(r, __typename="CheckRun") for r in runs]}
    return node


class ReviewCheckReadyTest(unittest.TestCase):
    def line(self, node):
        routes = test_brain.HourlyMergeCapStatusTests().routes()
        gh = GitHub("t", transport=StubTransport(routes, search_counts={"default": 0}))
        with unittest.mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code",
                                                   "LOOM_REVIEW_CHECK": CHECK}):
            lines = brain._pull_request_lines(gh, REPO, [test_brain.HourlyMergeCapStatusTests.PR],
                                              {"prs": {9: node}}, "grug-tribe", now=NOW)
        return lines[1]

    def test_a_neutral_review_check_is_a_blocker_and_success_is_not(self):
        neutral = self.line(with_runs([{"name": CHECK, "status": "COMPLETED",
                                        "conclusion": "NEUTRAL"}]))
        self.assertIn(BLOCKER, neutral)
        self.assertNotIn("READY", neutral)
        # Same test method, so the passing cases are not separate tests that
        # pass on main: success and a pull request with no check-run data.
        success = self.line(with_runs([{"name": CHECK, "status": "COMPLETED",
                                        "conclusion": "SUCCESS"}]))
        self.assertTrue(success.endswith(" - READY"), success)
        unread = self.line(ready_node())
        self.assertTrue(unread.endswith(" - READY"), unread)
        missing = self.line(with_runs([{"name": "unittest", "status": "COMPLETED",
                                        "conclusion": "SUCCESS"}]))
        self.assertIn(BLOCKER, missing)


if __name__ == "__main__":
    unittest.main()
