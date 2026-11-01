"""A needs-you item comes from the whole ticket, not from a label or one page
of comments (loom#2387)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import ask_state, criteria_gate, label_pass, needs_you  # noqa: E402
from bus.answer import HUMAN_MARKER  # noqa: E402
from bus.github import GitHub, GitHubError  # noqa: E402

REPO = "example-owner/project"
OP = "op-example"
BRAIN = "loom-brain[bot]"
STATION = "loom-station[bot]"
TRUSTED = [OP, BRAIN, STATION]
ASKED = "2030-01-02T00:00:00Z"
ASK = "Ask for the operator: approve the retention rule, (a) or (b)?"
API = "https://api.github.com"


def ref(number):
    return "%s#%d" % (REPO, number)


def issue(number=5, labels=("needs:human",), body=""):
    return {"number": number, "body": body, "html_url": "https://example.com/%d" % number,
            "labels": [{"name": n} for n in labels]}


def said(body, at="2030-01-03T00:00:00Z", by=OP, ident=1):
    return {"id": ident, "body": body, "created_at": at, "user": {"login": by},
            "html_url": "https://example.com/c/%d" % ident}


def ask(text=ASK, at=ASKED, by=OP):
    return said("%s\n%s\nKind: decision" % (HUMAN_MARKER, text), at, by)


def added(name="needs:human", at=ASKED, by=BRAIN):
    return {"event": "labeled", "created_at": at, "actor": {"login": by},
            "label": {"name": name}}


def merged(number=9, at="2030-01-03T00:00:00Z"):
    return {"event": "cross-referenced", "source": {"issue": {
        "number": number, "repository": {"full_name": REPO},
        "pull_request": {"merged_at": at}}}}


def judge(comments, item=None, events=(added(),), blockers=(), timeline=()):
    return needs_you.judge(REPO, item or issue(), list(comments), list(events), TRUSTED, [OP],
                           blockers, timeline)


class GitHubLists:
    """GitHub's list behavior: oldest first, 30 to a page unless per_page
    says otherwise, a Link header to the next page. Any write fails."""

    def __init__(self, routes):
        self.routes = routes
        self.reads = []

    def __call__(self, method, url, headers, body):
        if method != "GET":
            raise AssertionError("the command wrote: %s %s" % (method, url))
        self.reads.append(url)
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        data = self.routes[path]
        if isinstance(data, Exception):
            raise data
        if not isinstance(data, list):
            return 200, {}, json.dumps(data).encode()
        size, page = int(query.get("per_page", ["30"])[0]), int(query.get("page", ["1"])[0])
        rows = data[(page - 1) * size:page * size]
        link = ""
        if page * size < len(data):
            link = '<%s%s?per_page=%d&page=%d>; rel="next"' % (API, path, size, page + 1)
        return 200, {"link": link} if link else {}, json.dumps(rows).encode()


def routes_of(item, comments, events=(added(),), timeline=()):
    base = "/repos/%s/issues/%d" % (REPO, item["number"])
    return {"/repos/%s/issues" % REPO: [item], base + "/comments": list(comments),
            base + "/events": list(events), base + "/timeline": list(timeline)}


def report(routes):
    fake = GitHubLists(routes)
    return needs_you.run(GitHub("t", transport=fake), [REPO], TRUSTED, [OP])


class Pagination(unittest.TestCase):
    """A 36-comment ticket whose answer is the last comment: GitHub's first
    30 end before it (the oldest 30, not the newest)."""

    def setUp(self):
        self.item = issue()
        notes = [said("note %d" % n, "2030-01-03T00:%02d:00Z" % n, "someone", n + 2)
                 for n in range(34)]
        self.comments = [ask()] + notes + [said("Take (a).", "2030-02-01T00:00:00Z", OP, 36)]

    def test_the_answer_on_the_last_page_is_read(self):
        text, code = report(routes_of(self.item, self.comments))
        self.assertEqual(0, code)
        self.assertIn("Needs you: nothing", text)
        self.assertIn("Answered", text)
        self.assertIn("latest comment: %s at 2030-02-01T00:00:00Z" % OP, text)

    def test_every_page_of_a_long_ticket_is_read(self):
        comments = [ask()] + [said("Nope?", "2030-01-03T00:%02d:00Z" % (n % 60), "someone", n + 2)
                              for n in range(120)] + [said("Take (b).", "2030-02-01T00:00:00Z")]
        text, _ = report(routes_of(self.item, comments))
        self.assertIn("Needs you: nothing", text)
        self.assertIn("Take (b).", text)


class EachCause(unittest.TestCase):
    def test_a_written_open_ask_needs_you_with_every_field(self):
        text, code = report(routes_of(issue(labels=("needs:human", "needs:someone")), [ask()]))
        self.assertEqual(0, code)
        self.assertIn("Needs you: 1", text)
        self.assertIn("- %s: open" % ref(5), text)
        self.assertIn("> " + ASK, text)
        self.assertIn("to someone and the operator, asked %s" % ASKED, text)
        self.assertIn("latest comment: %s at %s" % (OP, ASKED), text)

    def test_a_brains_dated_operator_answer_line_answers(self):
        line = "Operator answer 2030-01-03: (a), with a dry run first. -- a session"
        found = judge([ask(), said(line, by=BRAIN)])
        self.assertEqual((needs_you.ANSWERED, ask_state.ANSWERED), (found.section, found.state))
        self.assertEqual(line, found.answer)

    def test_a_brains_prose_leaves_the_ask_open(self):
        for text in ("The App exists now. Next: wire it in.", "Operator answer: none yet.",
                     "Operator answer pending.", "Waiting on the operator answer 2030-01-03"):
            with self.subTest(text):
                self.assertEqual(needs_you.NEEDS_YOU, judge([ask(), said(text, by=BRAIN)]).section)

    def test_the_line_from_an_untrusted_author_leaves_the_ask_open(self):
        line = "Operator answer 2030-01-03: (a)"
        self.assertEqual(needs_you.NEEDS_YOU, judge([ask(), said(line, by="drive-by")]).section)

    def test_a_line_dated_before_the_ask_answered_an_earlier_one(self):
        line = "Operator answer 2030-01-01: (a)"
        self.assertEqual(needs_you.NEEDS_YOU, judge([ask(), said(line, by=BRAIN)]).section)

    def test_a_line_posted_before_the_ask_does_not_answer_it(self):
        line = said("Operator answer 2030-01-02: (a)", "2030-01-01T00:00:00Z", BRAIN)
        self.assertEqual(needs_you.NEEDS_YOU, judge([line, ask()]).section)

    def test_a_merged_pull_request_after_the_ask_is_checked_first_and_stays_open(self):
        found = judge([ask()], timeline=[merged(9)])
        self.assertEqual((needs_you.CHECK_FIRST, ask_state.OPEN), (found.section, found.state))
        self.assertEqual((ref(9),), found.merged)

    def test_the_merged_pull_request_is_printed_with_the_ask(self):
        text, _ = report(routes_of(issue(), [ask()], timeline=[merged(9)]))
        self.assertIn("Needs you: nothing", text)
        self.assertIn("merged after the ask: %s" % ref(9), text)
        self.assertIn("> " + ASK, text)

    def test_a_pull_request_merged_before_the_ask_changes_nothing(self):
        found = judge([ask()], timeline=[merged(9, "2030-01-01T00:00:00Z")])
        self.assertEqual(needs_you.NEEDS_YOU, found.section)

    def test_an_open_pull_request_changes_nothing(self):
        found = judge([ask()], timeline=[merged(9, "")])
        self.assertEqual(needs_you.NEEDS_YOU, found.section)

    def test_a_station_verification_tag_is_not_a_person_ask(self):
        text = criteria_gate.ask([(criteria_gate.LIVE, "Live: prints it")], {})
        found = judge([ask(text.replace("Kind: decision\n", ""), by=STATION)])
        self.assertEqual(needs_you.VERIFICATION, found.section)

    def test_a_station_tag_on_a_waiting_ticket_stays_a_tag(self):
        text = criteria_gate.ask([(criteria_gate.LIVE, "Live: prints it")], {})
        found = judge([ask(text, by=STATION)], issue(labels=("needs:human", "wait:blocked")))
        self.assertEqual(needs_you.VERIFICATION, found.section)

    def test_the_same_words_from_an_untrusted_author_are_an_ask(self):
        text = criteria_gate.ask([(criteria_gate.LIVE, "Live: prints it")], {})
        self.assertEqual(needs_you.NEEDS_YOU, judge([ask(text, by="drive-by")]).section)

    def test_a_person_ask_after_the_tag_is_an_ask(self):
        text = criteria_gate.ask([(criteria_gate.LIVE, "Live: prints it")], {})
        found = judge([ask(text, by=STATION), ask(at="2030-01-05T00:00:00Z")])
        self.assertEqual(needs_you.NEEDS_YOU, found.section)

    def test_a_label_with_no_written_ask_is_not_a_question(self):
        self.assertEqual(needs_you.NO_ASK, judge([]).section)

    def test_the_body_ask_section_counts_as_written(self):
        found = judge([], issue(body="## Ask\nPick one.\n"))
        self.assertEqual((needs_you.NEEDS_YOU, "Pick one."), (found.section, found.ask))

    def test_a_blocked_ask_waits(self):
        self.assertEqual(needs_you.WAITING, judge([ask()], blockers=[7]).section)

    def test_the_latest_comment_is_the_last_of_all_of_them(self):
        found = judge([ask(), said("Why?", "2030-01-04T00:00:00Z", "someone")])
        self.assertEqual(("someone", "2030-01-04T00:00:00Z"), (found.latest_by, found.latest_at))
        self.assertEqual(ASK, found.ask.split("\n")[0])


class LabelPass(unittest.TestCase):
    def plan(self, line):
        return label_pass.plan(issue(), [added()], comments=[ask(), said(line, by=BRAIN)],
                               trusted=TRUSTED, ops=[OP])

    def test_the_dated_line_takes_the_label_off(self):
        self.assertEqual(["needs:human"], [r.label for r in self.plan(
            "Operator answer 2030-01-03: (a)").remove])

    def test_prose_leaves_the_label_on(self):
        self.assertEqual([], self.plan("The App exists now. Next: wire it in.").remove)


class Failures(unittest.TestCase):
    def test_an_unreadable_ticket_is_never_nothing_to_do(self):
        routes = routes_of(issue(), [ask()])
        routes["/repos/%s/issues/5/events" % REPO] = GitHubError("boom", 500, "")
        text, code = report(routes)
        self.assertEqual(1, code)
        self.assertNotIn("nothing", text)
        self.assertIn("Unread, so this report is incomplete: 1", text)
        self.assertIn(ref(5), text)

    def test_an_unreadable_repo_is_never_nothing_to_do(self):
        text, code = report({"/repos/%s/issues" % REPO: GitHubError("boom", 500, "")})
        self.assertEqual(1, code)
        self.assertNotIn("nothing", text)

    def test_a_blocked_on_number_naming_no_issue_blocks_nothing(self):
        item = issue(body="## Blocked on\n\n- #77\n\n## Why\n")
        routes = routes_of(item, [ask()])
        routes["/repos/%s/issues/77" % REPO] = GitHubError("gone", 404, "")
        text, code = report(routes)
        self.assertEqual(0, code)
        self.assertIn("Needs you: 1", text)

    def test_no_label_no_item(self):
        text, _ = report(routes_of(issue(labels=("loom:ticket",)), [ask()]))
        self.assertIn("Needs you: nothing", text)
        self.assertNotIn(ref(5), text)

    def test_without_trusted_logins_it_refuses(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "", "LOOM_REPOS": REPO}), \
                contextlib.redirect_stderr(err):
            self.assertEqual(2, needs_you.main([]))
        self.assertIn("LOOM_BRIEF_AUTHORS", err.getvalue())

    def test_the_ask_time_is_the_labels_when_no_comment_holds_it(self):
        found = judge([], issue(body="## Ask\nPick one.\n"))
        self.assertEqual(ASKED, found.asked_at)


if __name__ == "__main__":
    unittest.main()
