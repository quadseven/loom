"""bus.brain status and resume print each repo as it finishes, and name a repo
whose read runs past the per-repo budget as timed out (loom#2049)."""
from __future__ import annotations

import io
import os
import sys
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import audit, brain, per_repo
from bus.github import GitHubError

FAST = "example-owner/fast"
HANG = "example-owner/hang"
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
ENV = {"LOOM_BRIEF_AUTHORS": "allowed-bot"}
BUDGET = 0.3


class Stuck:
    """A read that never returns while the test runs; released at teardown so
    its daemon thread ends."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def wait(self) -> None:
        self.release.wait()


def quiet_stderr(test: unittest.TestCase) -> io.StringIO:
    """Keep the skipped lines status and resume write to stderr out of the
    test output, where the suite runner would read them as skipped tests."""
    patcher = mock.patch.object(sys, "stderr", io.StringIO())
    err = patcher.start()
    test.addCleanup(patcher.stop)
    return err


class EachTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stuck = Stuck()
        self.addCleanup(self.stuck.release.set)

    def test_a_read_that_never_returns_is_timed_out_and_the_run_completes(self) -> None:
        def read(repo: str) -> str:
            if repo == HANG:
                self.stuck.wait()
            return "read " + repo
        started = time.monotonic()
        got = list(per_repo.each([HANG, FAST], read, BUDGET, workers=4))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(got, [(FAST, True, "read " + FAST), (HANG, False, None)])

    def test_queued_repos_still_run_when_every_worker_is_stuck(self) -> None:
        def read(repo: str) -> str:
            if repo == HANG:
                self.stuck.wait()
            return repo
        got = list(per_repo.each([HANG, FAST], read, BUDGET, workers=1))
        self.assertCountEqual(got, [(HANG, False, None), (FAST, True, FAST)])

    def test_an_error_is_raised_when_its_repo_comes_up(self) -> None:
        def read(repo: str) -> str:
            raise ValueError("bad " + repo)
        with self.assertRaisesRegex(ValueError, "bad"):
            list(per_repo.each([FAST], read, BUDGET, workers=2))

    def test_budget_reads_the_flag_then_the_environment(self) -> None:
        self.assertEqual(per_repo.budget({}, None), per_repo.DEFAULT_BUDGET)
        self.assertEqual(per_repo.budget({per_repo.BUDGET_ENV: "45"}, None), 45.0)
        self.assertEqual(per_repo.budget({per_repo.BUDGET_ENV: "soon"}, None), per_repo.DEFAULT_BUDGET)
        self.assertEqual(per_repo.budget({per_repo.BUDGET_ENV: "45"}, 7), 7.0)
        with self.assertRaises(ValueError):
            per_repo.budget({}, 0)


class StatusStreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.err = quiet_stderr(self)
        self.stuck = Stuck()
        self.addCleanup(self.stuck.release.set)
        self.parts: list[str] = []

        def section(gh, repo, *args, **kwargs):
            if repo == HANG:
                self.stuck.wait()
            return ["", "## %s" % repo, "", "### Pull requests", "- none"]
        for name, value in (("_readable_section", section),
                            ("repo_weights", lambda *a, **k: {}),
                            ("_lane_status", lambda *a, **k: "no lane status yet")):
            patcher = mock.patch.object(brain, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_repo_that_never_returns_is_named_and_the_rest_prints(self) -> None:
        started = time.monotonic()
        text = brain.status(None, [HANG, FAST], env=ENV, now=NOW, authors=["allowed-bot"],
                            emit=self.parts.append, budget=BUDGET)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("## %s" % FAST, text)
        self.assertIn("- skipped %s: timed out after 0.3s" % HANG, text)
        self.assertIn("## Lane status", text)
        self.assertIn("skipped %s: timed out" % HANG, self.err.getvalue())
        # The parts, printed in turn, are the whole text: heading, the repo
        # that finished, the timed-out repo, then the cross-repo sections.
        self.assertEqual("\n".join(self.parts), text)
        self.assertTrue(self.parts[0].startswith("# Loom status"))
        self.assertIn("## %s" % FAST, self.parts[1])
        self.assertIn("timed out", self.parts[2])
        self.assertIn("## Runaway tickets", self.parts[3])

    def test_a_section_prints_before_a_slower_repo_finishes(self) -> None:
        # The slow repo finishes only once the fast repo's section is out: a
        # status that held every section to the end would time it out.
        printed = threading.Event()

        def section(gh, repo, *args, **kwargs):
            if repo == HANG:
                printed.wait(5)
            return ["", "## %s" % repo]

        def emit(part: str) -> None:
            self.parts.append(part)
            if "## %s" % FAST in part:
                printed.set()
        with mock.patch.object(brain, "_readable_section", section):
            text = brain.status(None, [HANG, FAST], env=ENV, now=NOW, authors=["allowed-bot"],
                                emit=emit, budget=3)
        self.assertNotIn("timed out", text)
        self.assertIn("## %s" % FAST, self.parts[1])
        self.assertIn("## %s" % HANG, self.parts[2])


class ResumeStreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.err = quiet_stderr(self)
        self.stuck = Stuck()
        self.addCleanup(self.stuck.release.set)
        self.parts: list[str] = []
        self.hang_in = ""

        def journals(gh, repos, authors, skipped=None):
            if repos == [HANG] and self.hang_in == "journals":
                self.stuck.wait()
            return []

        def sensitive(gh, repo, now, check):
            if repo == HANG and self.hang_in == "items":
                self.stuck.wait()
            return []
        for target, name, value in (
                (brain, "_lock_holder", lambda *a: None),
                (brain, "_goal_journals", journals),
                (brain, "_ready_sensitive_in", sensitive),
                (brain, "_verify_due_in", lambda *a: []),
                (audit, "due", lambda *a: [])):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_resume(self) -> str:
        started = time.monotonic()
        text = brain.resume(None, [HANG, FAST], "tester", "example-owner/loom", now=NOW,
                            env=ENV, emit=self.parts.append, budget=BUDGET)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual("\n".join(self.parts), text)
        return text

    def test_a_repo_whose_journals_never_return_is_named_and_left_out(self) -> None:
        self.hang_in = "journals"
        text = self.run_resume()
        self.assertIn("## Timed-out repos", text)
        self.assertIn("- skipped %s: timed out after 0.3s" % HANG, text)
        self.assertIn("## Lane proof audit", text)

    def test_a_repo_whose_items_never_return_is_named_and_the_run_completes(self) -> None:
        self.hang_in = "items"
        text = self.run_resume()
        self.assertIn("- skipped %s: timed out after 0.3s" % HANG, text)
        self.assertTrue(self.parts[0].startswith("# Resume for tester"))
        self.assertIn("## Sensitive pull requests", self.parts[-1])
        self.assertIn("## Lane proof audit", self.parts[-1])


class SecondTryTest(unittest.TestCase):
    """On 2026-10-31 one slow or dropped read cost a remote brain the loom
    section of status and all of resume: a repo gets one more try, and one
    repo's dropped connection does not end the run."""

    def setUp(self) -> None:
        self.stuck = Stuck()
        self.addCleanup(self.stuck.release.set)
        self.calls: list = []

    def test_a_slow_read_is_read_again_with_a_fresh_budget(self) -> None:
        def read(repo):
            self.calls.append(repo)
            if len(self.calls) == 1:
                self.stuck.wait()
            return "answer"
        got = list(per_repo.each([FAST], read, 0.1, workers=2))
        self.assertEqual([(FAST, True, "answer")], got)
        self.assertEqual(2, len(self.calls))

    def test_a_dropped_read_is_read_again(self) -> None:
        def read(repo):
            self.calls.append(repo)
            if len(self.calls) == 1:
                raise GitHubError("request failed: RemoteDisconnected", 0, "")
            return "answer"
        self.assertEqual([(FAST, True, "answer")],
                         list(per_repo.each([FAST], read, 5, workers=2)))

    def test_a_connection_that_stays_down_gives_up_that_repo_alone(self) -> None:
        down = GitHubError("request failed: RemoteDisconnected", 0, "")

        def read(repo):
            if repo == HANG:
                raise down
            return "answer"
        got = dict((repo, (done, value)) for repo, done, value
                   in per_repo.each([HANG, FAST], read, 5, workers=2))
        self.assertEqual((False, down), got[HANG])
        self.assertEqual((True, "answer"), got[FAST])
        self.assertIn("connection failed twice", per_repo.not_read(5, down))

    def test_at_the_deadline_every_repo_not_read_is_handed_back_in_order(self) -> None:
        def read(repo):
            if repo != FAST:
                self.stuck.wait()
            return "answer"
        started = time.monotonic()
        got = list(per_repo.each([HANG, FAST, "example-owner/third"], read, 30, workers=4,
                                 until=time.monotonic() + 0.2))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([(FAST, True, "answer")], got[:1])
        self.assertEqual([HANG, "example-owner/third"], [repo for repo, _, _ in got[1:]])
        for _, done, err in got[1:]:
            self.assertFalse(done)
            self.assertIsInstance(err, per_repo.PastDeadline)
            self.assertEqual("deadline", per_repo.not_read(30, err))

    def test_a_deadline_already_past_reads_nothing(self) -> None:
        got = list(per_repo.each([FAST], lambda repo: self.calls.append(repo), 5, workers=2,
                                 until=time.monotonic() - 1))
        self.assertEqual([], self.calls)
        self.assertEqual([FAST], [repo for repo, done, _ in got if not done])

    def test_a_refusal_still_ends_the_run(self) -> None:
        def read(repo):
            self.calls.append(repo)
            raise GitHubError("forbidden", 403, "")
        with self.assertRaises(GitHubError):
            list(per_repo.each([FAST], read, 5, workers=2))
        self.assertEqual(1, len(self.calls))


