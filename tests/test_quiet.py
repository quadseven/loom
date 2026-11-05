"""A goal quiet since its last report is owed no new one (loom#1534).

A goal home got a new report comment every 6 hours, each emailing its author
"nothing moved, nothing needs you". These tests pin the quiet rule
(bus/quiet.py) and each place that follows it: `bus.brain status`, `python3
-m bus journal` and the heartbeat that edits the last report in place.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import __main__ as cli  # noqa: E402
from bus import brain, handoff, journal, metrics, quiet  # noqa: E402
from bus.github import GitHub  # noqa: E402
import sub_issue_graphql  # noqa: E402

REPO = "example-owner/example-repo"
GOAL = 3
ME = "example-brain"
NOW = datetime(2030, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
REPORT = quiet.REPORT_MARKER + "\nIntake is empty: no open tickets. Needs you: nothing."


def stamp(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(hours, now=NOW):
    return stamp(now - timedelta(hours=hours))


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


def ticket(number, *labels, state="open", **stamps):
    return dict({"number": number, "title": "Ticket %d" % number, "state": state, "body": "",
                 "labels": [{"name": "loom:ticket"}] + [{"name": x} for x in labels]}, **stamps)


class GoalHome:
    """A goal home's REST and GraphQL reads, and the writes made to it."""

    def __init__(self, comments, children=(), issues=()):
        self.comments = list(comments)
        self.children = list(children)
        self.issues = list(issues)
        self.posts, self.edits = [], []

    def goal(self):
        return {"number": GOAL, "title": "Quiet goal", "state": "open", "body": "",
                "created_at": "2029-12-01T00:00:00Z", "labels": [{"name": "loom:goal"}],
                "comments": len(self.comments)}

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        if method == "POST" and path == "/graphql":
            asked = json.loads(body or b"{}").get("query", "")
            if sub_issue_graphql.asks(asked):
                return reply(200, sub_issue_graphql.answer(
                    asked, lambda n: self.children if n == GOAL else []))
            # Status reads the goal's comments through REST (loom#2464).
            return reply(200, {"data": {"repository": {}}})
        if method == "GET" and path == "/user":
            return reply(200, {"login": ME})
        if method == "GET" and path == "/repos/%s/issues" % REPO:
            if query.get("labels") == [quiet.INVARIANT_LABEL]:
                return reply(200, [i for i in self.issues
                                   if quiet.INVARIANT_LABEL in quiet._labels(i)])
            if "labels" in query:
                return reply(200, [])
            return reply(200, [self.goal()] + self.issues)
        if path == "/repos/%s/issues/%d/comments" % (REPO, GOAL):
            if method == "POST":
                self.posts.append(json.loads(body)["body"])
                return reply(201, {"id": 999})
            since = (query.get("since") or [""])[0]
            return reply(200, [c for c in self.comments
                               if c.get("updated_at", c["created_at"]) >= since])
        if path == "/repos/%s/issues/%d/sub_issues" % (REPO, GOAL):
            return reply(200, self.children)
        if method == "PATCH" and path.startswith("/repos/%s/issues/comments/" % REPO):
            self.edits.append((int(path.rsplit("/", 1)[1]), json.loads(body)["body"]))
            return reply(200, {})
        if method == "GET":
            return reply(200, [])
        raise AssertionError("unexpected %s %s" % (method, url))


def report_comment(ident, created, body=REPORT, updated=None, login=ME):
    return {"id": ident, "user": {"login": login}, "created_at": created,
            "updated_at": updated or created, "body": body}


def journal_comment(ident, created, measure):
    return {"id": ident, "user": {"login": ME}, "created_at": created, "updated_at": created,
            "body": "%s\nTick %s, muse\n- Measure: %s" % (quiet.JOURNAL_MARKER, created, measure)}


