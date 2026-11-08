"""Characterization tests at the bus.history interface (loom#2457).

One trusted reading of a ticket's comments: the module takes raw comments,
applies trust filtering once on the way in, and returns the ticket's
history (rung, tried rungs, fix rounds, attempts, dead lane). The expected
values below are worked examples from the marker formats the factory,
station and brain have written historically, not recomputed by the code
under test.
"""
import unittest

from bus import factory, history

TRUSTED = ["alice-example"]
AT = "2026-10-17T05:00:00Z"
LATER = "2026-10-17T06:00:00Z"


def comment(body, login="alice-example", at=AT):
    return {"body": body, "created_at": at, "user": {"login": login}}


CLAIM = comment("Claimed for lane codex (body sha256 0123456789ab)")


class ReadHistory(unittest.TestCase):
    def test_trust_is_applied_once_on_the_way_in(self):
        comments = [
            comment(history.rung_marker(2), login="stranger"),
            comment(history.rung_marker(1)),
            CLAIM,
            comment("<!-- loom:fix v1 -->\nfix round", login="stranger"),
            comment("<!-- loom:fix v1 -->\nfix round"),
        ]
        found = history.read_history(comments, TRUSTED)
        self.assertEqual(found.rung, 1)
        self.assertEqual(found.tried_rungs, {1})
        self.assertEqual(found.fixes, 1)

    def test_a_stranger_only_history_reads_as_empty(self):
        comments = [comment(history.rung_marker(3), login="stranger"),
                    comment("Claimed for lane codex (body sha256 0123456789ab)", login="stranger")]
        found = history.read_history(comments, TRUSTED)
        self.assertEqual((found.rung, found.tried_rungs, found.attempts, found.dead_lane),
                         (0, set(), [], None))

    def test_rung_zero_marker_starts_the_ladder_again(self):
        comments = [comment(history.rung_marker(1)), CLAIM,
                    comment(history.rung_marker(2)), CLAIM,
                    comment(history.rung_marker(0)), comment(history.rung_marker(2)), CLAIM]
        found = history.read_history(comments, TRUSTED)
        self.assertEqual(found.rung, 2)
        self.assertEqual(found.tried_rungs, {2})

    def test_a_rung_with_no_claim_is_not_tried(self):
        comments = [comment(history.rung_marker(1)), CLAIM, comment(history.rung_marker(2))]
        self.assertEqual(history.read_history(comments, TRUSTED).tried_rungs, {1})

    def test_attempts_carry_the_ending_that_followed_each_claim(self):
        comments = [
            comment("Claimed for lane codex (body sha256 0123456789ab)"),
            comment("Lane codex failed: [work] broke", at=LATER),
            comment("Claimed for lane codex-2 (body sha256 0123456789ab)", at=LATER),
        ]
        found = history.read_history(comments, TRUSTED)
        self.assertEqual([a[2] for a in found.attempts], ["codex", "codex-2"])
        self.assertEqual([a[3] for a in found.attempts], ["[work] broke", ""])
        self.assertIsNone(found.dead_lane)

    def test_dead_lane_is_the_one_lane_of_attempts_since_the_last_brief(self):
        bodies = [
            "The station gave this queued ticket lane spark (tier medium).",
            "Claimed for lane spark (body sha256 0123456789ab)",
            ("Not a failure (lane spark is at a limit): the factory moved this "
             "ticket to lane codex-2, in its difficulty tier."),
            "Claimed for lane codex-2 (body sha256 0123456789ab)",
            "Claimed for lane codex-2 (body sha256 0123456789ab)",
        ]
        found = history.read_history([comment(b) for b in bodies], TRUSTED)
        self.assertEqual(found.dead_lane, "codex-2")
        self.assertEqual(len(found.attempts_since_brief), 2)

    def test_equivalent_marker_is_read_and_written_by_the_same_module(self):
        self.assertEqual(history.equivalent_marker("m-1"), "<!-- loom:equivalent m-1 -->")
        comments = [comment(history.rung_marker(1)), CLAIM,
                    comment(history.equivalent_marker("m-1"))]
        self.assertEqual(history.read_history(comments, TRUSTED).tried_rungs, {1})
        self.assertEqual(history.equivalent_of([comment(history.equivalent_marker("m-1"))]), "m-1")

    def test_marker_writers_round_trip_through_the_reader(self):
        self.assertEqual(history.rung_marker(2), "<!-- loom:rung 2 -->")
        comments = [comment(history.rung_marker(2)), CLAIM]
        self.assertEqual(history.read_history(comments, TRUSTED).rung, 2)
        self.assertEqual(history.rung_of(comments), 2)

    def test_literal_historical_formats_read_the_same(self):
        # Every body below is a literal, as written by the factory, station
        # and brain over the ticket's life: no marker writer of this module
        # builds them, so a format drift in the module cannot pass here.
        comments = [
            comment("<!-- loom:rung 1 -->"),
            comment("Claimed for lane codex (body sha256 0123456789ab)"),
            comment("<!-- loom:fix v1 -->\nfix round"),
            comment("Lane codex failed: [work] broke", at=LATER),
            comment("<!-- loom:equivalent m-1 -->"),
        ]
        found = history.read_history(comments, TRUSTED)
        self.assertEqual(found.rung, 1)
        self.assertEqual(found.tried_rungs, {1})
        self.assertEqual(found.fixes, 1)
        self.assertEqual(found.equivalent, "m-1")
        self.assertEqual([a[3] for a in found.attempts], ["[work] broke"])

    def test_retry_chain_reads_station_retry_state(self):
        comments = [
            comment("Automatic retry 1 of 3 (limit): moved from lane codex to lane agy."),
            comment("Claimed for lane agy (body sha256 0123456789ab)"),
        ]
        found = history.read_history(comments, TRUSTED)
        self.assertEqual((found.retry_count, found.retry_lanes, found.pending_retry),
                         (1, {"codex"}, None))
        self.assertEqual(history.retry_chain(comments), (1, {"codex"}, None))

    def test_retry_chain_resets_on_lane_outcome(self):
        comments = [
            comment("Automatic retry 1 of 3 (transient): moved from lane codex to lane agy."),
            comment("Lane agy failed: [work] broke"),
        ]
        self.assertEqual(history.retry_chain(comments), (0, set(), None))

    def test_graphql_shaped_trusted_comments_count(self):
        comments = [{"body": "<!-- loom:rung 2 -->", "created_at": AT,
                     "author": {"login": "alice-example", "__typename": "User"}},
                    {"body": "Claimed for lane codex (body sha256 0123456789ab)",
                     "created_at": AT,
                     "author": {"login": "alice-example", "__typename": "User"}}]
        found = history.read_history(comments, TRUSTED)
        self.assertEqual((found.rung, found.tried_rungs), (2, {2}))

    def test_factory_names_delegate_to_the_same_reading(self):
        comments = [comment(history.rung_marker(1)), CLAIM,
                    comment("<!-- loom:fix v1 -->\nfix round")]
        self.assertEqual(factory.rung_of(comments), history.rung_of(comments))
        self.assertEqual(factory.tried_rungs(comments), history.tried_rungs(comments))
        self.assertEqual(factory.fixes_of(comments), history.fixes_of(comments))
        self.assertEqual(factory.attempts(comments), history.attempts(comments))
        self.assertEqual(factory.dead_lane(comments), history.dead_lane(comments))
        self.assertIs(factory.RUNG_RE, history.RUNG_RE)
        self.assertIs(factory.RUNG_MARKER, history.RUNG_MARKER)
        # The fix marker string is defined in bus/tickets.py for the brief
        # writers; the history module's copy must never drift from it.
        self.assertEqual(factory.FIX_MARKER, history.FIX_MARKER)


