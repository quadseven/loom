"""A trusted briefer's tickets are built; their claims stay theirs (loom#687).

LOOM_TRUSTED_BRIEFERS names logins whose loom:ticket briefs this Loom
builds exactly like a LOOM_BRIEF_AUTHORS login's. Ownership (assignment and
claim:<login> labels), journals, proofs, adoption and merges stay
LOOM_BRIEF_AUTHORS only.
"""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from bus import brain, rewrite, station, tickets
from bus.github import GitHub
from tests.test_bus_station import (AUTHORS, OWNERS, READY_BODY, REPO, Base, FakeGitHub,
                                    labeled)

BRIEFER = "dana-example"
ENV = {"LOOM_BRIEF_AUTHORS": " ".join(AUTHORS), "LOOM_TRUSTED_BRIEFERS": BRIEFER}


def by_briefer(fake, number, state="briefed", lane="codex"):
    """A loom:ticket the trusted briefer authored and labelled."""
    fake.add(number, author=BRIEFER, state=state, lane=lane, body=READY_BODY,
             events=[labeled("loom:" + state, BRIEFER), labeled("lane:" + lane, BRIEFER)])


class Pass(Base):
    def setUp(self):
        super().setUp()
        self.settings.briefers = [BRIEFER]
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_quiet(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_pass()
        return err.getvalue()


class TrustedBriefIsBuiltTest(Pass):
    """Acceptance box 1: the briefer's loom:ticket is claimed and briefed."""

    def test_a_trusted_briefers_ticket_is_claimed_for_the_lane(self):
        by_briefer(self.fake, 12)
        self.run_quiet()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])
        self.assertIn("loom:running", self.fake.names(12))
        # The claim is this Loom's: labelled for its operator. No assignment
        # (an assignee is emailed for every comment on the ticket).
        self.assertEqual(tickets.assignees(self.fake.issues[12]), [])
        self.assertIn("claim:alice-example", self.fake.names(12))

    def test_a_trusted_briefers_ticket_they_assigned_themselves_is_a_hand_off(self):
        by_briefer(self.fake, 12)
        self.fake.issues[12]["assignees"] = [{"login": BRIEFER}]
        self.fake.issues[12]["labels"].append({"name": "claim:" + BRIEFER})
        self.run_quiet()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])

    def test_a_trusted_briefers_queued_ticket_gets_a_lane(self):
        self.settings.factory_on = True
        by_briefer(self.fake, 14, state="queued")
        self.run_quiet()
        self.assertNotIn("loom:queued", self.fake.names(14))

    def test_without_the_setting_the_same_ticket_is_refused(self):
        self.settings.briefers = []
        by_briefer(self.fake, 12)
        err = self.run_quiet()
        self.assertEqual(self.listing(), [])
        self.assertIn("not claiming %s#12" % REPO, err)

    def test_settings_read_the_briefers_apart_from_the_authors(self):
        settings = station.Settings.from_env(dict(ENV, LOOM_HOME=self.home))
        self.assertEqual(settings.authors, AUTHORS)
        self.assertEqual(settings.briefers, [BRIEFER])
        self.assertEqual(station.Settings.from_env({"LOOM_HOME": self.home}).briefers, [])

    def test_prioritize_trusts_the_briefers_ticket(self):
        issue = {"state": "open", "labels": [{"name": "loom:ticket"}],
                 "user": {"login": BRIEFER}, "body": "Priority: P2\n\n## Why\nx\n"}
        self.assertIn("Priority: P1", brain._prioritize_body(issue, "P1", ENV))
        with self.assertRaisesRegex(ValueError, "not a trusted login"):
            brain._prioritize_body(issue, "P1", {"LOOM_BRIEF_AUTHORS": " ".join(AUTHORS)})

    def test_one_function_decides_brief_trust(self):
        ticket = {"user": {"login": BRIEFER}, "labels": [{"name": "loom:ticket"}]}
        self.assertTrue(tickets.brief_author_trusted(ticket, AUTHORS, [BRIEFER]))
        self.assertFalse(tickets.brief_author_trusted(ticket, AUTHORS, []))
        # A briefer's issue that is not a loom:ticket is not a brief.
        self.assertFalse(tickets.brief_author_trusted(
            {"user": {"login": BRIEFER}, "labels": []}, AUTHORS, [BRIEFER]))
        self.assertTrue(tickets.brief_author_trusted(
            {"user": {"login": "Alice-Example"}, "labels": []}, AUTHORS, [BRIEFER]))
        self.assertEqual(tickets.briefing_logins(["a", "B"], ["b", "c"]), ["a", "b", "c"])


