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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub, GitHubError, unreadable
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

    def test_a_fault_still_ends_the_run(self):
        for status, body in ((500, {"message": "boom"}),
                             (403, {"message": "API rate limit exceeded"})):
            with self.subTest(status=status):
                with self.assertRaises(GitHubError):
                    self.resume(self.routes(moved_status=status, moved_body=body))


class StatusSkipTests(unittest.TestCase):
    def test_a_moved_repo_gets_a_skipped_line_and_the_others_finish(self):
        routes = {("GET", "/repos/%s/issues" % MOVED): (301, MOVED_BODY),
                  ("GET", "/repos/%s/issues" % REPO): (200, []),
                  ("GET", "/repos/%s/issues" % LOCK_REPO): (200, [])}
        out, err = quiet(brain.status, GitHub("t", transport=Routes(routes)), [MOVED, REPO],
                         LOCK_REPO, env=ENV)
        line = "skipped %s: HTTP 301 Moved Permanently: renamed or transferred" % MOVED
        self.assertIn("## %s\n\n- %s" % (MOVED, line), out)
        self.assertIn(line, err)
        self.assertIn("## %s\n\n### Goals" % REPO, out)
        self.assertIn("## Lane status", out)

    def test_a_fault_still_ends_status(self):
        routes = {("GET", "/repos/%s/issues" % MOVED): (500, {"message": "boom"}),
                  ("GET", "/repos/%s/issues" % LOCK_REPO): (200, [])}
        with self.assertRaises(GitHubError):
            quiet(brain.status, GitHub("t", transport=Routes(routes)), [MOVED], LOCK_REPO, env=ENV)


if __name__ == "__main__":
    unittest.main()
