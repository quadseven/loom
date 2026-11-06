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


if __name__ == "__main__":
    unittest.main()