class QuietRuleTest(unittest.TestCase):
    def quiet(self, comments, children=(), breaches=(), authors=(ME,)):
        return quiet.quiet(comments, children, breaches, NOW, authors)

    def test_a_goal_where_nothing_moved_since_its_report_is_quiet(self):
        comments = [report_comment(1, ago(7))]
        children = [ticket(41, "loom:queued", created_at=ago(30), updated_at=ago(30)),
                    ticket(42, "loom:verified", state="closed", created_at=ago(60),
                           updated_at=ago(20), closed_at=ago(20))]
        found = self.quiet(comments, children)
        self.assertEqual(found, quiet.Quiet(NOW - timedelta(hours=7), NOW - timedelta(hours=7)))
        self.assertFalse(found.heartbeat_due(NOW))

    def test_a_goal_with_no_report_yet_is_never_quiet(self):
        self.assertIsNone(self.quiet([journal_comment(1, ago(2), "3 open")]))
        # A stranger's report is no report of this Loom's.
        self.assertIsNone(self.quiet([report_comment(1, ago(7), login="stranger")]))

    def test_each_thing_that_moved_since_the_report_ends_the_quiet(self):
        last = ago(7)
        moved = {
            "opened": ticket(41, created_at=ago(1)),
            "closed": ticket(41, state="closed", created_at=ago(30), closed_at=ago(1)),
            "merged": ticket(41, "loom:merged", state="closed", updated_at=ago(1)),
            "shipped": ticket(41, "loom:shipped", state="closed", updated_at=ago(1)),
            "verified": ticket(41, "loom:verified", state="closed", updated_at=ago(1)),
            "failed": ticket(41, "loom:failed", created_at=ago(30)),
            "asks the operator": ticket(41, "loom:human", created_at=ago(30)),
            "needs-operator": ticket(41, "needs-operator", created_at=ago(30)),
            "runaway": ticket(41, "loom:runaway", created_at=ago(30)),
            "needs-brain": ticket(41, "needs-brain", created_at=ago(30)),
            "regression": ticket(41, "loom:regression", created_at=ago(30)),
            "stuck in loom:pr": ticket(41, "loom:pr", created_at=ago(30)),
            "invariant": ticket(41, "loom:invariant", created_at=ago(30)),
        }
        for word, child in moved.items():
            with self.subTest(word):
                self.assertIsNone(self.quiet([report_comment(1, last)], [child]))
        with self.subTest("breach"):
            breach = {"number": 9, "state": "open", "labels": [{"name": "loom:invariant"}]}
            self.assertIsNone(self.quiet([report_comment(1, last)], [], [breach]))
        # A ticket that merged before the report, or a pull request younger
        # than the stuck age, is not news.
        self.assertIsNotNone(self.quiet([report_comment(1, ago(3))], [
            ticket(41, "loom:merged", state="closed", updated_at=ago(8), closed_at=ago(8)),
            ticket(42, "loom:pr", created_at=ago(4))]))

    def test_a_closed_ticket_or_breach_holds_nothing_open(self):
        # A closed ticket that still carries an ask or a failed label, or a
        # closed breach, is not news once it closed before the report.
        closed = dict(ticket(41, "loom:human", "loom:failed", "loom:invariant",
                             state="closed", created_at=ago(30), closed_at=ago(20)))
        breach = {"number": 9, "state": "closed", "labels": [{"name": "loom:invariant"}]}
        self.assertIsNotNone(self.quiet([report_comment(1, ago(7))], [closed], [breach]))

    def test_the_metric_line_counts_only_when_its_numbers_change(self):
        before = journal_comment(1, ago(9), "3/9 pass as of 2030-01-08T03:00Z")
        report = report_comment(2, ago(7))
        same = journal_comment(3, ago(1), "3 / 9 numbers pass, read 2030-01-08 11:00Z")
        changed = journal_comment(4, ago(1), "4/9 pass as of 2030-01-08T11:00Z")
        self.assertIsNotNone(self.quiet([before, report, same]))
        self.assertIsNone(self.quiet([before, report, changed]))
        reasons = quiet.moved([], [], quiet.rows([before, report, changed]),
                              NOW - timedelta(hours=7), NOW)
        self.assertEqual(reasons, ["its metric line changed: 4/9 pass as of 2030-01-08T11:00Z"])

    def test_a_heartbeat_is_due_a_week_after_the_last_report_was_touched(self):
        old = ago(24 * 7 + 1)
        self.assertTrue(self.quiet([report_comment(1, old)]).heartbeat_due(NOW))
        # An edit in place (last week's heartbeat) restarts the week.
        touched = self.quiet([report_comment(1, old, updated=ago(24))])
        self.assertEqual(touched.since, NOW - timedelta(hours=24 * 7 + 1))
        self.assertFalse(touched.heartbeat_due(NOW))

    def test_a_heartbeat_replaces_its_old_stamp(self):
        text = quiet.heartbeat(REPORT + "\n" + quiet.HEARTBEAT_STAMP % "2030-01-01T00:00Z", NOW)
        self.assertEqual(text.splitlines()[-1],
                         quiet.HEARTBEAT_STAMP % "2030-01-08T12:00Z")
        self.assertNotIn("2030-01-01T00:00Z", text)
        with self.assertRaises(TypeError):
            quiet.heartbeat(None, NOW)


