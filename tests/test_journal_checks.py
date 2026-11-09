"""Journal checks flag missing work evidence without fabricating progress."""
from __future__ import annotations

import unittest

from bus import handoff, journal
from bus.github import GitHub
from test_journal_in_place import Fake, REPO, comment, tick


class JournalChecks(unittest.TestCase):
    def publish(self, lines, previous=None, holder="muse", login="me",
                created="2030-01-01T09:00:05Z", at="2030-01-01T10:00:00Z", steps=None):
        old = [comment(41, previous, created, login)] if previous else []
        fake = Fake(old)
        journal.post(GitHub("test-placeholder", transport=fake), REPO, 7,
                     tick(at, holder, *lines), steps=steps)
        return fake.writes()[-1][2]["body"]

    def test_missing_counted_work_is_flagged_by_the_real_writer(self):
        body = self.publish(["- Waiting: issue 7 remains blocked"])
        self.assertIn("- Journal check: missing counted moved line", body)
        self.assertNotIn("merged 0", body)

    def test_repeated_stuck_list_without_action_is_flagged_by_the_real_writer(self):
        lines = ["- Waiting: issue 7 remains blocked", "- Moved: merged 0, briefed 0"]
        old = tick("2030-01-01T09:00:00Z", "muse", *lines)
        body = self.publish(lines, old)
        self.assertIn("- Journal check: repeated unchanged stuck list without action", body)

    def current(self, body):
        return body.split(handoff.EARLIER_OPEN)[0]

    def test_counted_work_and_plain_counted_footer_are_preserved(self):
        for line in ("- Moved: merged 1, briefed 0", "merged 1, briefed 2",
                     "- Merged: 1", "- Moved: 1, with issue 7 merged"):
            with self.subTest(line=line):
                body = self.publish([line])
                self.assertIn(line, body)
                self.assertNotIn("Journal check:", body)

    def test_a_changed_waiting_state_is_not_a_repeat(self):
        old = tick("2030-01-01T09:00:00Z", "muse", "- Waiting: issue 7 blocked")
        body = self.publish(["- Waiting: issue 7 resolved; issue 8 blocked", "- Moved: merged 0"], old)
        self.assertNotIn("Journal check:", self.current(body))

    def test_positive_counted_action_does_not_flag_repeated_waiting(self):
        lines = ["- Waiting: issue 7 blocked", "- Moved: escalated 1"]
        old = tick("2030-01-01T09:00:00Z", "muse", *lines)
        self.assertNotIn("Journal check:", self.publish(lines, old))

    def test_explicit_standdown_or_no_work_has_a_reason(self):
        for reason in ("- Standdown: another holder owns the lock",
                       "No work: queue is empty", "Nothing moved: waiting for review"):
            with self.subTest(reason=reason):
                body = self.publish([reason])
                self.assertIn(reason, body)
                self.assertNotIn("Journal check:", body)
        self.assertIn("missing counted moved line", self.publish(["- Standdown: none"]))

    def test_another_holder_or_author_does_not_supply_previous_tick(self):
        lines = ["- Waiting: issue 7 blocked", "- Moved: merged 0"]
        for holder, login in (("other", "me"), ("muse", "other")):
            with self.subTest(holder=holder, login=login):
                old = tick("2030-01-01T09:00:00Z", holder, *lines)
                self.assertNotIn("Journal check:", self.publish(lines, old, login=login))

    def test_previous_day_is_compared_but_never_edited_into_today(self):
        lines = ["- Waiting: issue 7 blocked", "- Moved: merged 0"]
        old = tick("2030-01-01T23:50:00Z", "muse", *lines)
        body = self.publish(lines, old, created="2030-01-01T23:50:05Z", at="2030-01-02T00:10:00Z")
        self.assertIn("repeated unchanged stuck list", body)
        self.assertNotIn(handoff.EARLIER_OPEN, body)

    def test_only_immediately_previous_tick_counts_not_any_historical_match(self):
        lines = ["- Waiting: issue 7 blocked", "- Moved: merged 0"]
        oldest = tick("2030-01-01T08:00:00Z", "muse", *lines)
        latest = tick("2030-01-01T09:00:00Z", "muse", "- Waiting: issue 8 blocked")
        body = self.publish(lines, journal.merged(latest, oldest))
        self.assertNotIn("Journal check:", self.current(body))

    def test_future_and_equal_timestamp_ticks_are_not_previous_evidence(self):
        lines = ["- Waiting: issue 7 blocked", "- Moved: merged 0"]
        for at in ("2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z"):
            with self.subTest(at=at):
                body = self.publish(lines, tick(at, "muse", *lines))
                self.assertNotIn("Journal check:", self.current(body))

    def test_empty_waiting_lists_are_not_stuck(self):
        for waiting in ("none", "nothing", ""):
            lines = ["- Waiting: " + waiting, "- Moved: merged 0"]
            old = tick("2030-01-01T09:00:00Z", "muse", *lines)
            self.assertNotIn("Journal check:", self.publish(lines, old))

    def test_quoted_and_fenced_counts_do_not_supply_evidence(self):
        body = self.publish(["> - Moved: merged 2", "```text", "- Moved: merged 3", "```"])
        self.assertIn("missing counted moved line", body)

    def test_replay_recomputes_warning_once_and_keeps_steps_last(self):
        body = self.publish(["- Journal check: missing counted moved line"], steps="- Steps: journal 1s")
        self.assertEqual(body.count("- Journal check:"), 1)
        self.assertNotIn("Steps:", body)
        corrected = self.publish(["- Journal check: missing counted moved line", "- Moved: merged 1"])
        self.assertNotIn("Journal check:", corrected)

    def test_unmarked_comments_still_do_not_count_as_peer_journals(self):
        comments = [{"body": "Brain tick: merged 1", "login": "me", "at": "2030-01-01T09:00:00Z"}]
        self.assertEqual(handoff.journals(comments, ["me"]), [])

    def test_indented_and_long_fences_cannot_supply_counts(self):
        for lines in (["   ```text", "- Moved: merged 2", "   ```"],
                      ["````text", "```", "- Moved: merged 2", "````"],
                      ["~~~", "- Moved: merged 2", "~~~"]):
            with self.subTest(lines=lines):
                self.assertIn("missing counted moved line", self.publish(lines))

    def test_a_warning_example_inside_a_fence_is_not_removed(self):
        lines = ["- Moved: merged 1", "```text", "- Journal check: sample text", "```"]
        body = self.publish(lines)
        self.assertIn("\n".join(lines), body)

    def test_indented_stuck_items_are_compared_without_list_order(self):
        old = tick("2030-01-01T09:00:00Z", "muse", "- Waiting:",
                   "  - issue 7 blocked", "  - issue 8 blocked", "- Moved: merged 0")
        body = self.publish(["- Waiting:", "  - issue 8 blocked", "  - issue 7 blocked",
                             "- Moved: merged 0"], old)
        self.assertIn("repeated unchanged stuck list without action", self.current(body))

    def test_malformed_or_unbounded_counts_are_flagged_not_raised(self):
        for value in ("-1", "1.5", "9" * 5000):
            with self.subTest(value=value[:10]):
                body = self.publish(["- Moved: " + value])
                self.assertIn("missing counted moved line", body)

    def test_repeated_deferral_is_counted_but_not_positive_action(self):
        lines = ["- Waiting: issue 7 remains blocked",
                 "- Moved: merged 0, briefed 0, deferred 3"]
        old = tick("2030-01-01T09:00:00Z", "muse", *lines)
        body = self.current(self.publish(lines, old))
        self.assertIn("- Journal check: repeated unchanged stuck list without action", body)
        self.assertNotIn("missing counted moved line", body)
