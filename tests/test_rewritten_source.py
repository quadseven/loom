"""A rewritten issue closes with the ticket it became (brain.settle_source).

Before this, a loom:rewritten issue stayed open forever, so every rewritten
source counted as open work in every repo."""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import audit, brain, proof, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
ENV = {"LOOM_BRIEF_AUTHORS": "station-bot"}
CRITERIA = "## Acceptance criteria\n- [x] A test passes\n- [x] Live: the daily line shows it\n"
TICKET = 20
SOURCE = 6


def ticket_body(line="Rewritten from issue %d." % SOURCE):
    return "## Why\nwhy\n## What\nwhat\n" + CRITERIA + "## Size\nS\n\n" + line + "\n\nPart of #2\n"


def rewrite_comment(ticket=TICKET):
    return {"body": "%s\nRewritten as ticket #%d, which refs this issue." % (
        brain.TRIAGE_MARKER, ticket), "user": {"login": "station-bot"}}


def record(verified_by="codex/model-x"):
    return {"kind": "docs", "revision": "abc1234", "verified_by": verified_by,
            "observed": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"},
            "sources": ["https://example.com/logs/1"],
            "criteria": [{"criterion": "A test passes", "result": "pass", "evidence": "ci"},
                         {"criterion": "Live: the daily line shows it", "result": "pass",
                          "evidence": "seen at 2030-01-01T01:00Z"}]}


