"""bus.brain status reads the heaviest repo first, prints each repo as it is
read, and stops at a deadline inside a Muse tick (loom#2415).

On 2026-11-01 Muse's tick cut status off at 115 s with a partial read: two
repo sections printed, 17 not read, and nothing said which. These tests run
status over a fake GitHub of three repos, at the transport, so the request
count and the order of the reads are the real client's."""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub, GitHubError
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

HEAVY = "example-owner/heavy"
LIGHT = "example-owner/light"
SLOW = "example-owner/slow"
LANES = "example-owner/lanes"
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
ENV = {"LOOM_BRIEF_AUTHORS": "allowed-bot"}
AUTHORS = ["allowed-bot"]
# Open issues per repo: the heavy repo has the most to read.
SIZES = {HEAVY: 5, LIGHT: 1, SLOW: 2}
REPO = re.compile(r"^/repos/([^/]+/[^/]+)/")


GRUG = {"__typename": "Bot", "login": "grug-tribe"}


def _issue(number: int, labels: list, body: str = "") -> dict:
    return {"number": number, "id": 9000 + number, "state": "open",
            "title": "Issue %d" % number, "labels": [{"name": n} for n in labels],
            "body": body, "created_at": "2030-01-01T00:00:00Z",
            "updated_at": "2030-01-01T00:00:00Z"}


def _listing(repo: str) -> list:
    """A goal home, an epic, and SIZES[repo] tickets each with its lane pull
    request open: the heavier repo has more pull requests to read."""
    tickets = [_issue(10 + n, ["loom:ticket", "loom:pr"], "Part of #2\n")
               for n in range(SIZES[repo])]
    return [_issue(1, ["loom:goal"], "# Goal\n"), _issue(2, ["epic"], "Part of #1\n")] + tickets


def _pulls(repo: str) -> list:
    return [{"number": 100 + n, "title": "PR %d" % n, "head": {"ref": "loom/%d-work" % (10 + n)},
             "body": "Refs #%d" % (10 + n), "labels": [], "user": {"login": "example-lane"},
             "created_at": "2030-01-01T00:00:00Z"} for n in range(SIZES[repo])]


def _node() -> dict:
    return {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T10:45:00Z",
                                              "statusCheckRollup": {"state": "SUCCESS"}}}]},
            "reviews": {"nodes": [{"author": GRUG, "state": "APPROVED",
                                   "submittedAt": "2030-01-01T11:00:00Z"}]},
            "reviewThreads": {"nodes": []}}


class Fake:
    """GitHub at the transport: answers by path and counts each request.
    A repo in `hold` has every read wait until `release` is set; `order`
    records the repo of each request as it is sent."""

    def __init__(self, hold: tuple = ()) -> None:
        self.lock = threading.Lock()
        self.calls: Counter = Counter()
        self.order: list = []
        self.hold = set(hold)
        self.release = threading.Event()
        self.fault: dict = {}

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        found = REPO.match(path)
        repo = found.group(1) if found else ""
        query = json.loads(body or b"{}").get("query", "") if path == "/graphql" else ""
        if query.startswith("query { r0: repository("):  # every repo's size in one query
            with self.lock:
                self.calls[(method, path)] += 1
                self.order.append("")
            return self.reply({"data": {
                "r%s" % n: {"issues": {"totalCount": len(_listing(r)) - SIZES[r]},
                            "pullRequests": {"totalCount": SIZES[r]}}
                for n, r in ((n, "example-owner/" + name) for n, name in
                             re.findall(r'r(\d+): repository\(owner:"[^"]+", name:"([^"]+)"\)',
                                        query)) if r in SIZES}})
        if query:
            named = re.search(r'name:\s*"([^"]+)"', query)
            repo = next((r for r in SIZES if named and r.endswith("/" + named.group(1))), "")
        with self.lock:
            self.calls[(method, path)] += 1
            self.order.append(repo)
        if repo in self.hold:
            self.release.wait(10)
        if repo in self.fault:
            return self.fault[repo], {}, b'{"message": "Unprocessable"}'
        if repo in SIZES:
            if path.startswith("/repos/%s/issues?state=open" % repo):
                return self.reply(_listing(repo))
            if path.startswith("/repos/%s/pulls?state=open" % repo):
                return self.reply(_pulls(repo))
            if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+", path):
                return self.reply({"mergeable": True, "mergeable_state": "clean"})
            if path.endswith("/files") or "/files?" in path:
                return self.reply([{"filename": "bus/a.py", "patch": "+x"}])
            if path == "/graphql":
                return self.reply({"data": {"repository": {
                    "p%d" % pr["number"]: _node() for pr in _pulls(repo)}}})
        if "/git/ref/" in path:
            return 404, {}, b'{"message": "Not Found"}'
        if path == "/graphql":
            return self.reply({"data": {"repository": {}}})
        return self.reply([])

    @staticmethod
    def reply(body):
        return 200, {}, json.dumps(body).encode()


