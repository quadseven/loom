"""Status and resume skip a repo or item GitHub will not show (loom#800).

A resume ended part way through when one LOOM_REPOS entry had been moved:
the read answered HTTP 301 and the GitHubError ended the command, so the
brain got nothing for the other repos. A moved, gone or forbidden repo is
now named on a "skipped <repo>: <reason>" line; any other fault still ends
the run, so it never reads as an empty repo."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub, GitHubError, RateLimited, unreadable
from tests.test_handoff import (LOCK_REPO, NOW, PR, REPO, Routes, at, comment, defer,
                          journal, lock_routes)

MOVED = "example-owner/moved-repo"
ENV = {"LOOM_BRIEF_AUTHORS": "allowed-bot"}
MOVED_BODY = {"message": "Moved Permanently",
              "url": "https://api.github.com/repositories/1/issues"}


def quiet(fn, *args, **kwargs):
    """fn's result and what it wrote to stderr."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = fn(*args, **kwargs)
    return out, err.getvalue()


class UnreadableTests(unittest.TestCase):
    def test_moved_gone_and_forbidden_are_unreadable(self):
        moved = GitHubError("GET x -> 301", 301, json.dumps(MOVED_BODY))
        self.assertEqual("HTTP 301 Moved Permanently: renamed or transferred", unreadable(moved))
        self.assertEqual("HTTP 404 Not Found",
                         unreadable(GitHubError("x", 404, '{"message": "Not Found"}')))
        self.assertEqual("HTTP 403 Resource not accessible by integration", unreadable(
            GitHubError("x", 403, '{"message": "Resource not accessible by integration"}')))
        self.assertEqual("GraphQL NOT_FOUND", unreadable(
            GitHubError("graphql errors", 200, '[{"type": "NOT_FOUND"}]')))

    def test_a_budget_refusal_is_never_unreadable(self):
        # The client's own refusal while the window is spent is a 403 with
        # no body; read as forbidden, it named a rate-limited repo or item
        # as unreadable and dropped it (loom#1578).
        self.assertIsNone(unreadable(RateLimited("GET x: GitHub budget exhausted", 403, "", 1)))

    def test_a_fault_is_never_unreadable(self):
        for err in (GitHubError("x", 500, "boom"), GitHubError("x", 0, ""),
                    GitHubError("x", 403, '{"message": "API rate limit exceeded"}'),
                    GitHubError("x", 403, '{"message": "secondary rate limit"}'),
                    GitHubError("graphql errors", 200, '[{"type": "RATE_LIMITED"}]')):
            with self.subTest(status=err.status, body=err.body):
                self.assertIsNone(unreadable(err))


