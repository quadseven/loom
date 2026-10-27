"""A goal report keeps "Needs you:" apart from the count of what waits (loom#1775).

On 2026-10-25 a report ended "Needs you: nothing. loom:human: 28 in <repo>."
and the operator read it as "nothing needed" and "28 things needed" at once.
The count now has its own "Waiting on you:" line; `journal` compares only the
"Needs you:" line, so a moving count alone edits the last report in place, and
refuses a report that mixes the two.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import __main__ as cli  # noqa: E402
from bus import brain, journal, quiet  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests.test_quiet import (GOAL, ME, NOW, REPO, GoalHome, ago, report_comment,  # noqa: E402
                              ticket)

OLD = quiet.REPORT_MARKER + "\nTicket 41 opened.\nNeeds you: nothing. loom:human: 28 in %s." % REPO
NEEDS = "Needs you: nothing new for this goal"


def report(count_line, needs=NEEDS):
    return "%s\nTicket 41 opened.\n%s\n%s" % (quiet.REPORT_MARKER, needs, count_line)


def human(number, *labels):
    return ticket(number, "loom:human", *labels, created_at=ago(1))


class CheckTest(unittest.TestCase):
    def test_nothing_beside_a_count_on_the_needs_you_line_is_refused(self):
        with self.assertRaisesRegex(journal.ReportInvalid, "says nothing is needed and"):
            journal.check_report(OLD)

    def test_a_count_on_a_real_ask_is_refused_too(self):
        # The count moves; the ask must not move with it.
        with self.assertRaisesRegex(journal.ReportInvalid, "carries a loom:human count"):
            journal.check_report(quiet.REPORT_MARKER + "\nNeeds you: decide #44. loom:human: 3 in r/n.")

    def test_nothing_beside_a_count_on_the_waiting_line_is_refused(self):
        with self.assertRaisesRegex(journal.ReportInvalid, "Waiting on you"):
            journal.check_report(report("Waiting on you: nothing, 28 open loom:human"))

    def test_the_two_line_forms_pass(self):
        journal.check_report(report(journal.waiting_line({REPO: 28}, 4)))
        journal.check_report(report("Waiting on you: nothing"))
        journal.check_report(report("Waiting on you: nothing", needs="Needs you: decide #44"))
        journal.check_report(quiet.REPORT_MARKER + "\nNo needs-you line at all.")

    def test_the_helper_writes_the_line_from_data(self):
        self.assertEqual(
            journal.waiting_line({"o/a": 25, "o/b": 3, "o/c": 0}, 4),
            "Waiting on you: 28 open loom:human (25 in o/a, 3 in o/b; 4 of them under this "
            "goal), listed in the daily digest")
        self.assertEqual(journal.waiting_line({"o/a": 0}), "Waiting on you: nothing")
        self.assertEqual(journal.waiting_line({}), "Waiting on you: nothing")

    def test_a_public_home_folds_private_repos_into_one_count(self):
        self.assertEqual(
            journal.waiting_line({"pub/a": 25, "pub/b": 3, "priv/c": 7, "priv/d": 2}, 4,
                                 public={"pub/a": True, "pub/b": True, "priv/c": False,
                                         "priv/d": False}),
            "Waiting on you: 37 open loom:human (25 in pub/a, 3 in pub/b, 9 in private repos; 4 "
            "of them under this goal), listed in the daily digest")

    def test_a_private_home_keeps_every_repo_named(self):
        self.assertEqual(
            journal.waiting_line({"pub/a": 25, "priv/c": 7}, 4,
                                 public={"pub/a": True, "priv/c": True}),
            "Waiting on you: 32 open loom:human (25 in pub/a, 7 in priv/c; 4 of them under this "
            "goal), listed in the daily digest")

    def test_a_repo_whose_visibility_cannot_be_read_counts_as_private(self):
        self.assertEqual(
            journal.waiting_line({"pub/a": 25, "unk/b": 7}, 4, public={"pub/a": True}),
            "Waiting on you: 32 open loom:human (25 in pub/a, 7 in private repos; 4 of them "
            "under this goal), listed in the daily digest")

    def test_a_needs_you_with_nothing_after_it_does_not_read_the_next_line(self):
        text = quiet.REPORT_MARKER + "\nNeeds you:\n" + journal.waiting_line({REPO: 3}, 1)
        self.assertEqual(journal._ask(text), "")


class PostTest(unittest.TestCase):
    def post(self, home, text):
        gh = GitHub("t", transport=home)
        return journal.post(gh, REPO, GOAL, text, trusted=[ME], now=NOW,
                            quiet=lambda: brain.goal_quiet(gh, REPO, GOAL, [ME], NOW))

    def home(self, last):
        return GoalHome([report_comment(5, ago(7), body=last)], [ticket(41, created_at=ago(1))])

    def test_the_old_contradictory_line_is_refused_and_nothing_is_posted(self):
        home = self.home(report(journal.waiting_line({REPO: 28}, 4)))
        with self.assertRaises(journal.ReportInvalid):
            self.post(home, OLD)
        self.assertEqual((home.posts, home.edits), ([], []))

    def test_the_two_line_form_posts_when_it_asks_something_new(self):
        home = self.home(report("Waiting on you: nothing"))
        text = report(journal.waiting_line({REPO: 28}, 4), needs="Needs you: decide #44")
        self.post(home, text)
        self.assertEqual((home.posts, home.edits), ([text], []))

    def test_a_changed_waiting_count_alone_edits_in_place(self):
        home = self.home(report(journal.waiting_line({REPO: 28}, 4)))
        self.post(home, report(journal.waiting_line({REPO: 27}, 3)))
        self.assertEqual(home.posts, [])  # no new comment, so no email
        self.assertEqual([ident for ident, _ in home.edits], [5])
        self.assertIn("Waiting on you: 27 open loom:human", home.edits[0][1])

    def test_a_changed_needs_you_still_posts_a_new_comment(self):
        home = self.home(report(journal.waiting_line({REPO: 28}, 4)))
        text = report(journal.waiting_line({REPO: 28}, 4), needs="Needs you: decide #44")
        self.post(home, text)
        self.assertEqual((home.posts, home.edits), ([text], []))

    def test_the_journal_command_prints_not_posted_and_exits_nonzero(self):
        home = self.home(report("Waiting on you: nothing"))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.md")
            with open(path, "w") as fh:
                fh.write(OLD + "\n")
            with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": ME, "LOOM_GITHUB_LOGIN": ""}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["journal", "%s#%d" % (REPO, GOAL), "--file", path],
                                gh=GitHub("t", transport=home))
        self.assertEqual(code, 2)
        self.assertTrue(out.getvalue().startswith("not posted: the \"Needs you:\" line"),
                        out.getvalue())
        self.assertEqual((home.posts, home.edits), ([], []))


class StatusTest(unittest.TestCase):
    def status(self, home):
        gh = GitHub("t", transport=home)
        out = brain.status(gh, [REPO], REPO, env={"LOOM_BRIEF_AUTHORS": ME}, now=NOW,
                           authors=[ME])
        return out.split("## Waiting on you")[1].split("\n## ")[0]

    def test_status_fills_the_line_with_the_repo_count_and_the_goals_share(self):
        # Three loom:human issues in the repo; two sit under the goal.
        mine = [human(41), human(42)]
        other = human(77)
        home = GoalHome([report_comment(1, ago(7))], mine, issues=mine + [other])
        section = self.status(home)
        self.assertIn("- %s#%d: Waiting on you: 3 open loom:human (3 in %s; 2 of them under "
                      "this goal), listed in the daily digest" % (REPO, GOAL, REPO), section)

    def test_status_says_nothing_only_when_nothing_waits(self):
        home = GoalHome([report_comment(1, ago(7))], [ticket(41, "loom:queued")])
        self.assertIn("- %s#%d: Waiting on you: nothing" % (REPO, GOAL), self.status(home))

    def test_a_closed_loom_human_issue_is_not_counted(self):
        closed = dict(human(41), state="closed")
        home = GoalHome([report_comment(1, ago(7))], [closed], issues=[])
        self.assertIn("Waiting on you: nothing", self.status(home))

    def test_waiting_lines_folds_private_repos_for_a_public_home(self):
        waiting = {REPO: {"count": 3, "goals": {GOAL: 2}},
                   "priv/other": {"count": 7, "goals": {}}}
        lines = brain.waiting_lines(waiting, [REPO, "priv/other"],
                                    public={REPO: True, "priv/other": False})
        self.assertIn("- %s#%d: Waiting on you: 10 open loom:human (3 in %s, 7 in private repos; "
                      "2 of them under this goal), listed in the daily digest" % (REPO, GOAL, REPO),
                      "\n".join(lines))

    def test_waiting_lines_names_every_repo_for_a_private_home(self):
        waiting = {REPO: {"count": 3, "goals": {GOAL: 2}},
                   "priv/other": {"count": 7, "goals": {}}}
        lines = brain.waiting_lines(waiting, [REPO, "priv/other"],
                                    public={REPO: False, "priv/other": False})
        self.assertIn("- %s#%d: Waiting on you: 10 open loom:human (7 in priv/other, 3 in %s; "
                      "2 of them under this goal), listed in the daily digest" % (REPO, GOAL, REPO),
                      "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
