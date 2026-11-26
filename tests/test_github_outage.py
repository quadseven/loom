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
from bus.github import AppToken, GitHub, GitHubError, GitHubOutage, RateLimited  # noqa: E402

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


def client(answer, aware=True, app=False):
    fake = Fake(answer)
    auth = (AppToken("123", "456", lambda: "unused by the test signer",
                     sign=lambda *_: b"test signature") if app else "t")
    gh = GitHub(auth, transport=fake)
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

    def test_the_outage_flag_blocks_app_authentication_before_transport(self):
        gh, fake = client((500, {}, b"{}"), app=True)
        gh.outage = True
        for call in (gh.app_login, gh.app_token, lambda: gh.rest("GET", PATH)):
            with self.subTest(call=call), self.assertRaises(GitHubOutage):
                call()
        self.assertEqual(fake.calls, 0)

    def test_app_authentication_keeps_outages_distinct_from_ordinary_errors(self):
        for answer, error_type in (((500, {}, b"{}"), GitHubOutage),
                                   (GitHubError("timed out", 0, ""), GitHubOutage),
                                   ((401, {}, b"{}"), GitHubError)):
            for endpoint in ("app_login", "app_token"):
                with self.subTest(answer=answer, endpoint=endpoint):
                    gh, _ = client(answer, app=True)
                    with self.assertRaises(error_type) as failure:
                        getattr(gh, endpoint)()
                    self.assertIs(type(failure.exception), error_type)

    def test_an_outage_reading_repo_visibility_reaches_the_caller(self):
        gh, _ = client((500, {}, b"{}"))
        with self.assertRaises(GitHubOutage):
            gh._repo_is_private_for_post(REPO)
        self.assertNotIn(REPO, gh._repo_private_cache)

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
    def test_a_declared_outage_skips_the_station_heartbeat_and_pass(self):
        gh, fake = client((500, {}, b"{}"), aware=False, app=True)
        gh.outage = True
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(station.station_beat, "Guard") as heartbeat, \
                mock.patch.object(station.Station, "run_pass") as run_pass, \
                contextlib.redirect_stderr(err):
            settings = station.Settings(tmp, [REPO], [], [], status_repo=REPO)
            code = station.run(settings, gh)
        self.assertEqual(code, 0)
        heartbeat.assert_not_called()
        run_pass.assert_not_called()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(err.getvalue().count("the pass paused"), 1)

    def test_a_real_heartbeat_500_pauses_before_running_the_pass(self):
        reading = (200, {}, b'[{"number": 7}]')
        comments = (200, {}, b"[]")
        failure = (500, {}, b"{}")
        for phase, responses in (("read", [failure]),
                                 ("stamp", [reading, comments, failure])):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(station.Station, "run_pass") as run_pass:
                transport = mock.Mock(side_effect=responses)
                gh = GitHub("t", transport=transport)
                err = io.StringIO()
                settings = station.Settings(tmp, [REPO], [], [], status_repo=REPO)
                with contextlib.redirect_stderr(err):
                    code = station.run(settings, gh)
                self.assertEqual(code, 0)
                run_pass.assert_not_called()
                self.assertEqual(transport.call_count, len(responses))
                self.assertEqual(err.getvalue().count("GitHub outage:"), 1)
                self.assertEqual(err.getvalue().count("the pass paused"), 1)

    def test_a_paused_pass_does_not_renew_its_heartbeat(self):
        gh, _ = client((200, {}, b"[]"))
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(station.station_beat.Guard, "renew") as renew, \
                mock.patch.object(station.Station, "run_pass",
                                  side_effect=GitHubOutage("unreachable", 0, "", 0)), \
                contextlib.redirect_stderr(err):
            settings = station.Settings(tmp, [REPO], [], [], status_repo=REPO)
            code = station.run(settings, gh)
        self.assertEqual(code, 0)
        renew.assert_not_called()
        self.assertEqual(err.getvalue().count("the pass paused"), 1)

    def test_an_outage_keeps_local_cleanup_but_skips_the_disk_guard_ask(self):
        gh, fake = client((500, {}, b"{}"))
        with tempfile.TemporaryDirectory() as tmp:
            st = station.Station(gh, station.Settings(tmp, [REPO], [], []))
            with mock.patch.multiple(
                st,
                start_remote_check_ticker=mock.DEFAULT,
                capture_pod_logs=mock.DEFAULT,
                sample_operator_budget=mock.DEFAULT,
                _budget_exhausted=mock.Mock(return_value=False),
                check_repo_access=mock.Mock(return_value=True),
                ask_operator_login=mock.Mock(side_effect=lambda: gh.rest("GET", PATH)),
                prune_trees=mock.DEFAULT,
                tend_watchdog=mock.DEFAULT,
                tend_disk=mock.DEFAULT,
                _check_disk_guard_ask=mock.DEFAULT,
                _tend_usage=mock.DEFAULT,
            ) as calls, self.assertRaises(GitHubOutage):
                st.run_pass()
            for step in ("prune_trees", "tend_watchdog", "tend_disk", "_tend_usage"):
                calls[step].assert_called_once_with()
            calls["_check_disk_guard_ask"].assert_not_called()
        self.assertEqual(fake.calls, 1)

    def test_an_app_login_outage_pauses_before_running_the_pass(self):
        gh, fake = client((500, {}, b"{}"), app=True)
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(station.Station, "run_pass") as run_pass, \
                contextlib.redirect_stderr(err):
            code = station.run(station.Settings(os.path.join(tmp, "loom"), [REPO], [], []), gh)
        self.assertEqual(code, 0)
        run_pass.assert_not_called()
        self.assertEqual(fake.calls, 1)
        self.assertEqual(err.getvalue().count("the pass paused"), 1)

    def test_an_outage_rerunning_ci_does_not_record_a_refusal(self):
        gh, _ = client((500, {}, b"{}"))
        with tempfile.TemporaryDirectory() as tmp:
            st = station.Station(gh, station.Settings(tmp, [REPO], [], []))
            with mock.patch.object(st, "_cannot_re_run") as refused, \
                    self.assertRaises(GitHubOutage):
                st._rerun_flaky(REPO, 7, 8, "head", ["CI"], [])
            refused.assert_not_called()

    def test_an_outage_does_not_read_ci_evidence_as_absent(self):
        for method, args in (("_has_pull_request_workflow", (REPO,)),
                             ("_repo_ci_green_on_head", (REPO, "head")),
                             ("_weakened_tests", (REPO, 8))):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                gh, _ = client((500, {}, b"{}"))
                st = station.Station(gh, station.Settings(tmp, [REPO], [], []))
                with self.assertRaises(GitHubOutage):
                    getattr(st, method)(*args)

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
        lines = [line for line in err.getvalue().splitlines() if "GitHub outage" in line]
        self.assertEqual(len(lines), 1, err.getvalue())


if __name__ == "__main__":
    unittest.main()
