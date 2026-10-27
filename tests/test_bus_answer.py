"""One-tap answers (loom#1777): the answer options an ask's own words offer,
the answer comment's format, and who counts as having answered."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import answer, brain, operator_hold, shadow  # noqa: E402

BOT = "loom-app[bot]"
OP = "operator-one"


def comment(body, login=BOT, at="2030-01-01T10:00:00Z", ident=1):
    return {"id": ident, "body": body, "user": {"login": login}, "created_at": at,
            "html_url": "https://github.com/example-owner/app/issues/5#issuecomment-%d" % ident}


class Options(unittest.TestCase):
    TABLE = (
        ("Should the lane keep its key? Reply yes or no.", answer.CHOICE, ("Yes", "No")),
        ("Rotate it now (reply yes) or leave it for the window.", answer.CHOICE, ("Yes", "No")),
        ("Which store? Reply a or b: a is the vault, b the parameter store.", answer.CHOICE, ("a", "b")),
        ("Pick one, reply a, b or c.", answer.CHOICE, ("a", "b", "c")),
        ("Options: (a) move the volume first (b) pin the node (c) do nothing.", answer.CHOICE,
         ("a", "b", "c")),
        ("Kind: decision\nMerge #12 now or hold it until the window.", answer.CHOICE, ("Merge", "Hold")),
        (operator_hold.ask(8, ["it declares operator steps before merge: resize the disk"]),
         answer.CHOICE, ("Merge", "Hold")),
        ("Keep this ticket open or close it as done?", answer.CHOICE, ("Keep", "Close")),
        ("Sign in to the console and reply done.", answer.CHOICE, ("Done", "Not now")),
        ("Kind: account\nCreate the service account for the lane.", answer.CHOICE, ("Done", "Not now")),
        ("Kind: physical\nPower-cycle the switch in the rack.", answer.CHOICE, ("Done", "Not now")),
        ("Kind: hardware\nSeat the second disk.", answer.CHOICE, ("Done", "Not now")),
        ("How many lanes should run at night? Reply with one number.", answer.NUMBER, ()),
        ("Kind: money\nWhat should the monthly cap be, and on which card?", answer.TEXT, ()),
        ("", answer.TEXT, ()),
    )

    def test_each_rule_of_the_table_gives_its_options(self):
        for ask, kind, choices in self.TABLE:
            with self.subTest(ask=ask[:50]):
                found = answer.options(ask)
                self.assertEqual(found.kind, kind)
                self.assertEqual(found.choices, choices)

    def test_the_same_ask_always_offers_the_same_answers(self):
        ask = "Keep the old runner or close it?"
        self.assertEqual(answer.options(ask), answer.options(ask))

    def test_a_kind_from_the_loom_human_comment_line_counts(self):
        # bus.brain.human writes the Kind line after the ask.
        self.assertEqual(answer.options("Plug in the probe.\nKind: physical").choices, ("Done", "Not now"))

    def test_accept_takes_only_an_offered_choice_or_a_number(self):
        opts = answer.options("Reply yes or no.")
        self.assertEqual(answer.accept(opts, "yes", "  soon  "), ("Yes", "soon"))
        with self.assertRaises(ValueError):
            answer.accept(opts, "maybe")
        with self.assertRaises(ValueError):
            answer.accept(answer.Options(answer.NUMBER), "three")
        self.assertEqual(answer.accept(answer.Options(answer.NUMBER), "3"), ("3", ""))
        with self.assertRaises(ValueError):
            answer.accept(answer.Options(answer.TEXT), "   ")

    def test_decision_default_is_an_offered_option_and_only_reversible_decisions_may_have_one(self):
        ask = "Kind: decision\nKeep this ticket open or close it?\nDefault: Keep\nReversible: yes"
        self.assertEqual(answer.decision_default(ask), "Keep")
        with self.assertRaises(ValueError):
            answer.decision_default(ask.replace("Default: Keep", "Default: Maybe"))
        with self.assertRaises(ValueError):
            answer.decision_default(ask.replace("Reversible: yes", "Reversible: no"))
        with self.assertRaises(ValueError):
            answer.decision_default("Kind: money\nDefault: Keep\nReversible: yes")
        with self.assertRaises(ValueError):
            answer.decision_default("Kind: decision\nPick the approach.\nDefault: Keep\nReversible: yes")


class Comment(unittest.TestCase):
    def test_the_comment_has_the_marker_the_login_the_choice_and_the_note(self):
        body = answer.comment_body(OP, "Merge", "the disk is resized")
        self.assertEqual(body, "<!-- loom:answer v1 --> Answer from operator-one (via the dashboard): "
                               "Merge. the disk is resized")
        found = answer.parse(comment(body))
        self.assertEqual((found.login, found.choice, found.note), (OP, "Merge", "the disk is resized"))
        self.assertEqual(answer.parse(comment(answer.comment_body(OP, "Yes"))).note, "")

    def test_a_comment_without_the_marker_is_no_answer(self):
        self.assertIsNone(answer.parse(comment("Answer from operator-one (via the dashboard): Merge.")))

    def test_the_human_marker_is_the_brains(self):
        self.assertEqual(answer.HUMAN_MARKER, brain.HUMAN_MARKER)

    def test_the_pending_label_is_not_a_ticket_state(self):
        self.assertNotIn(answer.ANSWERED_LABEL[len("loom:"):], brain.STATES)


class Trust(unittest.TestCase):
    def test_only_a_trusted_author_or_the_operator_named_counts(self):
        body = answer.comment_body(OP, "Yes")
        self.assertIsNotNone(answer.trusted_answer(comment(body), [BOT, OP], [OP]))
        self.assertIsNotNone(answer.trusted_answer(comment(body, login=OP), [BOT], [OP]))
        self.assertIsNone(answer.trusted_answer(comment(body, login="stranger"), [BOT], [OP]))
        named_other = answer.comment_body("stranger", "Yes")
        self.assertIsNone(answer.trusted_answer(comment(named_other), [BOT], [OP]))

    def test_operators_default_to_the_person_logins_of_the_brief_authors(self):
        self.assertEqual(answer.operators({"LOOM_BRIEF_AUTHORS": "%s Operator-One" % BOT}), [OP])
        env = {"LOOM_OPERATORS": "op@github=operator-one, operator-two", "LOOM_BRIEF_AUTHORS": BOT}
        self.assertEqual(answer.operators(env), [OP, "operator-two"])
        self.assertEqual(answer.operator_map(env)["op@github"], OP)

    def test_an_answer_reads_as_the_operators_own_comment(self):
        found = answer.as_operator_comments([comment(answer.comment_body(OP, "Close", "already done in #3"))],
                                            [BOT], [OP], operator="owner-login")
        self.assertEqual(found[0]["user"], {"login": "owner-login", "type": "User"})
        self.assertEqual(found[0]["body"], "Close. already done in #3")


class Verdict(unittest.TestCase):
    def thread(self, *answers, number=8):
        ask = comment(brain.HUMAN_MARKER + "\n" + operator_hold.ask(number, [operator_hold.GENERIC_WHY])
                      + "\nKind: decision", at="2030-01-01T09:00:00Z", ident=1)
        return [ask] + [comment(answer.comment_body(OP, choice), at="2030-01-01T10:%02d:00Z" % i, ident=10 + i)
                        for i, choice in enumerate(answers)]

    def test_the_newest_merge_or_hold_after_the_hold_ask_decides(self):
        self.assertTrue(answer.is_merge(answer.pr_verdict(self.thread("Hold", "Merge"), 8, [BOT], [OP])))
        self.assertTrue(answer.is_hold(answer.pr_verdict(self.thread("Merge", "Hold"), 8, [BOT], [OP])))

    def test_no_verdict_for_another_pull_request_a_newer_ask_or_an_older_head(self):
        self.assertIsNone(answer.pr_verdict(self.thread("Merge", number=7), 8, [BOT], [OP]))
        newer_ask = self.thread("Merge") + [comment(brain.HUMAN_MARKER + "\n" + operator_hold.ask(
            8, ["a new reason"]), at="2030-01-01T11:00:00Z", ident=99)]
        self.assertIsNone(answer.pr_verdict(newer_ask, 8, [BOT], [OP]))
        self.assertIsNone(answer.pr_verdict(self.thread("Merge"), 8, [BOT], [OP], since="2030-01-01T12:00:00Z"))
        self.assertIsNone(answer.pr_verdict(self.thread("Yes"), 8, [BOT], [OP]))

    def test_a_verdict_is_written_once_to_the_shadow_ledger_and_left_out_of_the_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = os.path.join(folder, "ledger.jsonl")
            env = {"LOOM_SHADOW_LEDGER": ledger}
            found = answer.pr_verdict(self.thread("Merge"), 8, [BOT], [OP])
            first = answer.record_verdict("example-owner/app", 8, found, "abc", ("example-owner/app", 5), env=env)
            again = answer.record_verdict("example-owner/app", 8, found, "abc", ("example-owner/app", 5), env=env)
            self.assertTrue(first["new"])
            self.assertFalse(again["new"])
            entries = shadow.read(ledger)
            self.assertEqual(len(entries), 1)
            self.assertEqual((entries[0]["action"], entries[0]["label"]), (shadow.VERDICT, "merge"))
            self.assertIn("operator-one answered merge", entries[0]["reason"])
            with mock.patch.dict(os.environ, {"LOOM_SHADOW": "1"}):
                digest = shadow.digest_lines(0, 2e9, ledger, env={"LOOM_SHADOW": "1"})
            self.assertIn("nothing would have merged", digest[0])
            report = "\n".join(shadow.report_lines(entries, 0, ledger))
            self.assertIn("0 would-actions", report)
            self.assertIn("operator verdict merge", report)


if __name__ == "__main__":
    unittest.main()
