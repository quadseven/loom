"""A person's session claims an issue by assignment or comment (bus/human_claims.py).

A second operator's session claims an issue by assigning its login and
commenting a fixed phrase. No lane of this Loom may take such an issue, and
no reaper may unassign the login or remove a claim from it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import claim_referee, human_claims, stale_claims, station, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests.test_release_stale_claims import Fake, at, issue, labeled, REPO  # noqa: E402

LOGIN = "ccqw"
# The claim comment as the second operator's session writes it.
PHRASE = "Claimed by ccqw's Claude Code"
MARKER = "<!-- ccqw:claim v1 -->"
DAY = 24 * 3600


def claim_comment(by=LOGIN, body=None, ago=3 * DAY, timeline=True):
    found = {"user": {"login": by}, "body": body if body is not None else PHRASE + ".",
             "created_at": at(ago)}
    if timeline:
        found.update(event="commented", actor={"login": by})
    return found


def held(body, claims=None, by=LOGIN):
    return human_claims.commented([claim_comment(by=by, body=body)], claims)


class Config(unittest.TestCase):
    def test_the_default_needs_no_setting(self):
        for env in ({}, {human_claims.ENV: " "}):
            self.assertEqual([LOGIN], list(human_claims.configured(env)))
            self.assertEqual(LOGIN, held(PHRASE, human_claims.configured(env)))

    def test_the_setting_names_several_claims_or_turns_them_off(self):
        claims = human_claims.parse("A-Op: Mine  now | taken by *! ;b-op:taken\nbroken")
        self.assertEqual(["a-op", "b-op"], sorted(claims))
        self.assertEqual("a-op", held("MINE now", claims, by="a-op"))
        self.assertEqual("a-op", held("Taken by anyone!", claims, by="a-op"))
        self.assertIsNone(held("taken", claims, by="a-op"))
        self.assertEqual({}, human_claims.parse("off"))


class DefaultForms(unittest.TestCase):
    """The default matches each form the live claim comments take."""

    def test_the_hidden_marker_alone_holds_the_issue(self):
        self.assertEqual(LOGIN, held(MARKER + "\nClaiming for triage (rewrite, labels)."))

    def test_claimed_by_anyones_claude_code_holds_the_issue(self):
        for body in (PHRASE, MARKER + "\n" + PHRASE + ".", "claimed BY Someone Else's claude code",
                     "Claimed by ccqw\u2019s Claude Code"):
            with self.subTest(body=body):
                self.assertEqual(LOGIN, held(body))

    def test_other_words_hold_nothing(self):
        for body in ("Claimed by ccqw", "Claude Code is great", "<!-- other:claim v1 -->",
                     "Claimed by a person, not Claude Code"):
            with self.subTest(body=body):
                self.assertIsNone(held(body))


class Holder(unittest.TestCase):
    def test_an_assignment_to_the_claim_login_holds_the_issue(self):
        self.assertEqual(LOGIN, human_claims.holder(issue(1, labels=(), assignees=("CCQW",))))
        self.assertIsNone(human_claims.holder(issue(1, labels=(), assignees=("someone",))))

    def test_the_claim_comment_holds_the_issue_whatever_its_case(self):
        said = claim_comment(body="Working it.\n" + PHRASE.upper().replace(" ", "\n"))
        self.assertEqual(LOGIN, human_claims.holder(issue(1, labels=()), [said]))
        said = claim_comment(body="Claimed by ccqw's  claude code: on it", timeline=False)
        self.assertEqual(LOGIN, human_claims.holder(issue(1, labels=()), [said]))

    def test_the_phrase_from_anyone_else_holds_nothing(self):
        said = claim_comment(by="stranger")
        self.assertIsNone(human_claims.holder(issue(1, labels=()), [said]))

    def test_the_reader_reads_comments_once_and_only_for_an_issue_that_has_some(self):
        reads = []

        def read(repo, number):
            reads.append(number)
            return [claim_comment(timeline=False)]
        reader = human_claims.Reader(read)
        quiet = dict(issue(1, labels=()), comments=0)
        talked = dict(issue(2, labels=()), comments=3, updated_at="x")
        self.assertIsNone(reader.holder(REPO, quiet))
        self.assertEqual(LOGIN, reader.holder(REPO, talked))
        self.assertEqual(LOGIN, reader.holder(REPO, talked))
        self.assertEqual([2], reads)


class Ownership(unittest.TestCase):
    def test_a_claim_login_assignment_owns_even_its_own_loom_ticket(self):
        # A trusted briefer's own loom:ticket is otherwise a hand-off to this
        # Loom whoever it is assigned to among them.
        found = dict(issue(1, labels=("loom:ticket",), assignees=(LOGIN,)), user={"login": LOGIN})
        self.assertEqual(LOGIN, tickets.owned_elsewhere(found, ["me"], [LOGIN]))


class Reapers(unittest.TestCase):
    """No reaper unassigns the claim login or removes a claim it holds."""

    def test_the_stale_claim_release_never_takes_a_claim_login_assignment(self):
        found = issue(1, labels=(), assignees=(LOGIN,))
        self.assertEqual({}, stale_claims.claimers(found, ["me"], [LOGIN]))

    def test_a_claim_comment_keeps_a_claim_label_from_going_stale(self):
        found = issue(1, labels=("claim:" + LOGIN,))
        line = [labeled("claim:" + LOGIN, LOGIN, 5 * DAY), claim_comment(ago=4 * DAY)]
        self.assertFalse(stale_claims.stale(found, line, LOGIN, "claim:" + LOGIN,
                                            time.time(), DAY))

    def test_the_referee_leaves_every_claim_on_a_held_issue(self):
        engines = claim_referee.ENGINE_CLAIMS
        both = issue(1, labels=("claim:claude-code", "claim:" + LOGIN, "loom:briefed"),
                     assignees=(LOGIN,))
        line = [labeled("claim:claude-code", "agent[bot]", 3 * DAY),
                labeled("claim:" + LOGIN, LOGIN, 2 * DAY)]
        self.assertEqual([], claim_referee.plan(both, line, time.time(), engines, [], []))
        idle = issue(2, labels=("claim:claude-code", "loom:briefed"))
        line = [labeled("claim:claude-code", "agent[bot]", 3 * DAY), claim_comment(ago=2 * DAY)]
        self.assertEqual([], claim_referee.plan(idle, line, time.time(), engines, [], []))

    def test_the_release_step_writes_nothing_on_a_held_issue(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = Fake()
        fake.issues[1] = issue(1, labels=(), assignees=(LOGIN,))
        fake.timelines[1] = [claim_comment(ago=3 * DAY)]
        settings = station.Settings(os.path.join(tmp.name, "loom"), [REPO], ["me"],
                                    ["example-owner"], briefers=[LOGIN], claim_idle_hours=24)
        with mock.patch("bus.station._say"):
            station.Station(GitHub("t", transport=fake), settings,
                            now=time.time()).release_stale_claims()
        self.assertEqual([], fake.writes)


if __name__ == "__main__":
    unittest.main()
