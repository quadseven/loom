"""A ladder assignment waits for a claim without spending another move."""
import datetime
import os
import unittest
from unittest.mock import patch

from bus import brain, factory
from bus.github import GitHub
from tests.test_bus_brain import (BODY, BRAIN, OLD, REPO, TRUSTED, TWICE, Routed,
                                  claim, lanes_routes, reply, stuck_ticket, ticket_routes)


class SameRungTests(unittest.TestCase):
    def test_escalate_leaves_an_unclaimed_rung_alone(self):
        for state in ("briefed", "queued"):
            with self.subTest(state=state):
                issue = stuck_ticket(state, created=OLD)
                routes = ticket_routes(issue, TWICE, [])
                routes[("GET", "/repos/%s/pulls" % REPO)] = reply(200, [])
                fake = Routed(routes)
                with patch.dict(os.environ, {**TRUSTED, "LOOM_REPOS": "",
                                             "LOOM_UNCLAIMED_MOVE_CAP": "6"}), \
                        patch.object(brain, "_LANE_LOAD", {}):
                    outcome = brain.escalate(GitHub("t", transport=fake), REPO, 9,
                                             lanes_repo="quadseven/loom")
                self.assertEqual([method for method, _, _ in fake.calls if method != "GET"], [])
                self.assertIn("waiting for lane claude on rung 1 claude-sonnet-5-5", outcome)

    def test_status_shows_the_wait_and_still_marks_a_ticket_that_failed(self):
        briefed = stuck_ticket("briefed", created=OLD)
        queued = dict(stuck_ticket("queued", created=OLD), number=10)
        failed = dict(stuck_ticket("briefed", body="Model: gpt-6-luna\n\n" + BODY),
                      number=11, labels=[{"name": name} for name in
                                         ("loom:ticket", "loom:briefed", "lane:codex")])
        routes = lanes_routes(())
        routes.update({
            ("GET", "/repos/%s/issues" % REPO): reply(200, [briefed, queued, failed]),
            ("GET", "/repos/%s/pulls" % REPO): reply(200, []),
            ("GET", "/issues/9/events"): reply(200, TWICE),
            ("GET", "/issues/10/events"): reply(200, TWICE),
            ("GET", "/issues/11/events"): reply(200, TWICE),
            ("GET", "/issues/11/comments"): reply(200, [claim("2030-01-01T00:09:00Z", "codex")]),
        })
        report = brain.status(GitHub("t", transport=Routed(routes)), [REPO], REPO,
                              now=datetime.datetime(2030, 1, 2, tzinfo=datetime.timezone.utc),
                              env=TRUSTED)
        lines = {line.split()[1]: line for line in report.splitlines() if line.startswith("- #")}
        for number in ("#9", "#10"):
            self.assertIn("waiting for lane claude on rung 1 claude-sonnet-5-5", lines[number])
            self.assertNotIn("next rung", lines[number])
            self.assertNotIn("REPEATEDLY STUCK", lines[number])
        self.assertIn("REPEATEDLY STUCK", lines["#11"])
        self.assertIn("next rung 1 claude-sonnet-5-5", lines["#11"])

    def test_move_cap_counts_changes_of_model_or_lane(self):
        destinations = (
            "1 claude-sonnet-5-5 claude",
            "1 claude-sonnet-5-5 claude",
            "3 claude-opus-5-5 claude",
            "3 claude-opus-5-5 anthropic",
            "3 claude-opus-5-5 anthropic",
        )
        comments = [{"body": "%s\nRung: %s" % (brain.ESCALATE_MARKER, destination),
                     "user": {"login": BRAIN}, "created_at": "2030-01-01T00:00:0%dZ" % i}
                    for i, destination in enumerate(destinations)]
        self.assertEqual(factory.unclaimed_moves(comments, [BRAIN]), 3)