class StatusTest(unittest.TestCase):
    def status(self, home, now=NOW):
        gh = GitHub("t", transport=home)
        report = brain.status(gh, [REPO], REPO, env={"LOOM_BRIEF_AUTHORS": ME}, now=now,
                              authors=[ME])
        return report, report.split("### Reports owed before unlock")[1].split("###")[0]

    def test_status_owes_no_report_for_a_quiet_goal(self):
        home = GoalHome([report_comment(1, ago(13))],
                        [ticket(41, "loom:queued", created_at=ago(30), updated_at=ago(30))])
        report, owed = self.status(home)
        self.assertIn("report overdue", report)  # the reading-time clock alone would owe it
        self.assertEqual(owed.strip(), "- none")
        self.assertIn("  - quiet since %s: no report owed; heartbeat from %s"
                      % (ago(13), ago(13 - 24 * 7)), report)

    def test_status_still_owes_a_report_for_a_goal_that_moved(self):
        # The ticket's merge is what makes the report owed: without it the
        # same goal is quiet.
        home = GoalHome([report_comment(1, ago(13))],
                        [ticket(41, "loom:merged", state="closed", updated_at=ago(14))])
        self.assertEqual(self.status(home)[1].strip(), "- none")
        home.children = [ticket(41, "loom:merged", state="closed", updated_at=ago(1))]
        report, owed = self.status(home)
        self.assertIn("#%d report OVERDUE since" % GOAL, owed)
        self.assertNotIn("quiet since", report)

    def test_status_counts_a_report_edited_in_place_as_fresh(self):
        # The report was created 30 hours ago and edited an hour ago; the
        # goal moved in between, so only the edit time keeps it from owed.
        edited = REPORT + "\n" + handoff.REPORTED_STAMP % ago(1)
        home = GoalHome([report_comment(1, ago(30), body=edited, updated=ago(1))],
                        [ticket(41, "loom:merged", state="closed", updated_at=ago(5))])
        self.assertEqual(self.status(home)[1].strip(), "- none")

    def test_a_forged_report_stamp_is_not_read_as_an_edit(self):
        # The stamp is plain text: one later than the comment's last edit
        # (or in the future) is ignored, and the report dates from creation.
        forged = REPORT + "\n" + handoff.REPORTED_STAMP % ago(-48)
        home = GoalHome([report_comment(1, ago(30), body=forged)],
                        [ticket(41, "loom:merged", state="closed", updated_at=ago(5))])
        self.assertIn("#%d report OVERDUE since" % GOAL, self.status(home)[1])

    def test_status_owes_a_heartbeat_a_week_after_the_last_report(self):
        home = GoalHome([report_comment(1, ago(24 * 7 + 1))])
        _, owed = self.status(home)
        self.assertIn("#%d report HEARTBEAT: quiet since %s" % (GOAL, ago(24 * 7 + 1)), owed)
        self.assertIn("edits the report in place, no new comment", owed)