class ResumeSkipTests(unittest.TestCase):
    def routes(self, moved_status=301, moved_body=MOVED_BODY, pr_status=200):
        goal = {"number": 1, "title": "Goal", "state": "open", "labels": [{"name": "loom:goal"}]}
        comments = [comment(journal(5, "muse", defer(PR, "pr-abcdef123456"),
                                    defer((MOVED, 3), "pr-0123456789ab")))]
        routes = lock_routes()
        routes.update({
            ("GET", "/repos/%s/issues" % MOVED): (moved_status, moved_body),
            ("GET", "/repos/%s/issues/3" % MOVED): (moved_status, moved_body),
            ("GET", "/repos/%s/issues" % REPO): (200, [goal]),
            ("GET", "/repos/%s/issues/1/comments" % REPO): (200, comments),
            ("GET", "/repos/%s/issues/7" % REPO): (pr_status, {"state": "open", "pull_request": {}}),
            ("GET", "/repos/%s/pulls/7" % REPO): (200, {"state": "open",
                                                         "head": {"sha": "abcdef1234567890"}}),
        })
        return routes

    def resume(self, routes, repos=(MOVED, REPO)):
        return quiet(brain.resume, GitHub("t", transport=Routes(routes)), list(repos),
                     "lane-machine", LOCK_REPO, NOW, ENV)

    def test_a_moved_repo_is_named_and_the_others_finish(self):
        out, err = self.resume(self.routes())
        line = "skipped %s: HTTP 301 Moved Permanently: renamed or transferred" % MOVED
        self.assertIn("## Skipped repos (fix LOOM_REPOS)\n- " + line, out)
        self.assertIn(line, err)
        # The other repo's goal and its live merge handoff are all there.
        self.assertIn("- resume merge %s#7 (deferred by muse at %s)" % (REPO, at(5)), out)
        # The handoff item in the moved repo is named, not resumed or read.
        self.assertIn("- skipped %s#3: HTTP 301 Moved Permanently" % MOVED, out)
        self.assertNotIn("resume merge %s#3" % MOVED, out)
        self.assertIn("## Verify due", out)

    def test_a_repo_whose_pull_requests_answer_moved_is_named_once(self):
        routes = Routes(self.routes())

        def moved_pulls(method, url, headers, body):
            if "/repos/%s/pulls" % MOVED in url:
                return 301, {}, json.dumps(MOVED_BODY).encode()
            return routes(method, url, headers, body)

        out, _ = quiet(brain.resume, GitHub("t", transport=moved_pulls), [MOVED, REPO],
                       "lane-machine", LOCK_REPO, NOW, ENV)
        line = "- skipped %s: HTTP 301 Moved Permanently: renamed or transferred" % MOVED
        self.assertEqual(1, out.count(line))
        self.assertIn("## Sensitive pull requests", out)
        self.assertIn("- resume merge %s#7 (deferred by muse at %s)" % (REPO, at(5)), out)
        self.assertNotIn(("GET", "/repos/%s/issues" % MOVED), routes.calls)

    def test_a_handoff_item_that_answers_forbidden_is_named(self):
        routes = self.routes()
        routes[("GET", "/repos/%s/issues/7" % REPO)] = (403, {"message": "Must have admin rights"})
        out, _ = self.resume(routes, repos=(REPO,))
        self.assertIn("- skipped %s#7: HTTP 403 Must have admin rights" % REPO, out)
        self.assertNotIn("resume merge %s#7" % REPO, out)

    def test_a_fault_is_named_with_what_failed_and_the_others_finish(self):
        # loom#1578: a fault in one repo no longer ends resume with the repos
        # after it unread. It is never read as an empty repo either: the
        # repo is named under the failed repos with the error, and reported
        # as missed, which the command turns into a nonzero exit.
        missed = []
        out, _ = quiet(brain.resume, GitHub("t", transport=Routes(self.routes(
            moved_status=500, moved_body={"message": "boom"}))), [MOVED, REPO],
            "lane-machine", LOCK_REPO, NOW, ENV, missed=missed)
        self.assertIn("## Failed repos (read again next tick)\n- skipped %s: read failed: "
                      % MOVED, out)
        # The goal in the other repo defers an item in the faulting repo:
        # that item is named in its section with the fault, and the goal's
        # other item still resumes.
        self.assertIn("- skipped %s#3: read failed: GET /repos/%s/issues/3 -> 500"
                      % (MOVED, MOVED), out)
        self.assertEqual([MOVED, "%s#3" % MOVED], missed)
        self.assertIn("- resume merge %s#7 (deferred by muse at %s)" % (REPO, at(5)), out)
        self.assertNotIn("Skipped repos", out)

    def test_a_rate_limit_past_the_deadline_is_named_and_not_waited_out(self):
        missed = []
        routes = self.routes(moved_status=403, moved_body={"message": "API rate limit exceeded"})
        out, _ = quiet(brain.resume, GitHub("t", transport=Routes(routes)), [MOVED],
                       "lane-machine", LOCK_REPO, NOW, ENV, deadline=0.5, missed=missed)
        self.assertRegex(out, r"## Failed repos \(read again next tick\)\n- skipped %s: "
                              r"rate limited until \d{4}-\d\d-\d\dT[\d:]+Z, past the deadline"
                         % MOVED)
        self.assertEqual([MOVED], missed)
        self.assertNotIn("Skipped repos", out)