class ReadsSideBySideTest(unittest.TestCase):
    """The cause of the slow status: each pull request's reads ran one after
    another. Two lines that wait for each other only finish side by side."""

    def test_pull_request_lines_read_side_by_side(self) -> None:
        meet = threading.Barrier(2, timeout=5)

        def line(gh, repo, pr, *args):
            meet.wait()
            return "- #%s" % pr["number"]
        with mock.patch.object(brain, "_pull_request_line", line):
            got = brain._pull_request_lines(None, FAST, [{"number": 1}, {"number": 2}], {}, "bot")
        self.assertEqual(got, ["### Pull requests", "- #1", "- #2"])

    def test_histories_read_side_by_side(self) -> None:
        meet = threading.Barrier(2, timeout=5)

        def history(gh, repo, issue, *args):
            meet.wait()
            return issue["number"]
        found = [{"number": n, "labels": [{"name": "loom:failed"}], "updated_at": str(n)}
                 for n in (1, 2)]
        with mock.patch.object(brain, "_history", history):
            got, skipped = brain._histories(None, FAST, found, NOW)
        self.assertEqual(got, {1: 1, 2: 2})
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()


class SideBySideKeepsPerItemOutcomesTest(unittest.TestCase):
    """Reading items side by side keeps each item's outcome as the loop that
    read them in turn had it (loom#2058 review): an item GitHub will not show
    is named and the others are read; any other error still ends the read,
    as loom#800 requires."""

    @staticmethod
    def marks(*numbers: int) -> list:
        return [types.SimpleNamespace(item=(FAST, n)) for n in numbers]

    def test_an_unreadable_item_among_several_is_named_and_the_others_read_in_order(self) -> None:
        def revision(gh, repo, number):
            if number == 2:
                raise GitHubError("gone", 404, '{"message": "Not Found"}')
            return "rev-%d" % number
        with mock.patch.object(brain, "item_revision", revision):
            current, gone = brain._item_revisions(None, self.marks(1, 2, 3, 1), {})
        self.assertEqual(list(current.items()), [((FAST, 1), "rev-1"), ((FAST, 3), "rev-3")])
        self.assertEqual(gone, {(FAST, 2): "HTTP 404 Not Found"})

    def test_an_item_in_a_skipped_repo_is_not_read(self) -> None:
        with mock.patch.object(brain, "item_revision", mock.Mock(side_effect=AssertionError)):
            current, gone = brain._item_revisions(None, self.marks(1), {FAST: "HTTP 404 x"})
        self.assertEqual((current, gone), ({}, {(FAST, 1): "HTTP 404 x"}))

    def test_any_other_github_error_still_ends_the_item_read(self) -> None:
        def revision(gh, repo, number):
            if number == 2:
                raise GitHubError("server", 502, "")
            return "rev"
        with mock.patch.object(brain, "item_revision", revision):
            with self.assertRaises(GitHubError):
                brain._item_revisions(None, self.marks(1, 2, 3), {})

    def test_a_history_error_ends_the_histories_read_as_the_loop_did(self) -> None:
        # _history never caught a GitHubError per ticket: the old dict
        # comprehension let the first one end the repo's read, so a fault
        # never reads as a ticket with no history. That stays.
        def history(gh, repo, issue, *args):
            if issue["number"] == 2:
                raise GitHubError("gone", 404, "")
            return issue["number"]
        found = [{"number": n, "labels": [{"name": "loom:failed"}], "updated_at": str(n)}
                 for n in (1, 2, 3)]
        with mock.patch.object(brain, "_history", history):
            with self.assertRaises(GitHubError):
                brain._histories(None, FAST, found, NOW)

    def test_histories_keep_the_ticket_order(self) -> None:
        def history(gh, repo, issue, *args):
            time.sleep(0.05 if issue["number"] == 1 else 0)
            return "h%d" % issue["number"]
        found = [{"number": n, "labels": [{"name": "loom:failed"}], "updated_at": str(n)}
                 for n in (1, 2, 3)]
        with mock.patch.object(brain, "_history", history):
            got, _ = brain._histories(None, FAST, found, NOW)
        self.assertEqual(list(got.items()), [(1, "h1"), (2, "h2"), (3, "h3")])
