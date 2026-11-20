"""escalate and status leave a ticket alone that already waits on its next rung."""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests import test_bus_brain as base  # noqa: E402
from tests.test_bus_brain import (  # noqa: E402
    BODY, REPO, TRUSTED, TWICE, Routed, claim, lanes_routes, reply, stuck_ticket,
    ticket_routes,
)


def issue_on(model, lane):
    """A stuck ticket briefed on a model and one lane."""
    issue = stuck_ticket("failed", body="Model: %s\n\n%s" % (model, BODY))
    issue["labels"] = [{"name": n} for n in
                       ("loom:ticket", "loom:briefed", "lane:" + lane)]
    return issue


class SameRungTest(unittest.TestCase):
    setUp = base.EscalateTests.setUp
    run_escalate = base.EscalateTests.run_escalate

    def test_escalate_refuses_a_ticket_already_on_its_next_rung(self):
        # Sonnet on lane claude is rung 1, and no claim has run there.
        with self.assertRaises(ValueError) as caught:
            self.run_escalate(ticket_routes(issue_on("claude-sonnet-5-5", "claude"),
                                            TWICE, []))
        self.assertIn("rung 1", str(caught.exception))

    def test_a_refused_escalate_writes_nothing(self):
        fake = Routed(ticket_routes(issue_on("claude-sonnet-5-5", "claude"), TWICE, []))
        gh = GitHub("t", transport=fake)
        with unittest.mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": base.BRAIN}):
            with self.assertRaises(ValueError):
                brain.escalate(gh, REPO, 9)
        self.assertEqual([c for c in fake.calls if c[0] != "GET" and c[1] != "/graphql"], [])

    def test_a_ticket_that_ran_and_failed_on_its_rung_still_climbs(self):
        fake, outcome = self.run_escalate(ticket_routes(
            issue_on("claude-sonnet-5-5", "claude"), TWICE,
            [claim("2030-01-01T00:11:00Z")]))
        self.assertEqual(outcome, "rung 2 gpt-6-luna on codex")

    def status(self, issue, comments):
        routes = {("GET", "/repos/%s/issues" % REPO): reply(200, [issue]),
                  ("GET", "/issues/9/events"): reply(200, TWICE),
                  ("GET", "/issues/9/comments"): reply(200, comments),
                  ("GET", "/repos/%s/pulls" % REPO): reply(200, [])}
        routes.update(lanes_routes(()))
        return brain.status(GitHub("t", transport=Routed(routes)), [REPO], REPO,
                            env=TRUSTED)

    def test_status_does_not_mark_a_ticket_waiting_on_its_next_rung(self):
        report = self.status(issue_on("claude-sonnet-5-5", "claude"), [])
        self.assertNotIn("next rung 1", report)
        self.assertNotIn("REPEATEDLY STUCK", report)

    def test_status_still_marks_a_ticket_that_failed_on_its_rung(self):
        report = self.status(issue_on("gpt-6-luna", "codex"),
                             [claim("2030-01-01T00:11:00Z", lane="codex")])
        self.assertIn("REPEATEDLY STUCK", report)
        self.assertIn("next rung 1", report)


if __name__ == "__main__":
    unittest.main()
