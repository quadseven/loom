"""The station reads an answer given on the dashboard (loom:answer comment,
loom#1777) as the operator's: the stale-brief check and the brief's notes
read it as the operator's newest comment, and a Merge or Hold on a lane pull
request held for the operator is applied through the existing merge path and
its gates, and written to the shadow ledger as the operator's verdict."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bus import answer, brain, brief_check, merge_tier, operator_hold, shadow, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402
from test_bus_factory import Base, ME, REPO, iso  # noqa: E402
from test_operator_hold import BRAIN_REPO, PLAIN_BODY, PR_BODY, Route, brain_routes  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

OP = ME["login"]  # the factory tests' one trusted login, a person: the operator
BOT = "loom-app[bot]"


def given(choice, note="", login=OP, user=ME, at=0):
    """An answer comment the dashboard posted, as the factory reads it."""
    return {"id": 7000 + at, "body": answer.comment_body(login, choice, note), "user": user,
            "created_at": iso(at)}


def ledger():
    return shadow.read(os.environ["LOOM_SHADOW_LEDGER"])


class StaleBrief(unittest.TestCase):
    def test_a_dashboard_answer_is_the_operators_newest_comment(self):
        comments = [{"id": 1, "body": "Still needed, keep going.", "user": {"login": "operator-one"},
                     "created_at": "2030-01-01T09:00:00Z"},
                    {"id": 2, "body": answer.comment_body("operator-one", "Close", "already done in #3"),
                     "user": {"login": BOT, "type": "Bot"}, "created_at": "2030-01-01T10:00:00Z"}]
        found = brief_check.check(None, REPO, {"number": 5, "body": "## Why\nx\n"}, comments,
                                  ["operator-one", BOT], "main")
        self.assertIsNotNone(found)
        self.assertEqual(found.kind, brief_check.STALE)
        self.assertIn("already done in #3", found.reason)

    def test_an_answer_naming_someone_who_is_no_operator_is_not_the_operators(self):
        comments = [{"id": 2, "body": answer.comment_body("stranger", "Close", "already done in #3"),
                     "user": {"login": BOT, "type": "Bot"}, "created_at": "2030-01-01T10:00:00Z"}]
        self.assertIsNone(brief_check.operator_done(
            answer.as_operator_comments(comments, ["operator-one", BOT], ["operator-one"], "operator-one"),
            "operator-one"))

    def test_the_brief_notes_carry_the_answer(self):
        comments = [{"body": answer.comment_body("operator-one", "b", "the parameter store"),
                     "user": {"login": BOT}, "created_at": "2030-01-01T10:00:00Z"}]
        notes = tickets.operator_notes(comments, ["operator-one", BOT], ["operator-one"])
        self.assertEqual(notes, [("operator-one", "2030-01-01T10:00:00Z", "Answer: b. the parameter store")])


class FactoryVerdict(Base):
    """A lane pull request held for the operator (PR_BODY declares steps)."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patch = mock.patch.dict(os.environ, {"LOOM_SHADOW_LEDGER": os.path.join(folder.name, "ledger.jsonl")})
        patch.start()
        self.addCleanup(patch.stop)

    def held(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, body=PR_BODY)
        self.go()
        self.assertEqual(len(self.humans), 1)
        self.assertEqual(self.merged, [])

    def answer(self, choice):
        # As the dashboard leaves it: the answer posted, loom:human off and
        # loom:answer-pending on.
        self.gh.comments[5].append(given(choice, "steps are done"))
        labels = [x for x in self.gh.issues[5]["labels"] if x["name"] != "loom:human"]
        self.gh.issues[5]["labels"] = labels + [{"name": answer.ANSWERED_LABEL}]

    def labels(self):
        return [x["name"] for x in self.gh.issues[5]["labels"]]

    def test_merge_lands_it_through_the_factory_merge_path(self):
        self.held()
        self.answer("Merge")
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual(len(self.humans), 1)  # no new ask
        self.assertNotIn(answer.ANSWERED_LABEL, self.labels())
        verdicts = [e for e in ledger() if e["action"] == shadow.VERDICT and e["number"] == 50]
        self.assertEqual([e["label"] for e in verdicts], ["merge"])
        self.assertEqual(verdicts[0]["sha"], "sha50")

    def test_merge_lifts_only_the_hold_so_failing_checks_still_keep_it_open(self):
        self.held()
        self.answer("Merge")
        self.gh.details[50]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"
        self.go()
        self.assertEqual(self.merged, [])

    def test_hold_keeps_it_held_with_no_new_ask(self):
        self.held()
        self.answer("Hold")
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual(len(self.humans), 1)
        self.assertIn("loom:human", self.labels())
        self.assertNotIn(answer.ANSWERED_LABEL, self.labels())
        self.go()
        self.assertEqual((self.merged, len(self.humans)), ([], 1))
        verdicts = [e for e in ledger() if e["action"] == shadow.VERDICT and e["number"] == 50]
        self.assertEqual([e["label"] for e in verdicts], ["hold"])

    def test_an_answer_by_someone_who_is_no_operator_changes_nothing(self):
        self.held()
        self.gh.comments[5].append(given("Merge", login="stranger"))
        self.gh.issues[5]["labels"] = [x for x in self.gh.issues[5]["labels"] if x["name"] != "loom:human"]
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual(len(self.humans), 2)  # held again, asked again