class StatusSkipTests(unittest.TestCase):
    def test_a_moved_repo_gets_a_skipped_line_and_the_others_finish(self):
        routes = {("GET", "/repos/%s/issues" % MOVED): (301, MOVED_BODY),
                  ("GET", "/repos/%s/issues" % REPO): (200, []),
                  ("GET", "/repos/%s/issues" % LOCK_REPO): (200, []),
                  # Each repo's size (repo_weights): the moved repo answers null.
                  ("POST", "/graphql"): (200, {"data": {"r0": None, "r1": {}},
                                               "errors": [{"type": "NOT_FOUND"}]})}
        out, err = quiet(brain.status, GitHub("t", transport=Routes(routes)), [MOVED, REPO],
                         LOCK_REPO, env=ENV)
        line = "skipped %s: HTTP 301 Moved Permanently: renamed or transferred" % MOVED
        self.assertIn("## %s\n\n- %s" % (MOVED, line), out)
        self.assertIn(line, err)
        self.assertIn("## %s\n\n### Goals" % REPO, out)
        self.assertIn("## Lane status", out)

    def fault_routes(self):
        from bus import lock
        return {("GET", "/repos/%s/issues" % MOVED): (500, {"message": "boom"}),
                ("GET", "/repos/%s/issues" % REPO): (200, []),
                ("GET", "/repos/%s/issues" % LOCK_REPO): (200, []),
                # The tick clock status prints first: no tick record yet.
                ("GET", "/repos/%s/git/ref/heads/%s" % (LOCK_REPO, lock.LOCK_BRANCH)):
                    (404, {"message": "Not Found"}),
                # Each repo's size (repo_weights): the moved repo answers null.
                ("POST", "/graphql"): (200, {"data": {"r0": None, "r1": {}},
                                             "errors": [{"type": "NOT_FOUND"}]})}

    def test_a_fault_is_named_as_failed_and_status_is_partial(self):
        # On main a fault ended status with the GitHubError (loom#800). It
        # is now named, as resume names it (loom#1578): the failing repo
        # with its error, the other repos read, the partial line last, and
        # the command exits 3 (loom#2415). A fault is still not a moved
        # repo: it is not skipped as gone.
        missed = []
        out, err = quiet(brain.status, GitHub("t", transport=Routes(self.fault_routes())),
                         [MOVED, REPO], LOCK_REPO, env=ENV, missed=missed)
        self.assertEqual([MOVED], missed)
        self.assertRegex(out, r"## %s\n\n- skipped %s: read failed: GET /repos/%s/issues\S* -> 500"
                         % (MOVED, MOVED, MOVED))
        self.assertIn("skipped %s: read failed:" % MOVED, err)
        self.assertNotIn("renamed or transferred", out)
        self.assertIn("## %s\n\n### Goals" % REPO, out)
        self.assertIn("## Lane status", out)
        self.assertEqual("status partial: not read this run: %s" % MOVED, out.splitlines()[-1])

    def test_a_fault_makes_the_command_exit_3(self):
        args = brain.parser().parse_args(["status", MOVED, REPO, "--lanes-repo", LOCK_REPO])
        stdout = io.StringIO()
        with unittest.mock.patch.dict(os.environ, ENV), contextlib.redirect_stdout(stdout):
            code, err = quiet(brain.COMMANDS["status"],
                              GitHub("t", transport=Routes(self.fault_routes())), args)
        self.assertEqual(3, code)
        self.assertIn("- skipped %s: read failed:" % MOVED, stdout.getvalue())
        self.assertIn("## %s\n\n### Goals" % REPO, stdout.getvalue())
        self.assertIn("status partial: not read this run: %s" % MOVED, err)
        self.assertIn("status partial: not read this run: %s" % MOVED,
                      stdout.getvalue().splitlines()[-1])

if __name__ == "__main__":
    unittest.main()
