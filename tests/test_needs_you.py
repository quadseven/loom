"""A needs-you item comes from the whole ticket, not from a label or one page
of comments (loom#2387)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
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
        if callable(data) and not isinstance(data, type):
            data = data()
        if isinstance(data, Exception):
            raise data
        if not isinstance(data, list):
            return 200, {}, json.dumps(data).encode()
        size, page = int(query.get("per_page", ["30"])[0]), int(query.get("page", ["1"])[0])
        rows = data[(page - 1) * size:page * size]
        link = ""
        if page * size < len(data):
            last = (len(data) + size - 1) // size
            link = ", ".join('<%s%s?per_page=%d&page=%d>; rel="%s"' % (API, path, size, at, rel)
                             for at, rel in ((page + 1, "next"), (last, "last")))
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



SLOW, QUICK = "example-owner/slow", "example-owner/quick"


class Budget(unittest.TestCase):
    """The command finishes inside a Muse tick (loom#2394): Muse gave it 120,
    100 and then 45 seconds and got no output at all, because every repo and
    every ticket was read one after another and the report printed last."""

    def two_repos(self, slow_repo):
        quick = issue(5)
        base = "/repos/%s/issues/5" % QUICK
        routes = {"/repos/%s/issues" % QUICK: [quick],
                  base + "/comments": [ask()], base + "/events": [added()], base + "/timeline": [],
                  "/repos/%s/issues" % SLOW: slow_repo}
        return routes

    def test_a_repo_that_hangs_is_named_and_the_rest_is_printed(self):
        release = threading.Event()
        self.addCleanup(release.set)

        fake = GitHubLists(self.two_repos(lambda: release.wait(5) and []))
        started = time.monotonic()
        with mock.patch.object(needs_you, "DEADLINE", 0.5, create=True):
            text, code = needs_you.run(GitHub("t", transport=fake, sleep=lambda _: None),
                                       [SLOW, QUICK], TRUSTED, [OP])
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(1, code)
        self.assertIn("Needs you: 1", text)
        self.assertIn("- %s#5: open" % QUICK, text)
        self.assertIn("Unread, so this report is incomplete: 1", text)
        self.assertIn("- %s: not read within the 0.5s deadline" % SLOW, text)
        self.assertNotIn("nothing", text)

    def test_a_repo_that_fails_is_named_and_the_rest_is_printed(self):
        fake = GitHubLists(self.two_repos(GitHubError("request failed: RemoteDisconnected", 0, "")))
        text, code = needs_you.run(GitHub("t", transport=fake, sleep=lambda _: None),
                                   [SLOW, QUICK], TRUSTED, [OP])
        self.assertEqual(1, code)
        self.assertIn("- %s#5: open" % QUICK, text)
        self.assertIn("- %s: request failed: RemoteDisconnected" % SLOW, text)

    def test_the_deadline_prints_the_report_from_the_command(self):
        release = threading.Event()
        self.addCleanup(release.set)
        slow = lambda: release.wait(5) and []  # noqa: E731
        fake = GitHubLists({"/repos/%s/issues" % SLOW: slow})
        out = io.StringIO()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = {"LOOM_BRIEF_AUTHORS": OP, "LOOM_REPOS": SLOW, "LOOM_HOME": home.name}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), \
                mock.patch.object(needs_you, "auth_from_env", return_value="t"), \
                mock.patch.object(needs_you, "_short_transport", fake, create=True):
            self.assertEqual(1, needs_you.main(["--deadline", "0.3"]))
        self.assertIn("- %s: not read within the 0.3s deadline" % SLOW, out.getvalue())


class Requests(unittest.TestCase):
    """What one run reads, pinned so it does not grow back."""

    def busy_repo(self):
        asks = [issue(n, labels=("needs:human",)) for n in (3, 4)] + \
            [issue(6, labels=("needs:someone", "needs:human"))]
        others = [issue(n, labels=("loom:ticket",)) for n in range(10, 260)]
        routes = {"/repos/%s/issues" % REPO: asks + others}
        for item in asks:
            base = "/repos/%s/issues/%d" % (REPO, item["number"])
            routes.update({base + "/comments": [ask()], base + "/events": [added()],
                           base + "/timeline": []})
        return routes

    def test_each_page_and_each_asking_ticket_is_read_once(self):
        fake = GitHubLists(self.busy_repo())
        gh = GitHub("t", transport=fake)
        # As main reads: a first comments page also reads its issue's body,
        # which the listing already holds.
        gh.status_in_body = True
        gh.status_authors |= {OP}
        text, code = needs_you.run(gh, [REPO], TRUSTED, [OP])
        self.assertEqual(0, code)
        self.assertIn("Needs you: 3", text)
        paths = sorted("%s?page=%s" % (urlsplit(u).path,
                                       parse_qs(urlsplit(u).query).get("page", ["1"])[0])
                       for u in fake.reads)
        want = ["/repos/%s/issues?page=%d" % (REPO, n) for n in (1, 2, 3)]
        for n in (3, 4, 6):
            want += ["/repos/%s/issues/%d/%s?page=1" % (REPO, n, kind)
                     for kind in ("comments", "events", "timeline")]
        self.assertEqual(sorted(want), paths)

    def test_the_pages_of_a_busy_repo_are_read_in_parallel(self):
        gate = threading.Barrier(2, timeout=2)
        routes = self.busy_repo()
        rows = routes["/repos/%s/issues" % REPO]
        seen = []

        def listing():
            seen.append(1)
            if len(seen) > 1:  # pages 2 and 3 meet, or one waits alone
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    return GitHubError("read alone: the pages were read one at a time", 500, "")
            return rows
        routes["/repos/%s/issues" % REPO] = listing
        gh = GitHub("t", transport=GitHubLists(routes))
        text, code = needs_you.run(gh, [REPO], TRUSTED, [OP])
        self.assertEqual((0, "Needs you: 3"), (code, text.split("\n")[0]))

    def test_a_blocker_named_by_many_asks_is_read_once(self):
        routes = self.busy_repo()
        for n in (3, 4):
            routes["/repos/%s/issues" % REPO][[3, 4].index(n)]["body"] = "## Blocked on\n\n- #999\n"
        routes["/repos/%s/issues/999" % REPO] = {"number": 999, "state": "open"}
        fake = GitHubLists(routes)
        needs_you.run(GitHub("t", transport=fake), [REPO], TRUSTED, [OP])
        self.assertEqual(1, sum(urlsplit(u).path.endswith("/issues/999") for u in fake.reads))

    def test_tickets_are_read_in_parallel(self):
        gate = threading.Barrier(3, timeout=2)
        routes = self.busy_repo()
        for n in (3, 4, 6):
            def comments():
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    return GitHubError("read alone: the tickets were read one at a time", 500, "")
                return [ask()]
            routes["/repos/%s/issues/%d/comments" % (REPO, n)] = comments
        gh = GitHub("t", transport=GitHubLists(routes))
        text, code = needs_you.run(gh, [REPO], TRUSTED, [OP])
        self.assertEqual((0, "Needs you: 3"), (code, text.split("\n")[0]))



class ETags(GitHubLists):
    """GitHubLists that answers an If-None-Match naming the answer it would
    send with a 304, as GitHub does for one token. `token_bound` answers
    every read whole instead: on a private repo the last run's ETags were
    another token's."""

    def __init__(self, routes, token_bound=False):
        super().__init__(routes)
        self.token_bound = token_bound
        self.answers = []

    def __call__(self, method, url, headers, body):
        status, head, raw = super().__call__(method, url, headers, body)
        tag = '"%s"' % hashlib.sha256(raw + head.get("link", "").encode()).hexdigest()
        if not self.token_bound and headers.get("If-None-Match") == tag:
            self.answers.append((url, 304))
            return 304, dict(head, etag=tag), b""
        self.answers.append((url, status))
        return status, dict(head, etag=tag), raw


class KeptBetweenRuns(unittest.TestCase):
    """A second run within the cache's life reads only what changed
    (loom#2394): Muse's ticks left the heaviest repo unread at the deadline
    because every run read every asking ticket's comments and events again."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = home.name
        items = [dict(issue(n), updated_at="2030-01-02T00:00:00Z") for n in (3, 4)]
        self.routes = dict(routes_of(items[0], [ask()]), **routes_of(items[1], [ask()]))
        self.routes["/repos/%s/issues" % REPO] = items

    def main(self, fake, *argv):
        out = io.StringIO()
        env = {"LOOM_BRIEF_AUTHORS": OP, "LOOM_REPOS": REPO, "LOOM_HOME": self.home}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), \
                mock.patch.object(needs_you, "auth_from_env", return_value="t"), \
                mock.patch.object(needs_you, "_short_transport", fake, create=True):
            code = needs_you.main(list(argv))
        return out.getvalue(), code

    @staticmethod
    def kinds(fake):
        return sorted(urlsplit(u).path.rsplit("/", 1)[-1] for u in fake.reads)

    def test_an_unchanged_ticket_is_not_read_again(self):
        first = ETags(self.routes)
        text, code = self.main(first)
        self.assertEqual((0, "Needs you: 2"), (code, text.split("\n")[0]))
        self.assertEqual(["comments", "comments", "events", "events", "issues",
                          "timeline", "timeline"], self.kinds(first))
        again = ETags(self.routes)
        self.assertEqual((text, 0), self.main(again))
        # The listing and each timeline are asked with the ETag kept last run
        # and answered 304; the comments and events are not asked at all.
        self.assertEqual(["issues", "timeline", "timeline"], self.kinds(again))
        self.assertEqual([304, 304, 304], [status for _, status in again.answers])

    def test_with_another_token_an_unchanged_ticket_is_still_not_read(self):
        self.main(ETags(self.routes))
        again = ETags(self.routes, token_bound=True)
        text, code = self.main(again)
        self.assertEqual((0, "Needs you: 2"), (code, text.split("\n")[0]))
        self.assertEqual(["issues", "timeline", "timeline"], self.kinds(again))

    def test_a_ticket_that_changed_is_read_again(self):
        self.main(ETags(self.routes))
        base = "/repos/%s/issues/3" % REPO
        self.routes[base + "/comments"] = [ask(), said("Still thinking.", ident=2)]
        self.routes["/repos/%s/issues" % REPO][0]["updated_at"] = "2030-01-03T00:00:00Z"
        again = ETags(self.routes)
        text, _ = self.main(again)
        read = [urlsplit(u).path for u in again.reads]
        self.assertIn(base + "/comments", read)
        self.assertIn(base + "/events", read)
        self.assertNotIn("/repos/%s/issues/4/comments" % REPO, read)
        self.assertIn("latest comment: %s at 2030-01-03T00:00:00Z" % OP, text)

    def test_a_ticket_with_no_updated_at_is_read_every_run(self):
        for item in self.routes["/repos/%s/issues" % REPO]:
            del item["updated_at"]
        self.main(ETags(self.routes))
        again = ETags(self.routes)
        self.main(again)
        self.assertEqual(2, self.kinds(again).count("comments"))

    def test_an_answer_unused_for_a_day_is_removed(self):
        folder = needs_you.cache_dir({"LOOM_HOME": self.home})
        os.makedirs(folder)
        stale = os.path.join(folder, "old.json")
        with open(stale, "w") as fh:
            fh.write("{}")
        os.utime(stale, (time.time() - 2 * 86400,) * 2)
        self.main(ETags(self.routes))
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.listdir(folder))

    def test_workers_is_a_setting(self):
        seen = {}

        def run(*args, **kwargs):
            seen.update(kwargs)
            return "Needs you: nothing\n", 0
        with mock.patch.object(needs_you, "run", run):
            self.main(ETags({}), "--workers", "24")
        self.assertEqual(24, seen["workers"])
        with mock.patch.object(needs_you, "run", run):
            self.main(ETags({}))
        self.assertEqual(needs_you.WORKERS, seen["workers"])


    def test_workers_under_one_is_refused(self):
        # Elder on PR 2409: the --workers floor had no test.
        for bad in ("0", "-1"):
            err = io.StringIO()
            with mock.patch.object(needs_you, "run") as run, \
                    contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                needs_you.main(["--workers", bad])
            self.assertEqual(2, stop.exception.code)
            self.assertIn("--workers must be at least 1", err.getvalue())
            run.assert_not_called()

class Order(unittest.TestCase):
    """The heaviest repo's tickets are read first (loom#2394): its items
    were found last and trailed past the deadline."""

    def test_the_repo_with_most_open_issues_has_its_tickets_read_first(self):
        light, heavy = "example-owner/light", "example-owner/heavy"
        routes = {"/repos/%s/issues" % light: [issue(1)],
                  "/repos/%s/issues" % heavy: [issue(n) for n in (7, 8)] + [
                      issue(n, labels=("loom:ticket",)) for n in range(10, 160)]}
        for repo, numbers in ((light, (1,)), (heavy, (7, 8))):
            for n in numbers:
                base = "/repos/%s/issues/%d" % (repo, n)
                routes.update({base + "/comments": [ask()], base + "/events": [added()],
                               base + "/timeline": []})
        fake = GitHubLists(routes)
        text, code = needs_you.run(GitHub("t", transport=fake), [light, heavy], TRUSTED, [OP],
                                   workers=1)
        self.assertEqual(0, code)
        firsts = [urlsplit(u).path for u in fake.reads if u.split("?")[0].endswith("/comments")]
        self.assertEqual(["/repos/%s/issues/%d/comments" % (heavy, 7),
                          "/repos/%s/issues/%d/comments" % (heavy, 8),
                          "/repos/%s/issues/%d/comments" % (light, 1)], firsts)

    def test_drain_starts_the_heaviest_task_first_and_keeps_order_within_a_weight(self):
        ran = []

        def task(name):
            return lambda: ran.append(name) or []
        late = needs_you.drain([("a", task("a"), 1), ("b", task("b"), 5), ("c", task("c")),
                                ("d", task("d"), 5), ("e", task("e"), needs_you.FIRST)], 1, 5)
        self.assertEqual([], late)
        self.assertEqual(["e", "b", "d", "a", "c"], ran)


class Pages(unittest.TestCase):
    """GitHub.page_plan and read_page, the pages needs_you reads on its own
    threads."""

    def test_the_final_page_reads_on_when_the_list_grew_after_the_first(self):
        rows = [issue(n, labels=("loom:ticket",)) for n in range(1, 251)]
        calls = []

        def listing():  # 150 open when page 1 is read, 250 by the last page
            calls.append(1)
            return rows[:150] if len(calls) == 1 else rows
        gh = GitHub("t", transport=GitHubLists({"/repos/%s/issues" % REPO: listing}))
        first, later = gh.page_plan("/repos/%s/issues?state=open&per_page=100" % REPO)
        self.assertEqual(100, len(first))
        self.assertEqual(1, len(later))
        found = gh.read_page(later[-1], final=True)
        self.assertEqual(list(range(101, 251)), [i["number"] for i in found])

    def test_a_page_not_final_does_not_read_on(self):
        rows = [issue(n, labels=("loom:ticket",)) for n in range(1, 251)]
        fake = GitHubLists({"/repos/%s/issues" % REPO: rows})
        gh = GitHub("t", transport=fake)
        _, later = gh.page_plan("/repos/%s/issues?state=open&per_page=100" % REPO)
        self.assertEqual(list(range(101, 201)), [i["number"] for i in gh.read_page(later[0])])
        self.assertEqual(2, len(fake.reads))

if __name__ == "__main__":
    unittest.main()
