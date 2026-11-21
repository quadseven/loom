"""Status does not read a lane pull request READY when its own body leaves a
ticket code box as `- [ ]`: merging it would requeue the ticket (loom#2955)."""
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
from test_brain import NOW, REPO, StubTransport, ready_node, reply  # noqa: E402

TICKET_BODY = (
    "## Acceptance criteria\n"
    "- [ ] A unit test shows the new verdict (fails on main).\n"
    "- [ ] Live: after deploy, a status read shows the verdict.\n"
)


class ReadyUntickedBoxesTest(unittest.TestCase):
    def line(self, pr_body, ticket_body=TICKET_BODY):
        base = test_brain.HourlyMergeCapStatusTests()
        routes = base.routes()
        routes[("GET", "/issues/7")] = reply(200, {
            "number": 7, "state": "open", "body": ticket_body,
            "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]})
        routes[("GET", "/pulls/9/files")] = reply(200, [
            {"filename": "bus/a.py", "patch": "+x"},
            {"filename": "tests/test_a.py", "patch": "+y"}])
        pr = dict(base.PR, body=pr_body)
        gh = GitHub("t", transport=StubTransport(routes, search_counts={"default": 0}))
        with unittest.mock.patch.dict(os.environ,
                                      {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"}):
            lines = brain._pull_request_lines(gh, REPO, [pr], {"prs": {9: ready_node()}},
                                              "grug-tribe", now=NOW)
        return lines[1]

    def test_an_unticked_code_box_in_the_body_blocks_ready(self):
        line = self.line("Refs #7\n\n## Acceptance criteria\n"
                         "- [ ] A unit test shows the new verdict (fails on main).\n")
        self.assertIn("criteria not ticked", line)
        self.assertNotIn(" - READY", line)
        # Ticked code boxes with only the Live box open, and a body with no
        # checkbox list at all, keep today's verdict.
        ticked = self.line("Refs #7\n\n## Acceptance criteria\n"
                           "- [x] A unit test shows the new verdict (fails on main).\n"
                           "- [ ] Live: after deploy, a status read shows the verdict.\n")
        self.assertTrue(ticked.endswith(" - READY"), ticked)
        bare = self.line("Refs #7")
        self.assertTrue(bare.endswith(" - READY"), bare)


if __name__ == "__main__":
    unittest.main()