class TrustedBriefersClaimsStayTheirsTest(Pass):
    """Acceptance box 2: what the briefer merely owns is still skipped, and
    their journals and proofs are not trusted."""

    def test_a_ticket_assigned_to_the_briefer_that_they_did_not_author_is_skipped(self):
        by_briefer(self.fake, 13)
        self.fake.add(12)  # authored by one of this Loom's own logins
        self.fake.issues[12]["assignees"] = [{"login": BRIEFER}]
        err = self.run_quiet()
        # The briefer's own ticket is the hand-off; the one they only own is not.
        self.assertEqual(self.listing(), ["example-owner.example-repo-13.md"])
        self.assertEqual(self.fake.names(12), ["loom:ticket", "loom:briefed", "lane:codex"])
        self.assertIn("leaving %s#12 alone: assigned to %s" % (REPO, BRIEFER), err)

    def test_a_queued_ticket_claim_labelled_for_the_briefer_gets_no_lane(self):
        self.settings.factory_on = True
        self.fake.add(14, state="queued")
        self.fake.issues[14]["labels"].append({"name": "claim:" + BRIEFER})
        by_briefer(self.fake, 13)
        self.run_quiet()
        self.assertIn("loom:queued", self.fake.names(14))
        self.assertEqual(self.listing(), ["example-owner.example-repo-13.md"])

    def test_ownership_without_loom_ticket_stays_the_briefers(self):
        assigned = {"user": {"login": BRIEFER}, "labels": [],
                    "assignees": [{"login": BRIEFER}]}
        labelled = {"user": {"login": BRIEFER}, "labels": [{"name": "claim:" + BRIEFER}]}
        for issue in (assigned, labelled):
            self.assertEqual(tickets.owned_elsewhere(issue, AUTHORS, [BRIEFER]), BRIEFER)
            self.assertEqual(brain._triage_skip(issue, AUTHORS, [BRIEFER]),
                             "another operator's")
        # A briefer's loom:ticket that a third login owns is still that login's.
        third = {"user": {"login": BRIEFER}, "labels": [{"name": "loom:ticket"}],
                 "assignees": [{"login": "carol-example"}]}
        self.assertEqual(tickets.owned_elsewhere(third, AUTHORS, [BRIEFER]), "carol-example")
        # A pull request is never a brief, so the hand-off rule never covers one.
        pull = {"user": {"login": BRIEFER}, "labels": [{"name": "loom:ticket"}],
                "assignees": [{"login": BRIEFER}], "pull_request": {}}
        self.assertEqual(tickets.owned_elsewhere(pull, AUTHORS, [BRIEFER]), BRIEFER)
        # The briefer's own loom:ticket is the hand-off, the same function says.
        mine = {"user": {"login": BRIEFER}, "labels": [{"name": "loom:ticket"}],
                "assignees": [{"login": BRIEFER}]}
        self.assertIsNone(tickets.owned_elsewhere(mine, AUTHORS, [BRIEFER]))

    def test_triage_and_rewrite_skip_the_briefers_claims(self):
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.add_open_issue(100, body=READY_BODY)
        self.fake.add_open_issue(101, body="It is slow.")
        self.fake.open_issues[0]["assignees"] = [{"login": BRIEFER}]
        self.fake.open_issues[1]["labels"] = [{"name": "claim:" + BRIEFER}]
        self.fake.issues[101]["labels"] = [{"name": "claim:" + BRIEFER}]
        self.run_quiet()
        self.assertEqual(self.fake.names(100), [])
        self.assertEqual(self.fake.names(101), ["claim:" + BRIEFER])
        self.assertEqual(self.fake.comments[101], [])
        gh = mock.Mock()
        gh.paginate.return_value = [
            {"number": 1, "user": {"login": BRIEFER}, "labels": [{"name": "claim:" + BRIEFER}]},
            {"number": 2, "user": {"login": BRIEFER}, "labels": [{"name": "loom:triage"}]}]
        self.assertEqual([i["number"] for i in rewrite.candidates(gh, REPO)], [2])

    def test_adoption_skips_a_pull_request_assigned_to_the_briefer(self):
        self.fake.add_goal(30)
        self.fake.comments.setdefault(99, [])
        self.fake.pulls.append({
            "number": 99, "state": "open", "title": "Tidy the docs",
            "user": {"login": "alice-example"}, "body": "", "draft": False,
            "created_at": "2030-01-01T00:00:00Z", "assignees": [{"login": BRIEFER}],
            "head": {"ref": "fix/stray-files", "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}}})
        self.run_quiet()
        self.assertEqual([c for c in self.fake.created
                          if c["title"].startswith("Land or supersede #")], [])

    def test_move_refuses_a_ticket_the_briefer_owns_but_did_not_author(self):
        fake = FakeGitHub()
        fake.add(12, state="queued")
        fake.issues[12]["assignees"] = [{"login": BRIEFER}]
        by_briefer(fake, 13, state="queued")
        fake.issues[13]["assignees"] = [{"login": BRIEFER}]
        gh = GitHub("token", transport=fake)
        with self.assertRaisesRegex(ValueError, "assigned to %s" % BRIEFER):
            brain.move(gh, REPO, 12, "briefed", lane="codex")
        self.assertIn("loom:queued", fake.names(12))
        brain.move(gh, REPO, 13, "briefed", lane="codex")
        self.assertIn("loom:briefed", fake.names(13))

    def test_a_proof_from_the_briefer_is_not_trusted(self):
        body = "## Acceptance criteria\n- [x] It works.\n"
        record = {"criteria": [{"text": "It works.", "pass": True, "evidence": "seen"}]}
        fake = mock.Mock()
        fake.paginate.return_value = [
            {"user": {"login": BRIEFER},
             "body": "<!-- loom:proof v1 -->\n```json\n%s\n```" % record}]
        with self.assertRaisesRegex(ValueError, "no loom:proof comment"):
            brain._check_proven(fake, REPO, 12, {"body": body}, ENV)

    def test_journals_and_proofs_read_only_the_authors(self):
        # Every journal, proof, audit and adoption check reads these lists;
        # neither holds the briefer.
        self.assertEqual(brain.trusted_authors(ENV), AUTHORS)
        self.assertEqual(station.Settings.from_env(dict(ENV, LOOM_HOME=self.home)).authors,
                         AUTHORS)
        self.assertEqual(station.Settings(self.home, [REPO], AUTHORS, OWNERS).briefers, [])


if __name__ == "__main__":
    unittest.main()
