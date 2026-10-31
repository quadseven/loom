"""Outside work is left alone by every path that moves a ticket (loom#2333)."""
import unittest
from unittest import mock

from bus import brain, claim_referee, claimed, labels, lane, stale_claims, tickets
from bus.tickets import owned_elsewhere

OURS = ("operator-example", "loom-brain[bot]")


def issue(*names, assignees=()):
    return {"number": 7, "state": "open", "user": {"login": "operator-example"},
            "labels": [{"name": n} for n in names],
            "assignees": [{"login": a} for a in assignees]}


class OwnershipTest(unittest.TestCase):
    def test_claim_overflow_is_a_label_every_repo_gets(self):
        self.assertIn(labels.OVERFLOW_CLAIM, labels.LABELS)

    def test_claim_overflow_holds_the_ticket_even_beside_our_own_claim(self):
        self.assertEqual(owned_elsewhere(issue("claim:overflow"), OURS), "claim:overflow")
        self.assertEqual(owned_elsewhere(issue("claim:overflow", "claim:operator-example"),
                                         OURS), "claim:overflow")

    def test_a_person_outside_this_loom_beats_our_own_claim(self):
        # The evidence: an own claim made the station read the ticket as ours,
        # and a fix round took that claim off a contributor's ticket.
        self.assertEqual(owned_elsewhere(issue("claim:operator-example",
                                               assignees=["contributor-example"]), OURS),
                         "contributor-example")

    def test_an_app_assignee_owns_nothing_beside_our_own_claim(self):
        self.assertIsNone(owned_elsewhere(issue("claim:operator-example",
                                                assignees=["other-agent[bot]"]), OURS))

    def test_our_own_operator_assigned_is_ours(self):
        self.assertIsNone(owned_elsewhere(issue(assignees=["operator-example"]), OURS))

    def test_an_unpaid_overflow_price_holds_and_a_paid_one_does_not(self):
        self.assertEqual(owned_elsewhere(issue("overflow: large"), OURS), "overflow: large")
        self.assertIsNone(owned_elsewhere(issue("overflow: large", "overflow paid: 8"), OURS))
        self.assertIsNone(owned_elsewhere(issue("overflow paid: 3"), OURS))


class NeverExpiresTest(unittest.TestCase):
    def test_it_is_not_an_engine_claim_whatever_the_setting_says(self):
        self.assertNotIn("claim:overflow", claim_referee.ENGINE_CLAIMS)
        self.assertEqual(claim_referee.engines("claim:overflow claim:muse"), ("claim:muse",))

    def test_the_referee_plans_no_removal(self):
        held = issue("claim:overflow", "loom:briefed")
        self.assertEqual(claim_referee.plan(held, [], 1e10, claim_referee.ENGINE_CLAIMS, (), ()), [])

    def test_the_release_step_never_reads_it_as_a_claimer(self):
        self.assertEqual(stale_claims.claimers(issue("claim:overflow"), ["operator-example"]), {})

    def test_no_claim_on_a_held_issue_is_released_or_lapsed(self):
        held = issue("claim:overflow", "claim:other-example", "claim:operator-example")
        self.assertEqual(stale_claims.claimers(held, ["operator-example"]), {})
        self.assertEqual(stale_claims.own_claims(held, ["operator-example"]), {})
        priced = issue("overflow: small", "claim:operator-example")
        self.assertEqual(stale_claims.own_claims(priced, ["operator-example"]), {})
        taken = issue("claim:operator-example", assignees=["contributor-example"])
        self.assertEqual(stale_claims.own_claims(taken, ["operator-example"]), {})
        ours = issue("claim:operator-example", assignees=["other-agent[bot]"])
        self.assertEqual(stale_claims.own_claims(ours, ["operator-example"]),
                         {"operator-example": "claim:operator-example"})

    def test_the_referee_removes_no_engine_claim_on_a_held_issue(self):
        held = issue("claim:overflow", "claim:muse", "claim:dot", "loom:briefed")
        self.assertEqual(claim_referee.plan(held, [], 1e10, claim_referee.ENGINE_CLAIMS, (), ()), [])

    def test_the_claimed_settle_skips_it(self):
        self.assertEqual(claimed.skip_reason(issue("claim:overflow")), "outside work")


class PathsTest(unittest.TestCase):
    ENV = {"LOOM_BRIEF_AUTHORS": " ".join(OURS)}

    def gh(self, data):
        gh = mock.Mock()
        gh.rest.return_value = mock.Mock(data=data)
        return gh

    def test_escalate_refuses_each_kind_of_outside_work(self):
        for held in (issue("loom:ticket", "loom:briefed", "claim:overflow"),
                     issue("loom:ticket", "loom:briefed", "overflow: small"),
                     issue("loom:ticket", "loom:pr", "claim:operator-example",
                           assignees=["contributor-example"])):
            gh = self.gh(held)
            with mock.patch.dict("os.environ", self.ENV):
                with self.assertRaisesRegex(ValueError, "leaves it alone"):
                    brain.escalate(gh, "o/r", 7)
            self.assertEqual([c.args[0] for c in gh.rest.call_args_list], ["GET"])

    def test_move_refuses_a_rebrief(self):
        gh = self.gh(issue("loom:ticket", "loom:pr", "claim:overflow"))
        with mock.patch.dict("os.environ", self.ENV):
            for state in ("queued", "briefed", "pr", "failed"):
                with self.assertRaisesRegex(ValueError, "leave it to their Loom"):
                    brain.move(gh, "o/r", 7, state)

    def test_remote_recovery_moves_nothing_on_a_held_ticket(self):
        held = issue("loom:running", "lane:muse", "claim:overflow")
        gh = mock.Mock()
        gh.paginate.return_value = [held]
        with mock.patch.dict("os.environ", self.ENV):
            self.assertEqual(lane._recover_and_count(gh, ["o/r"], "muse", 1e10), 0)
        gh.rest.assert_not_called()

    def test_a_remote_lane_does_not_take_it(self):
        held = dict(issue("loom:queued", "lane:muse", "claim:overflow"),
                    body="Difficulty: easy\n\n## Why\nx\n")
        free = dict(issue("loom:queued", "lane:muse"), body=held["body"])
        with mock.patch.dict("os.environ", self.ENV):
            self.assertFalse(lane._takes(held, "muse"))
            self.assertTrue(lane._takes(free, "muse"))