class BrainVerdict(unittest.TestCase):
    """bus.brain merge, the merge path the factory calls, reads the same
    answer from the ticket's comments."""

    def merge(self, *thread, committed="2030-01-01T08:00:00Z"):
        routes = brain_routes(PR_BODY, ["bus/a.py"], {})
        routes["/graphql"]["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "committedDate"] = committed
        routes["/repos/%s/issues/9/comments" % BRAIN_REPO] = list(thread)
        fake = Route(routes)
        env = {"LOOM_MERGE_SETTLE_SECONDS": "0", "LOOM_AUTO_MERGE_TIERS": ",".join(merge_tier.TIERS),
               "LOOM_BRIEF_AUTHORS": "%s operator-one" % BOT}
        try:
            brain.merge(GitHub("t", transport=fake), BRAIN_REPO, 8, (BRAIN_REPO, 9), env=env)
        except ValueError as err:
            return fake, str(err)
        return fake, ""

    def ask(self):
        return {"id": 1, "body": brain.HUMAN_MARKER + "\n" + operator_hold.ask(8, ["steps"]) + "\nKind: decision",
                "user": {"login": BOT}, "created_at": "2030-01-01T09:00:00Z"}

    def said(self, choice, at="2030-01-01T10:00:00Z"):
        return {"id": 2, "body": answer.comment_body("operator-one", choice), "user": {"login": BOT},
                "created_at": at}

    def merged(self, fake):
        return any(path.endswith("/pulls/8/merge") for _, path, _ in fake.writes)

    def asked(self, fake):
        return [b for m, p, b in fake.writes if p.endswith("/issues/9/comments")]

    def test_merge_lifts_the_operator_hold(self):
        fake, refused = self.merge(self.ask(), self.said("Merge"))
        self.assertEqual(refused, "")
        self.assertTrue(self.merged(fake))
        self.assertEqual(self.asked(fake), [])

    def test_hold_refuses_without_a_new_ask(self):
        fake, refused = self.merge(self.ask(), self.said("Hold"))
        self.assertIn("operator-one answered hold", refused)
        self.assertFalse(self.merged(fake))
        self.assertEqual(self.asked(fake), [])

    def test_a_merge_given_before_the_head_commit_does_not_count(self):
        fake, refused = self.merge(self.ask(), self.said("Merge"), committed="2030-01-01T11:00:00Z")
        self.assertIn("held for the operator", refused)
        self.assertFalse(self.merged(fake))

    def test_with_no_answer_the_hold_asks_as_before(self):
        fake, refused = self.merge()
        self.assertIn("held for the operator", refused)
        self.assertEqual(len(self.asked(fake)), 1)

    def test_a_plain_pull_request_merges_without_reading_the_ticket_comments(self):
        fake = Route(brain_routes(PLAIN_BODY, ["bus/a.py"], {}))
        reads = []

        def spy(method, url, headers, body):
            reads.append(url)
            return fake(method, url, headers, body)
        brain.merge(GitHub("t", transport=spy), BRAIN_REPO, 8, (BRAIN_REPO, 9),
                    env={"LOOM_MERGE_SETTLE_SECONDS": "0", "LOOM_AUTO_MERGE_TIERS": ",".join(merge_tier.TIERS)})
        self.assertTrue(self.merged(fake))
        self.assertFalse(any("/issues/9/comments" in url for url in reads))


if __name__ == "__main__":
    unittest.main()