def quiet_stderr(test: unittest.TestCase) -> io.StringIO:
    patcher = mock.patch.object(sys, "stderr", io.StringIO())
    err = patcher.start()
    test.addCleanup(patcher.stop)
    return err


class StatusDeadlineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.err = quiet_stderr(self)
        self.parts: list = []

    def fake(self, hold: tuple = ()) -> Fake:
        fake = Fake(hold)
        self.addCleanup(fake.release.set)
        return fake

    def status(self, fake: Fake, repos, **kwargs) -> str:
        return brain.status(GitHub("t", transport=fake), repos, LANES, env=ENV, now=NOW,
                            authors=AUTHORS, emit=self.parts.append, **kwargs)

    def test_the_heaviest_repo_prints_before_the_light_ones_are_read(self) -> None:
        # One reader at a time, the heavy repo given last: its section is
        # read and printed before any light repo's section read starts.
        fake = self.fake()
        printed_at: dict = {}

        def emit(part: str) -> None:
            self.parts.append(part)
            for repo in SIZES:
                if "## %s" % repo in part:
                    printed_at[repo] = len(fake.order)
        with mock.patch.object(brain, "READ_WORKERS", 1):
            brain.status(GitHub("t", transport=fake), [LIGHT, SLOW, HEAVY], LANES, env=ENV,
                         now=NOW, authors=AUTHORS, emit=emit, deadline=1)
        self.assertEqual([HEAVY, SLOW, LIGHT], sorted(printed_at, key=printed_at.get))
        # One query weighs every repo; then the heavy repo's whole section
        # is read before the first request of another repo's section.
        self.assertEqual("", fake.order[0])
        sections = [repo for repo in fake.order[1:] if repo]
        self.assertEqual(HEAVY, sections[0])
        first_other = sections.index(next(r for r in sections if r != HEAVY))
        self.assertEqual(sections.count(HEAVY), first_other)

    def test_the_first_repo_prints_before_the_last_is_read(self) -> None:
        # The slow repo's section reads answer only once another repo's
        # section is out: a status that held its sections to the end would
        # be stopped at its deadline with the slow repo unread.
        fake = self.fake()
        printed = threading.Event()
        waiting = threading.Event()
        sent = Counter()
        seen: list = []

        def transport(method, url, headers, body):
            if "/repos/%s/" % SLOW in url:
                sent[SLOW] += 1
                if sent[SLOW] > 1:  # past its listing
                    waiting.set()
                    printed.wait(10)
            return fake(method, url, headers, body)

        def emit(part: str) -> None:
            self.parts.append(part)
            if "## example-owner/" in part and not printed.is_set():
                # The slow repo's read is still waiting when the first
                # section prints.
                waiting.wait(5)
                seen.append(waiting.is_set() and sent[SLOW] == 2)
                printed.set()
        text = brain.status(GitHub("t", transport=transport), [SLOW, LIGHT, HEAVY], LANES,
                            env=ENV, now=NOW, authors=AUTHORS, emit=emit, deadline=5 / 60)
        self.assertNotIn("status skipped", text)
        self.assertNotIn("status partial", text)
        heads = [p.split("\n")[1] for p in self.parts if p.startswith("\n## example-owner/")]
        self.assertEqual(3, len(heads))
        self.assertNotEqual("## %s" % SLOW, heads[0])
        self.assertEqual([True], seen)

    def test_past_the_deadline_the_unread_repos_are_named_and_the_run_is_partial(self) -> None:
        fake = self.fake(hold=())
        missed: list = []
        # The heavy repo's section reads hang once its listing is in.
        real = fake.__call__
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        listed = Counter()

        def transport(method, url, headers, body):
            path = url.split("api.github.com")[-1]
            if path.startswith("/repos/%s/" % HEAVY):
                listed[HEAVY] += 1
                if listed[HEAVY] > 1:
                    stuck.wait(10)
            return real(method, url, headers, body)
        started = time.monotonic()
        text = brain.status(GitHub("t", transport=transport), [LIGHT, HEAVY], LANES, env=ENV,
                            now=NOW, authors=AUTHORS, emit=self.parts.append,
                            deadline=0.5 / 60, missed=missed)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([HEAVY], missed)
        self.assertIn("## %s" % LIGHT, text)
        self.assertNotIn("## %s" % HEAVY, text)
        self.assertIn("status skipped %s: deadline" % HEAVY, text)
        # The cross-repo sections still print, and the last line says the
        # read is partial: a cut-off status never reads as complete.
        self.assertIn("## Lane status", text)
        self.assertEqual("status partial: not read this run: %s" % HEAVY, text.splitlines()[-1])
        self.assertEqual("\n".join(self.parts), text)

    def test_the_command_exits_3_and_names_the_unread_repos(self) -> None:
        fake = self.fake(hold=(HEAVY,))
        args = brain.parser().parse_args(
            ["status", LIGHT, HEAVY, "--lanes-repo", LANES, "--deadline", str(0.5 / 60)])
        out = io.StringIO()
        with mock.patch.dict(os.environ, ENV), mock.patch.object(sys, "stdout", out):
            code = brain.COMMANDS["status"](GitHub("t", transport=fake), args)
        self.assertEqual(brain.STATUS_PARTIAL_EXIT, code)
        self.assertEqual(3, code)
        self.assertIn("status skipped %s: deadline" % HEAVY, out.getvalue())
        self.assertIn("## %s" % LIGHT, out.getvalue())
        self.assertIn("status partial: not read this run: %s" % HEAVY, self.err.getvalue())

    def test_a_complete_read_exits_0_with_no_partial_line(self) -> None:
        args = brain.parser().parse_args(["status", LIGHT, HEAVY, "--lanes-repo", LANES])
        out = io.StringIO()
        with mock.patch.dict(os.environ, ENV), mock.patch.object(sys, "stdout", out):
            code = brain.COMMANDS["status"](GitHub("t", transport=self.fake()), args)
        self.assertEqual(0, code)
        self.assertNotIn("status partial", out.getvalue())

    def test_a_repo_github_answers_with_a_fault_is_named_and_the_rest_read(self) -> None:
        fake = self.fake()
        fake.fault[SLOW] = 422
        missed: list = []
        text = self.status(fake, [SLOW, LIGHT], deadline=1, missed=missed)
        self.assertEqual([SLOW], missed)
        self.assertIn("## %s" % LIGHT, text)
        self.assertIn("- skipped %s: read failed:" % SLOW, text)
        self.assertEqual("status partial: not read this run: %s" % SLOW, text.splitlines()[-1])

    def test_request_count_for_three_repos_is_pinned(self) -> None:
        # One GraphQL query weighs every repo; each listing is read once. A
        # change here is a change in what status reads: count it again and
        # say why in the pull request.
        fake = self.fake()
        self.status(fake, [LIGHT, SLOW, HEAVY], deadline=1)
        listings = [p for (m, p) in fake.calls if "/issues?state=open" in p
                    and not p.startswith("/repos/%s/" % LANES)]
        self.assertEqual(3, len(listings))
        self.assertTrue(all(fake.calls[("GET", p)] == 1 for p in listings))
        self.assertEqual(PINNED_REQUESTS, sum(fake.calls.values()))

    def test_heaviest_first_keeps_the_given_order_among_equals(self) -> None:
        self.assertEqual([HEAVY, LIGHT, SLOW],
                         brain.heaviest_first([LIGHT, SLOW, HEAVY, LIGHT],
                                              {HEAVY: 9, LIGHT: 1, SLOW: 1}))

    def test_repos_are_read_in_the_order_given_when_their_sizes_cannot_be_read(self) -> None:
        class Refused:
            def rest(self, method, path, body=None):
                raise GitHubError("POST /graphql -> 502", 502, "")
        self.assertEqual({}, brain.repo_weights(Refused(), [LIGHT, HEAVY]))
        self.assertEqual({}, brain.repo_weights(Refused(), [LIGHT]))
        self.assertEqual([LIGHT, HEAVY], brain.heaviest_first([LIGHT, HEAVY], {}))

    def test_a_repo_github_will_not_show_does_not_stop_the_others_being_weighed(self) -> None:
        class Partial:
            def rest(self, method, path, body=None):
                return type("Answer", (), {"data": {
                    "data": {"r0": None, "r1": {"issues": {"totalCount": 3},
                                                "pullRequests": {"totalCount": 4}}},
                    "errors": [{"type": "NOT_FOUND"}]}})()
        self.assertEqual({HEAVY: 7}, brain.repo_weights(Partial(), [LIGHT, HEAVY]))


# The requests status sends for the three fake repos and the lane status.
PINNED_REQUESTS = 60


if __name__ == "__main__":
    unittest.main()
