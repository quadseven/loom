"""Settled stale evidence stays held without creating another person ask."""
from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_brief_check as fixtures
from test_brief_check import HEALTH_INTAKE, OPERATOR, REPO, STALE_REWRITE, ticket


class SettledStaleAsks(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StationClaim()

    def check(self, extra, body=STALE_REWRITE, labels=None):
        issue = ticket(body=body + "\n" + extra, **({"labels": labels} if labels else {}))
        fake = self.fixture.routes(issue)
        fake.routes["/repos/%s/issues/124" % REPO] = {"number": 124, "state": "open"}
        fake.routes["/repos/%s/issues/206/timeline" % REPO] = [{
            "event": "cross-referenced", "source": {"type": "issue", "issue": {
                "number": 31, "body": "Refs %s#206" % REPO, "title": "Ship it",
                "repository": {"full_name": fixtures.OTHER},
                "pull_request": {"merged_at": fixtures.iso(3600)}}}}]
        held = self.fixture.station(fake)._brief_holds(REPO, issue, [])
        return held, fake

    def assert_held_without_ask(self, extra, labels=None):
        held, fake = self.check(extra, labels=labels)
        self.assertTrue(held, "settling a person ask must not authorize a stale build")
        self.assertEqual(fake.writes, [], "keep labels and comments unchanged")

    def test_a_recorded_operator_decision_does_not_raise_another_ask(self):
        self.assert_held_without_ask(
            "## Decision (operator %s, 2026-10-26)\nKeep the ticket open for the live result.\n"
            % OPERATOR, labels=("loom:ticket", "blocked"))

    def test_the_existing_named_operator_decision_format_is_honoured(self):
        self.assert_held_without_ask(
            "## Decision (%s, 2026-10-26)\nKeep the ticket open for the live result.\n"
            % OPERATOR, labels=("loom:ticket", "awaiting-live-check"))

    def test_a_decision_to_build_with_later_stale_evidence_still_asks(self):
        held, fake = self.check("## Decision (%s, 2026-10-26)\nBuild option B.\n" % OPERATOR)
        self.assertTrue(held)
        self.assertIn(("POST", "/repos/%s/issues/206/labels" % REPO,
                       {"labels": ["needs:human"]}), fake.writes)

    def test_a_stated_wait_uses_no_remote_reads_or_repeated_run_logs(self):
        issue = ticket(body=STALE_REWRITE + "\n## Blocked on\nThe next scheduled event.\n",
                       labels=("loom:ticket", "blocked"))
        fake = self.fixture.routes(issue)
        with mock.patch.object(fixtures.station, "_say") as say:
            for _ in range(2):
                worker = self.fixture.station(fake)
                self.assertTrue(worker._brief_holds(REPO, issue, []))
                self.assertTrue(worker._brief_holds(REPO, issue, []))
            say.assert_not_called()
        self.assertEqual(fake.reads, [])
        self.assertEqual(fake.writes, [])

    def test_closed_dependencies_and_empty_waits_do_not_park_a_fresh_ticket(self):
        for blocker in ("#123", "None", "None at this time", "No blockers known",
                        "Nothing blocks this ticket", "TBD", "The next scheduled event",
                        "N/A", "-", ""):
            with self.subTest(blocker=blocker):
                issue = ticket(body="## Acceptance criteria\n- [ ] Build the dependent feature.\n"
                               "## Blocked on\n%s\n" % blocker)
                fake = self.fixture.routes(issue)
                fake.routes["/repos/%s/issues/123" % REPO] = {"number": 123, "state": "closed"}
                self.assertFalse(self.fixture.station(fake)._brief_holds(REPO, issue, []))
                self.assertEqual(fake.writes, [])

    def test_a_non_person_blocker_does_not_raise_another_ask(self):
        self.assert_held_without_ask(
            "## Blocked on\nThe next scheduled event, not a person and not another ticket.\n",
            labels=("loom:ticket", "blocked"))

    def test_all_supported_non_person_blocker_headings_settle_the_ask(self):
        for heading in ("### Blocked on", "# Blocked by", "**Blocked on**", "Blocked on:"):
            with self.subTest(heading=heading):
                self.assert_held_without_ask(heading + "\nThe next scheduled event.\n",
                                             labels=("loom:ticket", "awaiting-live-check"))

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
                _, fake = self.check(extra, labels=("loom:ticket", "blocked"))
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
                _, fake = self.check(extra, labels=("loom:ticket", "blocked"))
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

    def test_a_stale_event_wait_gets_one_visible_label_then_no_more_reads_or_logs(self):
        issue = ticket(body=STALE_REWRITE + "\n## Blocked on\nThe next scheduled event.\n")
        fake = self.fixture.routes(issue)
        worker = self.fixture.station(fake)
        with mock.patch.object(fixtures.station, "_say") as say:
            self.assertTrue(worker._brief_holds(REPO, issue, []))
            first_reads = list(fake.reads)
            self.assertTrue(first_reads, "unlabelled text is checked for stale evidence first")
            self.assertTrue(worker._brief_holds(REPO, issue, []))
            self.assertTrue(self.fixture.station(fake)._brief_holds(REPO, issue, []))
            self.assertEqual(fake.reads, first_reads)
            self.assertEqual(say.call_count, 1)
            self.assertIn("blocked", say.call_args.args[0])
        self.assertEqual(fake.writes, [
            ("POST", "/repos/%s/issues/206/labels" % REPO, {"labels": ["blocked"]})])
        self.assertIn({"name": "blocked"}, issue["labels"])

    def test_a_failed_wait_label_write_does_not_cache_the_unlabelled_hold(self):
        issue = ticket(body=STALE_REWRITE + "\n## Blocked on\nThe next scheduled event.\n")
        fake = self.fixture.routes(issue)
        worker = self.fixture.station(fake)
        real_rest = worker.gh.rest
        def fail_label(method, path, *args, **kwargs):
            if method == "POST" and path.endswith("/labels"):
                raise fixtures.station.GitHubError("unavailable", status=503)
            return real_rest(method, path, *args, **kwargs)
        with mock.patch.object(worker.gh, "rest", side_effect=fail_label):
            self.assertTrue(worker._brief_holds(REPO, issue, []))
        self.assertNotIn({"name": "blocked"}, issue["labels"])
        self.assertTrue(worker._brief_holds(REPO, issue, []))
        self.assertEqual(fake.writes, [
            ("POST", "/repos/%s/issues/206/labels" % REPO, {"labels": ["blocked"]})])

    def test_negative_or_placeholder_blockers_do_not_silently_park_stale_tickets(self):
        for blocker in ("None at this time", "No blockers known", "Nothing blocks this ticket",
                        "TBD", "To be determined", "Unknown"):
            with self.subTest(blocker=blocker):
                held, fake = self.check("## Blocked on\n%s\n" % blocker)
                self.assertTrue(held)
                self.assertNotIn(("POST", "/repos/%s/issues/206/labels" % REPO,
                                  {"labels": ["blocked"]}), fake.writes)
                self.assertIn(("POST", "/repos/%s/issues/206/labels" % REPO,
                               {"labels": ["needs:human"]}), fake.writes)
