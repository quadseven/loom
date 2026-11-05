"""One label, needs:human, asks a person, and an answered ask leaves the
digest (loom#2278, loom#2261)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import ask_state, label_pass, labels  # noqa: E402
from dashboard import operators  # noqa: E402

OP = "op-example"
APP = "loom-app[bot]"
TRUSTED = [OP, APP]


def issue(*names, body=""):
    return {"number": 5, "body": body, "labels": [{"name": n} for n in names]}


def added(name, at="2030-01-02T00:00:00Z", by=APP):
    return {"event": "labeled", "created_at": at, "actor": {"login": by},
            "label": {"name": name}}


def dropped(name, at):
    return {"event": "unlabeled", "created_at": at, "actor": {"login": APP},
            "label": {"name": name}}


def said(body, at="2030-01-03T00:00:00Z", by=OP):
    return {"body": body, "created_at": at, "user": {"login": by}}


def state(item, comments=(), events=(added("needs-operator"),), blockers=()):
    return ask_state.state(item, list(comments), list(events), TRUSTED, [OP], blockers)


class Alias(unittest.TestCase):
    def test_each_old_label_reads_as_needs_human(self):
        for old in ("needs-operator", "needs:quadseven", "loom:human", "needs:human"):
            with self.subTest(old):
                self.assertTrue(labels.needs_human([old, "loom:ticket"]))

    def test_other_labels_do_not(self):
        self.assertFalse(labels.needs_human(["needs:someone", "needs-brain", "loom:ticket"]))

    def test_needs_human_is_a_managed_label(self):
        self.assertIn(labels.NEEDS_HUMAN, labels.LABELS)


class Dashboard(unittest.TestCase):
    def test_needs_human_is_not_a_person_named_human(self):
        self.assertEqual([], operators.needs_labels(["needs:human"]))

    def test_a_waiting_ask_is_still_an_ask_the_dashboard_reads(self):
        self.assertIn("needs:human", operators.ask_names(["needs:human", "wait:blocked"]))


class State(unittest.TestCase):
    def test_a_decision_after_the_ask_answers_it(self):
        self.assertEqual(ask_state.ANSWERED, state(
            issue("needs-operator"), [said("Decision: clear it.")]))

    def test_an_ask_with_no_reply_stays_open(self):
        self.assertEqual(ask_state.OPEN, state(issue("needs-operator")))

    def test_a_comment_before_the_ask_is_not_an_answer(self):
        self.assertEqual(ask_state.OPEN, state(
            issue("needs-operator"), [said("Context.", at="2030-01-01T00:00:00Z")]))

    def test_a_new_marker_ask_reopens_it(self):
        comments = [said("Yes."),
                    said("<!-- loom:human v1 -->\nOne more thing.", "2030-01-04T00:00:00Z", APP)]
        self.assertEqual(ask_state.OPEN, state(issue("loom:human"), comments))

    def test_a_question_back_is_not_an_answer(self):
        self.assertEqual(ask_state.OPEN, state(
            issue("needs-operator"), [said("Which lane did you mean?")]))

    def test_a_stranger_does_not_answer(self):
        self.assertEqual(ask_state.OPEN, state(
            issue("needs-operator"), [said("Done.", by="drive-by")]))

    def test_a_bot_journal_is_not_an_answer(self):
        self.assertEqual(ask_state.OPEN, state(
            issue("needs-operator"), [said("Tick note.", by=APP)]))

    def test_a_trusted_brain_records_a_finished_step(self):
        self.assertEqual(ask_state.ANSWERED, state(
            issue("needs-operator"), [said("Finished: the key is in place.", by=APP)]))

    def test_a_dashboard_answer_counts(self):
        body = "<!-- loom:answer v1 --> Answer from %s (via the dashboard): Merge." % OP
        self.assertEqual(ask_state.ANSWERED, state(
            issue("needs-operator"), [said(body, by=APP)]))

    def test_an_ask_with_no_label_event_stays_open(self):
        self.assertEqual(ask_state.OPEN, state(
            issue("needs-operator"), [said("Yes.")], events=()))

    def test_migrating_the_label_does_not_restart_the_ask(self):
        events = [added("loom:human"), added("needs:human", "2030-01-05T00:00:00Z"),
                  dropped("loom:human", "2030-01-05T00:00:01Z")]
        self.assertEqual(ask_state.ANSWERED, state(
            issue("needs:human"), [said("Yes, do it.")], events))

    def test_labelling_again_after_it_came_off_is_a_new_ask(self):
        events = [added("needs:human"), dropped("needs:human", "2030-01-04T00:00:00Z"),
                  added("needs:human", "2030-01-05T00:00:00Z")]
        self.assertEqual(ask_state.OPEN, state(issue("needs:human"), [said("Yes.")], events))

    def test_a_ticket_waiting_on_another_is_waiting(self):
        self.assertEqual(ask_state.WAITING, state(issue("needs-operator"), blockers=[9]))
        self.assertEqual(ask_state.WAITING, state(issue("needs-operator", "wait:after")))

    def test_an_answer_beats_a_wait(self):
        self.assertEqual(ask_state.ANSWERED, state(
            issue("needs-operator"), [said("Yes.")], blockers=[9]))


class Pass(unittest.TestCase):
    def plan(self, item, comments=(), events=(added("needs-operator"),), blockers=()):
        return label_pass.plan(item, list(events), [APP], comments=list(comments),
                               trusted=TRUSTED, ops=[OP], blockers=blockers)

    def test_an_answered_ask_loses_every_ask_label(self):
        found = self.plan(issue("needs-operator", "loom:human"), [said("Yes.")])
        self.assertEqual({"needs-operator", "loom:human"}, {r.label for r in found.remove})
        self.assertEqual([], found.add)

    def test_an_open_ask_keeps_its_label_and_is_not_relabelled(self):
        found = self.plan(issue("needs-operator"))
        self.assertEqual(([], []), (found.remove, found.add))

    def test_a_decision_naming_a_command_goes_to_the_brain(self):
        found = self.plan(issue("needs-operator"), [said("Decided: run `make clear`.")])
        self.assertEqual(["needs-brain"], found.add)

    def test_a_waiting_ask_gets_a_wait_label_and_migrates_on_that_write(self):
        found = self.plan(issue("needs-operator"), blockers=[9])
        self.assertEqual(["wait:blocked", "needs:human"], found.add)
        self.assertEqual(["needs-operator"], [r.label for r in found.remove])

    def test_any_other_write_migrates_the_old_label(self):
        found = self.plan(issue("loom:human", "loom:triage", "loom:ticket"))
        self.assertEqual({"loom:triage", "loom:human"}, {r.label for r in found.remove})
        self.assertEqual(["needs:human"], found.add)

    def test_a_claimed_ask_is_left_alone(self):
        found = self.plan(issue("needs-operator", "claim:someone"), [said("Yes.")])
        self.assertEqual(([], []), (found.remove, found.add))

    def test_an_ask_needs_its_events_read(self):
        self.assertTrue(label_pass.needs_events(issue("needs:human")))


if __name__ == "__main__":
    unittest.main()
