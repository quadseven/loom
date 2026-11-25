"""A GitHub outage ends a station pass instead of failing tickets (#3567)."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bus import station  # noqa: E402
from bus.github import GitHub, GitHubError, GitHubOutage, RateLimited  # noqa: E402

REPO = "example-owner/example-repo"
PATH = "/repos/%s/issues/7" % REPO


class Fake:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def __call__(self, method, url, headers, body):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def client(answer, aware=True):
    fake = Fake(answer)
    gh = GitHub("t", transport=fake)
    gh._sleep = lambda s: None
    gh.outage_aware = aware
    return gh, fake


class OutageAtTheClient(unittest.TestCase):
    def test_a_5xx_raises_an_outage_that_a_rate_limit_handler_ends_the_pass_on(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                gh, _ = client((status, {}, b"{}"))
                with self.assertRaises(RateLimited) as ctx:
                    gh.rest("GET", PATH)
                self.assertIsInstance(ctx.exception, GitHubOutage)
                self.assertEqual(ctx.exception.status, status)

    def test_a_timeout_or_dropped_connection_is_an_outage(self):
        gh, _ = client(GitHubError("request failed: TimeoutError()", 0, ""))
        with self.assertRaises(GitHubOutage):
            gh.rest("GET", PATH)

    def test_the_outage_flag_fails_every_call_without_a_request(self):
        gh, fake = client((200, {}, b"{}"))
        gh.outage = True
        with self.assertRaises(GitHubOutage):
            gh.rest("GET", PATH)
        self.assertEqual(fake.calls, 0)

    def test_calls_resume_once_github_answers_and_the_flag_clears(self):
        gh, fake = client((200, {}, b'{"number": 7}'))
        gh.outage = True
        with self.assertRaises(GitHubOutage):
            gh.rest("GET", PATH)
        gh.outage = False
        self.assertEqual(gh.rest("GET", PATH).status, 200)

    def test_a_client_that_is_not_outage_aware_keeps_the_plain_error(self):
        gh, _ = client((500, {}, b"{}"), aware=False)
        try:
            gh.rest("GET", PATH)
        except GitHubError as err:
            self.assertNotIsInstance(err, GitHubOutage)

    def test_a_4xx_is_not_an_outage(self):
        gh, _ = client((404, {}, b'{"message": "Not Found"}'))
        try:
            gh.rest("GET", PATH)
        except GitHubError as err:
            self.assertNotIsInstance(err, GitHubOutage)


class OutageAtTheStation(unittest.TestCase):
    def test_a_pass_in_an_outage_logs_one_outage_line_and_marks_the_pass_done_without_failing_anything(self):
        def pass_(self):
            raise GitHubOutage("GET /x -> 500: GitHub outage", 500, "", 0)
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(station.Station, "run_pass", pass_), \
                mock.patch.object(station.Station, "end_pass", lambda self: None), \
                mock.patch.object(station, "_trust_app_login", lambda s, gh: None), \
                contextlib.redirect_stderr(err):
            gh = mock.Mock(cache_dir="set", app=None)
            code = station.run(station.Settings(os.path.join(tmp, "loom"), [REPO], [], []), gh)
        self.assertEqual(code, 0)
        self.assertTrue(gh.outage_aware)
        lines = [l for l in err.getvalue().splitlines() if "GitHub outage" in l]
        self.assertEqual(len(lines), 1, err.getvalue())


if __name__ == "__main__":
    unittest.main()
