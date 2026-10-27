"""The brain may not cite a person's decision that no person recorded (loom#2089).

On 2026-10-27 the brain closed a lane pull request "per the operator decision
recorded on" a ticket whose only person-facing comment was an unanswered
loom:human ask. These tests hold the guard in the brain's write path.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import answer, brain, decision_claim, journal  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
OTHER = "example-owner/other-repo"
ENV = {"LOOM_BRIEF_AUTHORS": "example-brain[bot] example-op"}
PERSON = {"login": "example-op", "type": "User"}
APP = {"login": "example-brain[bot]", "type": "Bot"}
LANE_APP = {"login": "example-lanes[bot]", "type": "Bot"}
ASK = {"user": PERSON, "created_at": "2030-01-01T13:02:00Z",
       "body": "%s\nWaiting on the operator. Ask: merge it at the bar or close it as "
               "superseded.\nKind: decision" % brain.HUMAN_MARKER}
CLAIM = "Closing as superseded, per the operator decision recorded on the ask."
OWN = "Closing as superseded on the brain's own judgment: the ask had no reply."


def comment(user, body, when="2030-01-01T20:00:00Z"):
    return {"user": user, "body": body, "created_at": when}


class Fake:
    """Answers by path: an issue's comments and events, the issue itself, and
    an empty reply to every write. Every call is recorded."""

    def __init__(self, comments=None, events=None, issue=None):
        self.comments = comments or {}
        self.events = events or {}
        self.issue = issue or {"state": "open", "body": "", "labels": [{"name": "loom:ticket"}]}
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1].split("?")[0]
        self.calls.append((method, path))
        data = {}
        if method == "GET":
            parts = path.split("/")
            key = ("/".join(parts[2:4]), int(parts[5])) if len(parts) > 5 else None
            if path.endswith("/comments"):
                data = self.comments.get(key, [])
            elif path.endswith("/events"):
                data = self.events.get(key, [])
            else:
                data = self.issue
        return 200, {}, json.dumps(data).encode()

    def wrote(self):
        return [c for c in self.calls if c[0] != "GET"]


def close(fake, note):
    return brain.close(GitHub("t", transport=fake), REPO, 9, "not_planned", note, env=ENV)


class CloseCitingADecision(unittest.TestCase):
    def test_a_close_citing_an_operator_decision_on_an_unanswered_ask_is_refused(self):
        fake = Fake(comments={(REPO, 9): [ASK]})
        with self.assertRaises(decision_claim.DecisionNotRecorded) as caught:
            close(fake, CLAIM)
        self.assertIn("own judgment", str(caught.exception))
        self.assertEqual(fake.wrote(), [])

    def test_the_same_close_passes_once_a_person_answered_after_the_ask(self):
        fake = Fake(comments={(REPO, 9): [ASK, comment(PERSON, "Close it as superseded.")]})
        close(fake, CLAIM)
        self.assertIn(("PATCH", "/repos/%s/issues/9" % REPO), fake.wrote())

    def test_a_person_comment_before_the_ask_does_not_count(self):
        early = comment(PERSON, "Looks fine to me.", "2030-01-01T09:00:00Z")
        fake = Fake(comments={(REPO, 9): [early, ASK]})
        with self.assertRaises(decision_claim.DecisionNotRecorded):
            close(fake, CLAIM)

    def test_a_close_note_without_a_claim_is_unaffected_and_reads_nothing_more(self):
        fake = Fake(comments={(REPO, 9): [ASK]})
        close(fake, OWN)
        self.assertIn(("PATCH", "/repos/%s/issues/9" % REPO), fake.wrote())
        self.assertNotIn(("GET", "/repos/%s/issues/9/comments" % REPO), fake.calls)
        self.assertNotIn(("GET", "/repos/%s/issues/9/events" % REPO), fake.calls)

    def test_a_bot_or_app_comment_after_the_ask_is_not_a_person(self):
        for who in (APP, LANE_APP, {"login": "example-op", "type": "Bot"}):
            with self.subTest(who=who["login"]):
                fake = Fake(comments={(REPO, 9): [ASK, comment(who, "Close it.")]})
                with self.assertRaises(decision_claim.DecisionNotRecorded):
                    close(fake, CLAIM)
                self.assertEqual(fake.wrote(), [])

    def test_a_loom_marker_comment_or_slash_command_by_a_person_login_is_not_a_decision(self):
        later_ask = dict(ASK, created_at="2030-01-01T20:00:00Z")
        for later in (comment(PERSON, "<!-- loom:journal v1 -->\nTick"),
                      comment(PERSON, "/grug improve")):
            with self.subTest(body=later["body"]):
                fake = Fake(comments={(REPO, 9): [ASK, later]})
                with self.assertRaises(decision_claim.DecisionNotRecorded):
                    close(fake, CLAIM)
        # The ask itself, even when it is the newest comment, is not a reply.
        fake = Fake(comments={(REPO, 9): [later_ask]})
        with self.assertRaises(decision_claim.DecisionNotRecorded):
            close(fake, CLAIM)

    def test_a_person_label_after_the_ask_counts_and_a_factory_label_does_not(self):
        person = {"event": "labeled", "actor": PERSON, "label": {"name": "wontfix"},
                  "created_at": "2030-01-01T20:00:00Z"}
        factory = dict(person, label={"name": "loom:human"})
        bot = dict(person, actor=APP)
        for events, refused in (([person], False), ([factory], True), ([bot], True)):
            fake = Fake(comments={(REPO, 9): [ASK]}, events={(REPO, 9): events})
            if refused:
                with self.assertRaises(decision_claim.DecisionNotRecorded):
                    close(fake, CLAIM)
            else:
                close(fake, CLAIM)

    def test_a_dashboard_answer_posted_by_the_app_counts(self):
        posted = comment(APP, answer.comment_body("example-op", "Close"))
        fake = Fake(comments={(REPO, 9): [ASK, posted]})
        close(fake, CLAIM)
        self.assertIn(("PATCH", "/repos/%s/issues/9" % REPO), fake.wrote())

    def test_a_decision_on_the_ticket_the_text_names_counts(self):
        named = "per the operator decision recorded on %s#4" % OTHER
        fake = Fake(comments={(REPO, 9): [], (OTHER, 4): [ASK, comment(PERSON, "Close it.")]})
        close(fake, named)
        fake = Fake(comments={(REPO, 9): [], (OTHER, 4): [ASK]})
        with self.assertRaises(decision_claim.DecisionNotRecorded):
            close(fake, named)


class OtherWritePaths(unittest.TestCase):
    def test_a_journal_citing_an_unrecorded_decision_is_refused(self):
        fake = Fake(comments={(REPO, 9): [], (OTHER, 4): [ASK]})
        text = "Closing PR 9 as superseded, per the operator decision recorded on %s#4." % OTHER
        with self.assertRaises(decision_claim.DecisionNotRecorded):
            journal.post(GitHub("t", transport=fake), REPO, 9, text, "muse",
                         ["example-brain[bot]"])
        self.assertEqual(fake.wrote(), [])

    def test_a_move_note_and_an_epic_close_note_are_guarded(self):
        fake = Fake(comments={(REPO, 9): [ASK]})
        with self.assertRaises(decision_claim.DecisionNotRecorded):
            brain.move(GitHub("t", transport=fake), REPO, 9, "briefed", note="example-op decided to restart it")
        with self.assertRaises(decision_claim.DecisionNotRecorded):
            brain.close_epic(GitHub("t", transport=fake), REPO, 9, "Done per the owner's decision.")
        self.assertEqual(fake.wrote(), [])


class Claims(unittest.TestCase):
    def test_phrasings_that_claim_a_person_decided(self):
        for said in ("Done by the brain per the operator decision: closed.",
                     "per example-op's decision", "Alex decided to drop it.",
                     "The operator decided.", "operator decision recorded on #5",
                     "the owner has approved it", "following the maintainer's ruling"):
            with self.subTest(said=said):
                self.assertTrue(decision_claim.claim(said))

    def test_text_that_claims_no_person_decided(self):
        for said in (OWN, "Waiting on an operator decision on #3.",
                     "The operator has not decided.", "No operator decision was recorded.",
                     "Elder approved the head.", "The brain decided to close it.",
                     "Awaiting a decision by the operator.", "codex decided to rename it",
                     "needs a decision from the operator"):
            with self.subTest(said=said):
                self.assertEqual(decision_claim.claim(said), "")

    def test_the_guarded_phrase_list_lives_in_one_place(self):
        self.assertTrue(decision_claim.CLAIM_PATTERNS)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("bus/brain.py", "bus/journal.py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                self.assertNotIn("operator decision", fh.read().lower(), name)

    def test_muse_states_the_rule(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "MUSE.md"), encoding="utf-8") as fh:
            muse = " ".join(fh.read().split()).lower()
        self.assertIn("never cite an operator decision unless a person's comment or label "
                      "records it", muse)


if __name__ == "__main__":
    unittest.main()
