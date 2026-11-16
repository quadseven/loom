"""triage leaves a ready issue from an untrusted author at loom:triage."""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_bus_brain import INTAKE, REPO, Routes, item


class TriageUntrustedAuthorTests(unittest.TestCase):
    def run_triage(self, routes, authors=("example-brain",), briefers=("example-briefer",)):
        env = {"LOOM_BRIEF_AUTHORS": " ".join(authors),
               "LOOM_TRUSTED_BRIEFERS": " ".join(briefers)}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch.object(brain, "intake_epic", return_value=INTAKE), \
                unittest.mock.patch.object(brain, "_epic_tickets", return_value=[]):
            return brain.triage(GitHub("t", transport=routes), REPO, 1, 25)

    def test_ready_issue_from_outsider_stays_at_triage(self):
        routes = Routes([item(5, user={"login": "example-outsider"})])
        lines = self.run_triage(routes)
        self.assertEqual(routes.labelled(), {5: ["loom:triage"]})
        self.assertFalse(any(c[1].endswith("/sub_issues") for c in routes.calls))
        self.assertEqual(lines, ["#5 loom:triage, untrusted author"])

    def test_ready_issue_from_trusted_author_is_still_queued(self):
        routes = Routes([item(6, user={"login": "example-brain"}),
                         item(7, user={"login": "example-briefer"})])
        lines = self.run_triage(routes)
        self.assertEqual(routes.labelled(), {6: ["loom:ticket", "loom:queued"],
                                             7: ["loom:ticket", "loom:queued"]})
        self.assertEqual(lines, ["#6 queued under #3", "#7 queued under #3"])


if __name__ == "__main__":
    unittest.main()
