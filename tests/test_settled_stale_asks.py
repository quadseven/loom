"""Settled stale evidence stays held without creating another person ask."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_brief_check as fixtures
from test_brief_check import HEALTH_INTAKE, OPERATOR, REPO, STALE_REWRITE, ticket


class SettledStaleAsks(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StationClaim()

    def check(self, extra, body=STALE_REWRITE):
        issue = ticket(body=body + "\n" + extra)
        fake = self.fixture.routes(issue)
        fake.routes["/repos/%s/issues/124" % REPO] = {"number": 124, "state": "open"}
        fake.routes["/repos/%s/issues/206/timeline" % REPO] = [{
            "event": "cross-referenced", "source": {"type": "issue", "issue": {
                "number": 31, "body": "Refs %s#206" % REPO, "title": "Ship it",
                "repository": {"full_name": fixtures.OTHER},
                "pull_request": {"merged_at": fixtures.iso(3600)}}}}]
        held = self.fixture.station(fake)._brief_holds(REPO, issue, [])
        return held, fake

    def assert_held_without_ask(self, extra):
        held, fake = self.check(extra)
        self.assertTrue(held, "settling a person ask must not authorize a stale build")
        self.assertEqual(fake.writes, [], "keep labels and comments unchanged")

    def test_a_recorded_operator_decision_does_not_raise_another_ask(self):
        self.assert_held_without_ask(
            "## Decision (operator %s, 2026-10-26)\nKeep the ticket open for the live result.\n"
            % OPERATOR)

    def test_the_existing_named_operator_decision_format_is_honoured(self):
        self.assert_held_without_ask(
            "## Decision (%s, 2026-10-26)\nKeep the ticket open for the live result.\n"
            % OPERATOR)

    def test_a_non_person_blocker_does_not_raise_another_ask(self):
        self.assert_held_without_ask(
            "## Blocked on\nThe next scheduled event, not a person and not another ticket.\n")

    def test_all_supported_non_person_blocker_headings_settle_the_ask(self):
        for heading in ("### Blocked on", "# Blocked by", "**Blocked on**", "Blocked on:"):
            with self.subTest(heading=heading):
                self.assert_held_without_ask(heading + "\nThe next scheduled event.\n")

    def test_a_person_blocker_takes_priority_over_an_earlier_decision(self):
        decision = "## Decision (%s, 2026-10-26)\nKeep it open.\n" % OPERATOR
        for heading in ("### Blocked on", "# Blocked by", "**Blocked on**", "Blocked on:"):
            with self.subTest(heading=heading):
                _, fake = self.check(decision + heading + "\nneeds:the-operator to prepare it.\n")
                self.assertTrue(any(body == {"labels": ["needs:human"]}
                                    for _, _, body in fake.writes))

    def test_a_named_person_blocker_still_raises_an_ask(self):
        for blocker in ("needs:the-operator to prepare the move", "@the-operator to prepare it"):
            with self.subTest(blocker=blocker):
                held, fake = self.check("## Blocked on\n%s\n" % blocker)
                self.assertTrue(held)
                self.assertIn(("POST", "/repos/%s/issues/206/labels" % REPO,
                               {"labels": ["needs:human"]}), fake.writes)

    def test_empty_or_untrusted_decisions_do_not_settle_an_ask(self):
        for extra in ("## Blocked on\n", "## Decision (upstream, 2026-10-26)\nKeep it.\n",
                      "## Decision (operator %s, 2026-10-26)\n" % OPERATOR):
            with self.subTest(extra=extra):
                _, fake = self.check(extra)
                self.assertTrue(any(body == {"labels": ["needs:human"]}
                                    for _, _, body in fake.writes))

    def test_examples_and_original_issue_text_do_not_settle_the_current_ask(self):
        decision = "## Decision (operator %s, 2026-10-26)\nKeep it open.\n" % OPERATOR
        for extra in ("```markdown\n" + decision + "```\n",
                      "````markdown\n```\n" + decision + "````\n",
                      "```markdown\n```still-an-example\n" + decision + "```\n",
                      "<!--\n" + decision + "-->\n",
                      "## Original issue\n" + decision,
                      "> ## Blocked on\n> The next scheduled event.\n"):
            with self.subTest(extra=extra):
                _, fake = self.check(extra)
                self.assertTrue(any(body == {"labels": ["needs:human"]}
                                    for _, _, body in fake.writes))

    def test_the_health_diagnosis_gate_still_holds(self):
        held, fake = self.check("## Blocked on\nThe next scheduled event.\n", body=HEALTH_INTAKE)
        self.assertTrue(held)
        self.assertTrue(any(body == {"labels": ["needs-brain"]}
                            for _, _, body in fake.writes))

    def test_a_fresh_ticket_is_not_held_for_its_decision_alone(self):
        issue = ticket(body="## Acceptance criteria\n- [ ] The feature works.\n"
                       "## Decision (%s, 2026-10-26)\nBuild the feature.\n" % OPERATOR)
        fake = self.fixture.routes(issue)
        self.assertFalse(self.fixture.station(fake)._brief_holds(REPO, issue, []))
        self.assertEqual(fake.writes, [])
