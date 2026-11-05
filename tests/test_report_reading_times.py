"""Goal reports twice a day, short, and none for a backlog goal (2026-10-25).

The operator read "slow the standups down... twice a day 10am 9pm eastern...
info overload": every goal report comment emails him. Reports are due at his
two reading times (brain.REPORT_TIMES), a real goal's report is its first
line, at most five more and "Needs you:", and a per-repo backlog goal posts
none: its status is one line in the station's twice-daily digest.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import __main__ as cli  # noqa: E402
from bus import brain, journal, quiet  # noqa: E402
from bus.github import GitHub  # noqa: E402

import test_quiet as tq  # noqa: E402


class BacklogHome(tq.GoalHome):
    """A goal home labelled loom:backlog, which also answers its own issue read."""

    def goal(self):
        issue = super().goal()
        issue["labels"] = issue["labels"] + [{"name": "loom:backlog"}]
        return issue

    def __call__(self, method, url, headers, body):
        if method == "GET" and url.split("?")[0].endswith("/repos/%s/issues/%d" % (tq.REPO, tq.GOAL)):
            return tq.reply(200, self.goal())
        return super().__call__(method, url, headers, body)


def moved(now=tq.NOW):
    """A report 13 hours old (past a reading time) on a goal that moved since."""
    return [tq.report_comment(1, tq.ago(13, now))], [tq.ticket(41, created_at=tq.ago(1, now))]


class BacklogStatusTest(unittest.TestCase):
    def test_status_owes_no_report_for_a_backlog_goal(self):
        comments, children = moved()
        report = brain.status(GitHub("t", transport=BacklogHome(comments, children)), [tq.REPO],
                              tq.REPO, env={"LOOM_BRIEF_AUTHORS": tq.ME}, now=tq.NOW,
                              authors=[tq.ME])
        owed = report.split("### Reports owed before unlock")[1].split("###")[0]
        self.assertEqual(owed.strip(), "- none")
        self.assertIn("backlog goal: no report owed", report)
        # The same goal without the label is owed its report.
        report = brain.status(GitHub("t", transport=tq.GoalHome(comments, children)), [tq.REPO],
                              tq.REPO, env={"LOOM_BRIEF_AUTHORS": tq.ME}, now=tq.NOW,
                              authors=[tq.ME])
        self.assertIn("#%d report OVERDUE since" % tq.GOAL,
                      report.split("### Reports owed before unlock")[1])


class BacklogJournalTest(unittest.TestCase):
    def test_the_journal_command_posts_no_report_on_a_backlog_goal_and_exits_0(self):
        # The command reads the clock, so the comments are dated from now.
        home = BacklogHome(*moved(datetime.now(timezone.utc).replace(microsecond=0)))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.md")
            with open(path, "w") as fh:
                fh.write(quiet.REPORT_MARKER + "\nTicket 41 opened. Needs you: review #41.\n")
            with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": tq.ME,
                                              "LOOM_GITHUB_LOGIN": ""}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["journal", "%s#%d" % (tq.REPO, tq.GOAL), "--file", path],
                                gh=GitHub("t", transport=home))
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("not posted: a backlog goal home"),
                        out.getvalue())
        self.assertEqual((home.posts, home.edits), ([], []))


class ShortReportTest(unittest.TestCase):
    HEAD = quiet.REPORT_MARKER + "\nTwo tickets merged; the failure rate fell from 4 to 2."
    TAIL = "\nNeeds you: nothing new for this goal\nWaiting on you: nothing"

    def test_a_report_of_a_headline_and_five_lines_passes(self):
        body = self.HEAD + "".join("\nLine %d." % n for n in range(5)) + self.TAIL
        journal.check_report(body)

    def test_a_longer_report_is_refused_and_nothing_posted(self):
        body = (self.HEAD + "\n\n### Going well\n- one\n- two\n\n### Not going well\n- three"
                + "\n\n### Next\n- four" + self.TAIL)
        with self.assertRaisesRegex(journal.ReportInvalid, "at most 5 more"):
            journal.check_report(body)
        home = tq.GoalHome(*moved())
        with self.assertRaises(journal.ReportInvalid):
            journal.post(GitHub("t", transport=home), tq.REPO, tq.GOAL, body, trusted=[tq.ME],
                         now=tq.NOW)
        self.assertEqual((home.posts, home.edits), ([], []))


if __name__ == "__main__":
    unittest.main()