class Repo:
    """A stateful issues API for one repo, with the default-branch reads a
    proof's revision check makes."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.calls = []
        self.fail_patch = set()

    def add(self, number, labels, body, comments=(), state="open"):
        self.issues[number] = {"number": number, "state": state, "body": body,
                               "labels": [{"name": x} for x in labels]}
        self.comments[number] = [dict(c) for c in comments]

    def names(self, number):
        return [x["name"] for x in self.issues[number]["labels"]]

    def said(self, number):
        return [c["body"] for c in self.comments[number]]

    def writes(self, number):
        base = "/repos/%s/issues/%d" % (REPO, number)
        return [(m, p) for m, p, _ in self.calls
                if m != "GET" and (p == base or p.startswith(base + "/"))]

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        data = json.loads(body) if body else None
        self.calls.append((method, parts.path, data))
        rest = parts.path[len("/repos/%s" % REPO):]
        if rest == "":
            return 200, {}, json.dumps({"default_branch": "main"}).encode()
        if rest.startswith("/compare/"):
            return 200, {}, json.dumps({"status": "behind"}).encode()
        number, _, sub = rest[len("/issues/"):].partition("/")
        return self._issue(method, int(number), sub, data)

    def _issue(self, method, number, sub, data):
        issue = self.issues[number]
        if method == "GET":
            return 200, {}, json.dumps(self.comments[number] if sub else issue).encode()
        if sub == "comments":
            url = "https://github.com/%s/issues/%d#issuecomment-%d" % (
                REPO, number, 900 + len(self.comments[number]))
            self.comments[number].append({"body": data["body"], "html_url": url,
                                          "user": {"login": "station-bot"}})
            return 201, {}, json.dumps({"html_url": url}).encode()
        if sub.startswith("labels"):
            have = self.names(number) if method == "POST" else []
            issue["labels"] = [{"name": x} for x in have + [
                x for x in (data or {}).get("labels", []) if x not in have]]
            return 200, {}, b"[]"
        if number in self.fail_patch:
            return 502, {}, b'{"message": "bad gateway"}'
        issue.update(data)
        return 200, {}, b"{}"


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.gh = GitHub("t", transport=self.repo)
        # move() reads LOOM_BRIEF_AUTHORS from the environment: only a
        # trusted rewrite comment pairs a ticket with its source (loom#1956).
        patcher = unittest.mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def given(self, ticket_state="shipped", line="Rewritten from issue %d." % SOURCE,
              source_labels=("loom:rewritten",), source_comments=None,
              source_body="Please make the daily line show it.\n", ticket_comments=()):
        self.repo.add(TICKET, ["loom:ticket", "loom:" + ticket_state], ticket_body(line),
                      ticket_comments)
        self.repo.add(SOURCE, list(source_labels), source_body,
                      [rewrite_comment()] if source_comments is None else source_comments)

    def verify(self):
        return brain.move(self.gh, REPO, TICKET, "verified", record=record(),
                          verified_by="codex/model-x")

    def close(self, reason="completed"):
        return brain.close(self.gh, REPO, TICKET, reason, env=ENV)

    def test_rewritten_from_reads_the_line_and_the_older_refs_line(self):
        self.assertEqual(brain.rewritten_from(ticket_body()), SOURCE)
        self.assertEqual(brain.rewritten_from(ticket_body("Refs #7")), 7)
        self.assertIsNone(brain.rewritten_from("Part of #2\nSee issue 6 and #7.\n"))
        self.assertIsNone(brain.rewritten_from(None))

    def test_rewritten_from_ignores_the_text_an_in_place_rewrite_kept(self):
        # loom#1229: an issue rewritten in place keeps its original text below
        # the ticket; a Refs line there is the author's, not a source.
        kept = tickets.with_original(ticket_body("Size: S"), {"title": "Old",
                                                            "body": "Refs #7\n"})
        self.assertIn("> Refs #7", kept)
        self.assertIsNone(brain.rewritten_from(kept))
        unquoted = kept.replace("> Refs #7", "Refs #7")
        self.assertIsNone(brain.rewritten_from(unquoted))

    def test_verifying_the_ticket_closes_its_source_naming_the_ticket_and_proof(self):
        self.given()
        said = self.verify()
        source = self.repo.issues[SOURCE]
        self.assertEqual((source["state"], source.get("state_reason")), ("closed", "completed"))
        comment = self.repo.said(SOURCE)[-1]
        self.assertTrue(comment.startswith(brain.SOURCE_MARKER))
        self.assertIn("Ticket #%d" % TICKET, comment)
        proof_url = self.repo.comments[TICKET][0]["html_url"]
        self.assertTrue(proof.read(self.repo.comments[TICKET][0]["body"]))
        self.assertIn("proof: %s" % proof_url, comment)
        self.assertIn("closed source issue #%d as completed" % SOURCE, said)

    def test_closing_the_verified_ticket_after_says_nothing_more(self):
        self.given()
        self.verify()
        before = list(self.repo.said(SOURCE))
        self.assertIsNone(self.close())
        self.assertEqual(self.repo.issues[TICKET]["state"], "closed")
        self.assertEqual(self.repo.said(SOURCE), before)

    def test_closing_as_completed_closes_a_source_still_open(self):
        # A ticket verified before this change closes later; its source closes then.
        self.given("verified", ticket_comments=[{
            "body": proof.render(record(), CRITERIA), "user": {"login": "station-bot"},
            "html_url": "https://github.com/%s/issues/%d#issuecomment-55" % (REPO, TICKET)}])
        said = self.close()
        self.assertEqual(self.repo.issues[SOURCE]["state"], "closed")
        self.assertEqual(self.repo.issues[SOURCE]["state_reason"], "completed")
        self.assertIn("proof: https://github.com/%s/issues/%d#issuecomment-55" % (REPO, TICKET),
                      self.repo.said(SOURCE)[-1])
        self.assertIn("closed source issue", said)

    def test_a_ticket_closed_not_planned_leaves_its_source_open_with_a_comment(self):
        self.given("failed")
        said = self.close("not_planned")
        self.assertEqual(self.repo.issues[TICKET]["state_reason"], "not_planned")
        self.assertEqual(self.repo.issues[SOURCE]["state"], "open")
        comment = self.repo.said(SOURCE)[-1]
        self.assertIn("Ticket #%d, rewritten from this issue, was closed as not planned" % TICKET,
                      comment)
        self.assertIn("stays open", comment)
        self.assertIn("left source issue #%d open" % SOURCE, said)

    def test_an_older_refs_line_closes_its_paired_source(self):
        self.given(line="Refs #%d" % SOURCE)
        self.verify()
        self.assertEqual(self.repo.issues[SOURCE]["state"], "closed")

    def test_an_issue_that_is_not_the_paired_source_is_never_touched(self):
        cases = {
            "not labelled rewritten": dict(source_labels=("loom:triage",)),
            "rewritten as another ticket": dict(source_comments=[rewrite_comment(99)]),
            "no rewrite comment": dict(source_comments=[]),
            "a plain Refs line": dict(line="Refs #%d" % SOURCE, source_comments=[]),
        }
        for why, given in cases.items():
            with self.subTest(why=why):
                self.setUp()
                self.given(**given)
                self.assertIsNone(self.verify())
                self.assertEqual(self.repo.writes(SOURCE), [])
                self.assertEqual(self.repo.issues[SOURCE]["state"], "open")

    def test_a_source_with_its_own_unchecked_criteria_stays_open_with_one_comment(self):
        self.given(source_body="## Acceptance criteria\n- [ ] the daily line shows it\n")
        said = self.verify()
        self.assertEqual(self.repo.issues[SOURCE]["state"], "open")
        self.assertIn("unchecked acceptance criterion", self.repo.said(SOURCE)[-1])
        self.assertIn("left source issue", said)
        self.close()
        self.assertEqual(len(self.repo.said(SOURCE)), 2)

    def test_a_failed_source_close_is_reported_not_raised_and_retried(self):
        self.given()
        self.repo.fail_patch.add(SOURCE)
        said = self.verify()
        self.assertIn("not settled", said)
        self.assertIn("loom:verified", self.repo.names(TICKET))
        self.repo.fail_patch.clear()
        self.close()
        self.assertEqual(self.repo.issues[SOURCE]["state"], "closed")
        # The comment is not said twice when only the close was retried.
        self.assertEqual(len([c for c in self.repo.said(SOURCE)
                              if c.startswith(brain.SOURCE_MARKER)]), 1)

    def test_a_failed_audit_reopens_the_source_and_a_new_proof_closes_it_again(self):
        self.given()
        self.verify()
        self.close()
        said = brain.audit_proof(self.gh, REPO, TICKET, "fail",
                                 "the daily line is not in the log", env=ENV)
        self.assertIn("reopened source issue #%d" % SOURCE, said)
        self.assertEqual(self.repo.issues[SOURCE]["state"], "open")
        self.assertIn(audit.LABEL, self.repo.names(TICKET))
        self.assertIn("is open again", self.repo.said(SOURCE)[-1])
        self.repo.issues[TICKET]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:shipped"}]
        self.verify()
        self.assertEqual(self.repo.issues[SOURCE]["state"], "closed")

    def test_a_source_a_person_closed_is_not_reopened_by_an_audit(self):
        self.given("verified", ticket_comments=[{
            "body": proof.render(record(), CRITERIA), "user": {"login": "station-bot"}}])
        self.repo.issues[SOURCE]["state"] = "closed"
        self.assertIsNone(brain.reopen_source(self.gh, REPO, TICKET, ticket_body()))
        self.assertEqual(self.repo.writes(SOURCE), [])


class UnprovenCloseTests(unittest.TestCase):
    """A ticket closed as completed with no proof (the station's done close,
    or a person's) closes its source by the same rule (2026-10-20)."""

    setUp = SourceTests.setUp
    given = SourceTests.given

    def settle(self):
        return brain.settle_source(self.gh, REPO, TICKET, ticket_body(), "completed",
                                   unproven=True)

    def test_the_source_closes_saying_the_ticket_was_closed_not_verified(self):
        self.given()
        self.assertIn("closed source issue #%d" % SOURCE, self.settle())
        source = self.repo.issues[SOURCE]
        self.assertEqual((source["state"], source.get("state_reason")), ("closed", "completed"))
        comment = self.repo.said(SOURCE)[-1]
        self.assertIn("was closed as completed", comment)
        self.assertNotIn("verified", comment)
        self.assertNotIn("force-close", self.repo.names(SOURCE))

    def test_a_source_with_unchecked_criteria_closes_labelled_force_close(self):
        self.given(source_body="## Acceptance criteria\n- [ ] The daily line shows it\n")
        self.settle()
        self.assertEqual(self.repo.issues[SOURCE]["state"], "closed")
        self.assertIn("force-close", self.repo.names(SOURCE))

    def test_a_source_loom_already_held_is_left_alone(self):
        held = "%s\nTicket #%d ... stays open." % (brain._source_marker(TICKET, "held"), TICKET)
        self.given(source_comments=[rewrite_comment(), {"body": held,
                                                        "user": {"login": "station-bot"}}])
        self.assertIsNone(self.settle())
        self.assertEqual(self.repo.writes(SOURCE), [])


if __name__ == "__main__":
    unittest.main()
