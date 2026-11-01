"""bus.brain resume and status return inside their --deadline plus a small
fixed margin, even with reads still in flight (loom#1578).

On 2026-11-01 a Muse tick ran resume over 19 repos at --deadline 1.5 and the
command ran past the tick's 100 s timeout: it printed 4 repos and named 15
skipped, then hung. Two things held it. Each repo's reads ran their parts in
a thread pool, and Python joins a pool's workers at exit even when the
thread that started them is a daemon, so the process waited for every read
already sent. And the sections across repos were read after the deadline
with no clock of their own. These tests block reads well past the deadline
and time the return, in process and as a whole command."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_handoff import (ALLOWED, LOCK_REPO, REPO, REPO2, Routes,  # noqa: E402
                          lock_routes)
from test_status_deadline import (AUTHORS, ENV, HEAVY, LANES, LIGHT, NOW,  # noqa: E402
                                  Fake)

# A blocked read waits this long: far past every deadline below.
BLOCKED = 20.0
# What a run may take past deadline + brain.PAST_DEADLINE_SECONDS: thread
# start-up and the fake's own work, not a read.
SLACK = 2.0


def _routes() -> dict:
    """Two repos with one goal each, as the resume tests read them."""
    routes = lock_routes()
    for number, repo in ((1, REPO), (2, REPO2)):
        goal = {"number": number, "title": "Goal %d" % number, "state": "open",
                "labels": [{"name": "loom:goal"}]}
        routes[("GET", "/repos/%s/issues" % repo)] = (200, [goal])
        routes[("GET", "/repos/%s/issues/%d/comments" % (repo, number))] = (200, [])
    routes[("GET", "/repos/%s/issues/99" % REPO)] = (200, {"title": "a ticket"})
    return routes


class InProcessBound(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(sys, "stderr", io.StringIO())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        # A shorter margin keeps these quick; the bound is measured against it.
        margin = mock.patch.object(brain, "PAST_DEADLINE_SECONDS", 1.0)
        margin.start()
        self.addCleanup(margin.stop)

    def test_status_returns_inside_the_bound_when_the_lane_status_read_hangs(self) -> None:
        # Every repo is read; the lane status, read after the deadline in
        # the sections across repos, hangs. Status still returns inside the
        # bound, names the sections it skipped, and ends partial.
        fake = Fake(hold=(LANES,))
        fake.release = self.release
        missed: list = []
        deadline = 0.5 / 60
        started = time.monotonic()
        text = brain.status(GitHub("t", transport=fake), [LIGHT, HEAVY], LANES, env=ENV,
                            now=NOW, authors=AUTHORS, deadline=deadline, missed=missed)
        took = time.monotonic() - started
        self.assertLess(took, deadline * 60 + brain.PAST_DEADLINE_SECONDS + SLACK, took)
        self.assertIn("## %s" % LIGHT, text)
        self.assertIn("## %s" % HEAVY, text)
        self.assertIn("status skipped the sections across repos: deadline", text)
        self.assertEqual(["sections across repos"], missed)
        self.assertEqual("status partial: not read this run: sections across repos",
                         text.splitlines()[-1])

    def test_resume_returns_inside_the_bound_when_a_tail_read_hangs(self) -> None:
        # The sections across repos read a sensitive ticket's title after the
        # deadline; that read hangs. Resume returns inside the bound and
        # names the sections it skipped.
        routes = Routes(_routes())
        release = self.release

        def transport(method, url, headers, body):
            if url.endswith("/repos/%s/issues/99" % REPO):
                release.wait(BLOCKED)
            return routes(method, url, headers, body)

        def sensitive(gh, repos, now, env, ready=None):
            gh.rest("GET", "/repos/%s/issues/99" % REPO)
            return ["", "## Sensitive pull requests", "- none"]

        missed: list = []
        deadline = 0.5 / 60
        started = time.monotonic()
        with mock.patch.object(brain, "_sensitive_resume_lines", sensitive):
            text = brain.resume(GitHub("t", transport=transport), [REPO, REPO2], "lane-machine",
                                LOCK_REPO, NOW, ALLOWED, deadline=deadline, missed=missed)
        took = time.monotonic() - started
        self.assertLess(took, deadline * 60 + brain.PAST_DEADLINE_SECONDS + SLACK, took)
        self.assertIn("## %s#1 Goal 1" % REPO, text)
        self.assertIn("## %s#2 Goal 2" % REPO2, text)
        self.assertIn("resume skipped the sections across repos: deadline", text)
        self.assertEqual(["sections across repos"], missed)

    def test_a_tail_that_finishes_in_time_still_prints(self) -> None:
        # The bound only cuts a tail that runs past it.
        fake = Fake()
        missed: list = []
        text = brain.status(GitHub("t", transport=fake), [LIGHT, HEAVY], LANES, env=ENV,
                            now=NOW, authors=AUTHORS, deadline=1, missed=missed)
        self.assertEqual([], missed)
        self.assertIn("## Lane status", text)
        self.assertNotIn("sections across repos", text)

    def test_read_each_keeps_order_and_raises_the_first_failure_in_order(self) -> None:
        def fn(n):
            if n in (2, 3):
                raise ValueError(n)
            return n * 10
        self.assertEqual([0, 10, 20], brain._read_each(lambda n: n * 10, [0, 1, 2]))
        with self.assertRaisesRegex(ValueError, "2"):
            brain._read_each(fn, [0, 1, 2, 3])


# The whole command, as a Muse tick runs it: a fresh interpreter whose exit
# is timed. A read blocked in a repo's own parts (the second repo's goal
# tree, read beside its other parts) must not hold the exit.
CHILD = textwrap.dedent("""
    import sys, threading
    sys.path[:0] = [%(root)r, %(tests)r]
    from unittest import mock
    from bus import brain
    from bus.github import GitHub
    from test_handoff import Routes, REPO, REPO2, LOCK_REPO
    from test_read_deadline_bound import _routes
    routes = Routes(_routes())
    def transport(method, url, headers, body):
        if "/repos/%%s/issues/2/sub_issues" %% REPO2 in url:
            threading.Event().wait(%(blocked)r)
        return routes(method, url, headers, body)
    with mock.patch.object(brain, "GitHub", lambda *a, **kw: GitHub("t", transport=transport)), \\
            mock.patch.object(brain, "auth_from_env", lambda: "t"), \\
            mock.patch.object(brain, "_print_clock", lambda *a: None):
        code = brain.main(["resume", REPO, REPO2, "--holder", "lane-machine",
                           "--lock-repo", LOCK_REPO, "--deadline", %(deadline)r])
    sys.exit(code)
""")


class CommandBound(unittest.TestCase):
    def test_the_resume_command_exits_inside_the_bound_with_a_read_in_flight(self) -> None:
        deadline = 1.0 / 60
        script = CHILD % {"root": ROOT, "tests": TESTS, "blocked": BLOCKED,
                          "deadline": str(deadline)}
        env = dict(os.environ, LOOM_BRIEF_AUTHORS="allowed-bot", LOOM_SHADOW="0")
        started = time.monotonic()
        done = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=BLOCKED + 30, check=False)
        took = time.monotonic() - started
        self.assertEqual(brain.RESUME_PARTIAL_EXIT, done.returncode, done.stderr[-2000:])
        self.assertIn("resume skipped %s: deadline" % REPO2, done.stdout)
        self.assertIn("## %s#1 Goal 1" % REPO, done.stdout)
        self.assertIn("resume partial: not read this run: %s" % REPO2, done.stderr)
        # Interpreter start-up and imports are part of `took`, so the slack
        # is wider than in process, and still far under the blocked read.
        self.assertLess(took, deadline * 60 + brain.PAST_DEADLINE_SECONDS + 3 * SLACK, took)


if __name__ == "__main__":
    unittest.main()