class JournalTest(unittest.TestCase):
    def post(self, home, text=REPORT + "\nStill nothing."):
        gh = GitHub("t", transport=home)
        return journal.post(gh, REPO, GOAL, text, trusted=[ME], now=NOW,
                            quiet=lambda: brain.goal_quiet(gh, REPO, GOAL, [ME], NOW))

    def test_a_report_for_a_quiet_goal_is_refused_and_nothing_posted(self):
        home = GoalHome([report_comment(1, ago(7))])
        with self.assertRaises(journal.ReportQuiet) as held:
            self.post(home)
        self.assertTrue(str(held.exception).startswith("quiet since %s" % ago(7)))
        self.assertEqual((home.posts, home.edits), ([], []))

    def test_a_report_for_a_goal_that_moved_is_posted(self):
        # It asks something new of the operator; one asking the same is
        # edited in place (loom#1560).
        home = GoalHome([report_comment(1, ago(7))], [ticket(41, created_at=ago(1))])
        text = quiet.REPORT_MARKER + "\nTicket 41 opened. Needs you: review #41."
        self.post(home, text)
        self.assertEqual(home.posts, [text])
        self.assertEqual(home.edits, [])

    def test_a_report_asking_nothing_new_edits_the_last_report_in_place(self):
        # loom#1560: a goal with a stuck ticket is never quiet, and its reports
        # repeated the same news as new comments, each an email.
        home = GoalHome([report_comment(5, ago(7), body=REPORT + "\nOld news.")],
                        [ticket(41, created_at=ago(1))])
        self.post(home, REPORT + "\nNew news.")
        self.assertEqual(home.posts, [])  # no new comment, so no email
        self.assertEqual([ident for ident, _ in home.edits], [5])
        self.assertEqual(home.edits[0][1], REPORT + "\nNew news.\n"
                         + handoff.REPORTED_STAMP % "2030-01-08T12:00:00Z")

    def test_a_report_with_a_new_ask_is_posted(self):
        home = GoalHome([report_comment(5, ago(7))], [ticket(41, created_at=ago(1))])
        text = quiet.REPORT_MARKER + "\nNew news. Needs you: decide #44."
        self.post(home, text)
        self.assertEqual((home.posts, home.edits), ([text], []))

    def test_a_report_edited_in_place_holds_the_next_one_back(self):
        edited = REPORT + "\n" + handoff.REPORTED_STAMP % ago(1)
        home = GoalHome([report_comment(5, ago(30), body=edited, updated=ago(1))],
                        [ticket(41, created_at=ago(0.5))])
        with self.assertRaises(journal.ReportTooSoon):
            self.post(home)

    def test_the_weekly_heartbeat_edits_the_last_report_in_place(self):
        home = GoalHome([report_comment(1, ago(24 * 9)),
                         report_comment(2, ago(24 * 8), login="example-loom[bot]"),
                         report_comment(5, ago(24 * 7 + 1))])
        self.post(home)
        self.assertEqual(home.posts, [])  # no new comment, so no email
        self.assertEqual([ident for ident, _ in home.edits], [5])
        self.assertEqual(home.edits[0][1], REPORT + "\nStill nothing.\n"
                         + quiet.HEARTBEAT_STAMP % "2030-01-08T12:00Z")

    def test_a_heartbeat_with_no_report_of_its_own_posts_nothing(self):
        home = GoalHome([report_comment(2, ago(24 * 8), login="example-loom[bot]")])
        with mock.patch.dict(os.environ, {"LOOM_GITHUB_LOGIN": ""}), \
                self.assertRaisesRegex(journal.ReportQuiet, "no report on this goal home"):
            journal.post(GitHub("t", transport=home), REPO, GOAL, REPORT, trusted=[],
                         now=NOW, quiet=lambda: brain.goal_quiet(
                             GitHub("t", transport=home), REPO, GOAL, [], NOW))
        self.assertEqual((home.posts, home.edits), ([], []))

    def test_the_journal_command_says_not_posted_quiet_since_and_exits_0(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        home = GoalHome([report_comment(1, ago(7, now))])
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.md")
            with open(path, "w") as fh:
                fh.write(REPORT + "\n")
            with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": ME, "LOOM_GITHUB_LOGIN": ""}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["journal", "%s#%d" % (REPO, GOAL), "--file", path],
                                gh=GitHub("t", transport=home))
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("not posted: quiet since %s" % ago(7, now)),
                        out.getvalue())
        self.assertEqual((home.posts, home.edits), ([], []))


class VisibilityTest(unittest.TestCase):
    def test_a_quiet_goal_keeps_the_visibility_number_passing(self):
        # The daily number wants each goal reported by its reading time; a quiet
        # goal is owed none, so its older report still counts as current.
        home = GoalHome([report_comment(1, ago(30))])
        total = metrics.Totals()
        collector = metrics.Collector(GitHub("t", transport=home), [REPO], "", NOW.timestamp(),
                                      "Grug - Elder", [ME])
        collector._goal_reports(REPO, [home.goal()], total)
        self.assertEqual((total.report_ages, total.quiet_goals), ({}, ["%s#%d" % (REPO, GOAL)]))
        number = metrics.visibility_number(total)
        self.assertTrue(number.passed)
        self.assertEqual(number.value, "%s#%d quiet" % (REPO, GOAL))
        # The same goal with a ticket opened since its report is held to the cadence.
        home.children = [ticket(41, created_at=ago(1))]
        total = metrics.Totals()
        metrics.Collector(GitHub("t", transport=home), [REPO], "", NOW.timestamp(),
                          "Grug - Elder", [ME])._goal_reports(REPO, [home.goal()], total)
        self.assertFalse(metrics.visibility_number(total).passed)


if __name__ == "__main__":
    unittest.main()
