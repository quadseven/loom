"""A lane never ticks an operator-decision box (loom#2029).

A box whose text starts with "Operator decides" or "Decision:", or that
carries the decision marker, asks the operator to decide something. A tick
on it reads as a settled decision to every later step, so only an operator
ticks one: the station unticks a tick made while a lane ran, and a no-change
report on a ticket with an open decision box leaves it waiting on a person.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import criteria_gate, station, status, tickets  # noqa: E402
import test_bus_station as tbs  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = tbs.REPO
DECIDE = "Operator decides whether the old export stays on."
CODE = "A test shows the export is off by default."
BODY_OPEN = (tbs.BODY + "\n## Acceptance criteria\n\n- [ ] %s\n- [ ] %s\n\nSize: S\n"
             % (CODE, DECIDE))
BODY_TICKED = BODY_OPEN.replace("- [ ] " + DECIDE, "- [x] " + DECIDE)
# A no-change answer that proves the code box (loom#2032: an answer settles
# only when every code box is ticked on the ticket or in its checklist).
SAID = tbs.NO_CHANGE_SAID + "> - [x] %s\n" % CODE


class DecisionParser(unittest.TestCase):
    def test_a_box_is_a_decision_by_its_lead_words_or_the_marker(self):
        for text in (DECIDE, "operator decides the cap", "Decision: keep or drop the flag",
                     "**Decision:** which region", "Pick the cap %s" % criteria_gate.DECISION_MARKER,
                     "Operator decides whether a test is needed"):
            with self.subTest(text=text):
                self.assertTrue(criteria_gate.decision(text))
                self.assertEqual(criteria_gate.kind(text), criteria_gate.OPERATOR)
        for text in (CODE, "The decision log is written to disk", "A test shows the decision: x",
                     "The operator's ask names the box"):
            with self.subTest(text=text):
                self.assertFalse(criteria_gate.decision(text))

    def test_ticks_made_after_the_brief_are_found_and_unticked(self):
        self.assertEqual(criteria_gate.open_decisions(BODY_OPEN), [DECIDE])
        self.assertEqual(criteria_gate.decision_ticks(BODY_OPEN, BODY_TICKED), [DECIDE])
        self.assertEqual(criteria_gate.decision_ticks(BODY_TICKED, BODY_TICKED), [])
        code_ticked = BODY_OPEN.replace("- [ ] " + CODE, "- [x] " + CODE)
        self.assertEqual(criteria_gate.decision_ticks(BODY_OPEN, code_ticked), [])
        self.assertEqual(criteria_gate.untick(BODY_TICKED, [DECIDE]), BODY_OPEN)
        crlf = BODY_TICKED.replace("\n", "\r\n")
        self.assertEqual(criteria_gate.untick(crlf, [DECIDE]), BODY_OPEN.replace("\n", "\r\n"))


class DecisionTicks(tbs.Base):
    """The station's side: a lane's run ends and the ticket is settled."""

    def report(self, body_at_brief, body_now, editor="loom-app[bot]", pulls=()):
        self.fake.add(12, state="running", body=body_at_brief, editor=editor)
        self.fake.pulls = list(pulls)
        name = tickets.ticket_name(REPO, 12, 1)
        self.write(self.inbox("codex", "done", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex"))
        self.fake.issues[12]["body"] = body_now
        self.write(os.path.join(self.home, "logs", "codex", name + ".answered"), SAID)
        self.run_pass()
        self.run_pass()

    def incidents(self):
        return [c["body"] for c in self.fake.comments[12]
                if c["body"].startswith(station.DECISION_REVERT_MARKER)]

    def test_a_lane_tick_on_a_decision_box_is_reverted_with_one_incident_note(self):
        self.report(BODY_OPEN, BODY_TICKED)
        self.assertEqual(self.fake.issues[12]["body"], BODY_OPEN)
        notes = self.incidents()
        self.assertEqual(len(notes), 1, self.fake.comments[12])
        self.assertIn("lane codex", notes[0])
        self.assertIn(DECIDE, notes[0])

    def test_an_operator_tick_stays(self):
        self.report(BODY_OPEN, BODY_TICKED, editor=tbs.AUTHORS[1])
        self.assertEqual(self.fake.issues[12]["body"], BODY_TICKED)
        self.assertEqual(self.incidents(), [])

    def test_a_no_change_report_with_an_open_decision_box_waits_on_a_person(self):
        sha = "%040x" % 77
        self.report(BODY_OPEN, BODY_OPEN, pulls=[
            {"number": 77, "state": "closed", "merged_at": "2030-01-01T00:00:00Z",
             "merge_commit_sha": sha, "body": "Refs %s#12" % REPO, "head": {"ref": "a-fix"},
             "user": {"login": "carol-example"}}])
        names = self.fake.names(12)
        self.assertNotIn("loom:merged", names)
        self.assertIn("needs:human", names)
        asks = [c["body"] for c in self.fake.comments[12]
                if c["body"].startswith(station.HUMAN_MARKER)]
        self.assertEqual(len(asks), 1)
        self.assertIn(DECIDE, asks[0])
        self.assertIn("Kind: decision", asks[0])
        events = [t for c in self.fake.comments[12] if status.is_status(c["body"])
                  for _, t in status.parse(c["body"])]
        self.assertTrue(any("waits on a person, not loom:merged" in e for e in events), events)

    def test_a_no_change_report_with_no_decision_box_still_moves_to_merged(self):
        body = BODY_OPEN.replace("- [ ] " + DECIDE + "\n", "")
        self.report(body, body, pulls=[
            {"number": 77, "state": "closed", "merged_at": "2030-01-01T00:00:00Z",
             "merge_commit_sha": "%040x" % 77, "body": "Refs %s#12" % REPO,
             "head": {"ref": "a-fix"}, "user": {"login": "carol-example"}}])
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("needs:human", self.fake.names(12))


if __name__ == "__main__":
    unittest.main()