FIX = "<!-- loom:fix v1 -->\n\n### Check: lint\n\n    E1"
NO_CHANGE = "Lane claude made no change for the fix to #50 and said why on the pull request"
NO_REASON_FAILURE = "Lane zai failed: [work] agent made no commit and wrote no reason on loom/6-x"
SUBMODULE_FAILURE = ("Lane zai failed: [work] the change edits files inside submodule sub-example "
                     "(sub-example/src/a.c), which a push of this repo does not carry; "
                     "that repo needs the change first. nothing pushed")


class CommentReaders(unittest.TestCase):
    """The four fix-round and failure readers, called at bus.history itself
    (loom#2457), with the cases tests/test_bus_factory.py drives through the
    factory pass."""

    def test_no_change_at_reads_the_latest_unanswered_no_change_note(self):
        self.assertEqual(history.no_change_at([]), 0.0)
        noted = [comment(FIX), comment(NO_CHANGE, at=LATER)]
        self.assertEqual(history.no_change_at(noted), history._ts(LATER))
        # A newer fix round means the no-change note is no longer the latest.
        self.assertEqual(history.no_change_at(noted + [comment(FIX)]), 0.0)

    def test_two_no_change_rounds_on_one_check_count_two(self):
        rounds = [comment(b) for b in (FIX, NO_CHANGE, FIX, NO_CHANGE)]
        self.assertEqual(history.no_change_rounds(rounds), {"lint": 2})

    def test_a_round_that_changed_something_resets_the_count(self):
        rounds = [comment(b) for b in (FIX, NO_CHANGE, FIX, FIX, NO_CHANGE)]
        self.assertEqual(history.no_change_rounds(rounds), {"lint": 1})

    def test_a_check_a_later_round_does_not_name_loses_its_count(self):
        other = FIX.replace("### Check: lint", "### Check: unit")
        rounds = [comment(b) for b in (FIX, NO_CHANGE, other, NO_CHANGE)]
        self.assertEqual(history.no_change_rounds(rounds), {"unit": 1})

    def test_one_no_reason_failure_does_not_repeat(self):
        self.assertFalse(history.repeated_no_reason([comment(NO_REASON_FAILURE)]))

    def test_a_second_no_reason_failure_repeats(self):
        comments = [CLAIM, comment(NO_REASON_FAILURE), comment(history.rung_marker(1)),
                    CLAIM, comment(NO_REASON_FAILURE)]
        self.assertTrue(history.repeated_no_reason(comments))

    def test_a_rung_zero_marker_restarts_the_no_reason_count(self):
        comments = [comment(NO_REASON_FAILURE), comment(history.rung_marker(0)),
                    CLAIM, comment(NO_REASON_FAILURE)]
        self.assertFalse(history.repeated_no_reason(comments))

    def test_needs_other_repo_reads_the_newest_failure_only(self):
        self.assertIsNone(history.needs_other_repo([]))
        self.assertEqual(history.needs_other_repo([CLAIM, comment(SUBMODULE_FAILURE)]),
                         "sub-example")
        # A newer failure for another reason supersedes the submodule one.
        self.assertIsNone(history.needs_other_repo(
            [comment(SUBMODULE_FAILURE), comment(NO_REASON_FAILURE)]))

    def test_factory_names_delegate_to_the_history_readers(self):
        comments = [comment(b) for b in (FIX, NO_CHANGE, SUBMODULE_FAILURE)]
        for name in ("no_change_at", "no_change_rounds", "repeated_no_reason",
                     "needs_other_repo"):
            self.assertEqual(getattr(factory, name)(comments),
                             getattr(history, name)(comments), name)
        self.assertIs(factory.SUBMODULE_EDIT_RE, history.SUBMODULE_EDIT_RE)
        self.assertIs(factory.NO_CHANGE_RE, history.NO_CHANGE_RE)
        self.assertEqual(factory.NO_REASON, history.NO_REASON)


if __name__ == "__main__":
    unittest.main()
