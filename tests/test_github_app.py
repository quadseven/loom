"""The GitHub client on a GitHub App installation token (loom#629)."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import github  # noqa: E402
from bus.github import AppToken, GitHub, GitHubError, auth_from_env  # noqa: E402

KEY = ("-----BEGIN PRIVATE KEY-----\n"
       "FAKEKEYMATERIALnotarealkeyFAKEKEYMATERIAL0123456789abcdefABCDEF\n"
       "-----END PRIVATE KEY-----\n")
KEY_MARK = "FAKEKEYMATERIAL"
START = 1_900_000_000.0


def stamp(epoch):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


class FakeGitHub:
    """Mints installation tokens (one per POST, from a queue) and answers
    any GET with a rate limit body and headers."""

    def __init__(self, tokens, lifetime=3600, clock=None, mint_status=201):
        self.tokens = list(tokens)
        self.lifetime = lifetime
        self.clock = clock or Clock()
        self.mint_status = mint_status
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        if method == "POST" and "/app/installations/" in url:
            if self.mint_status != 201:
                return self.mint_status, {}, json.dumps({"message": "Bad credentials"}).encode()
            token = self.tokens.pop(0)
            answer = {"token": token, "expires_at": stamp(self.clock() + self.lifetime)}
            # The App's own JWT budget: it must not replace the installation's.
            limits = {"X-RateLimit-Remaining": "1", "X-RateLimit-Limit": "10",
                      "X-RateLimit-Reset": "2000000000"}
            return 201, limits, json.dumps(answer).encode()
        limits = {"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000",
                  "X-RateLimit-Reset": "2000000000", "X-RateLimit-Resource": "core"}
        return 200, limits, json.dumps({"resources": {}}).encode()

    def bearers(self, method=None):
        return [h["Authorization"] for m, _, h in self.calls if method in (None, m)]


def fake_sign(record):
    def sign(data, key):
        record.append((data, key))
        return b"signature-bytes"
    return sign


def app_client(tokens, clock, signed=None, lifetime=3600, mint_status=201):
    fake = FakeGitHub(tokens, lifetime, clock, mint_status)
    app = AppToken("123", "456", lambda: KEY, sign=fake_sign([] if signed is None else signed),
                   clock=clock)
    return GitHub(app, transport=fake), fake, app


class AppTokenClientTests(unittest.TestCase):
    def test_the_app_client_sends_the_installation_token_and_refreshes_before_expiry(self):
        clock, signed = Clock(), []
        gh, fake, app = app_client(["ghs_first", "ghs_second"], clock, signed)
        gh.rest("GET", "/rate_limit")
        gh.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET"), ["Bearer ghs_first"] * 2)
        posts = [c for c in fake.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "https://api.github.com/app/installations/456/access_tokens")
        # The POST carries the App's JWT: RS256, issued by the App, 9 minutes.
        jwt = posts[0][2]["Authorization"][len("Bearer "):]
        header, claims, signature = jwt.split(".")
        self.assertEqual(json.loads(unb64(header)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(unb64(claims)),
                         {"iat": int(START) - 60, "exp": int(START) + 540, "iss": "123"})
        self.assertEqual(unb64(signature), b"signature-bytes")
        self.assertEqual(signed, [((header + "." + claims).encode("ascii"), KEY)])
        # Still more than 5 minutes left: the same token.
        clock.now = START + 3600 - 301
        gh.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET")[-1], "Bearer ghs_first")
        # Within 5 minutes of expires_at: a new token before the request.
        clock.now = START + 3600 - 299
        gh.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET")[-1], "Bearer ghs_second")
        self.assertEqual(app.minted, 2)
        # The budget is the installation's, and the mints are counted apart.
        self.assertEqual(gh.rate["core"], (4999, 5000, 2000000000))
        self.assertEqual(gh.kinds, {"other": 4, "app token": 2})
        self.assertEqual(gh.auth, "app")

    def test_a_401_makes_the_next_request_mint_a_new_token(self):
        clock = Clock()
        gh, fake, _ = app_client(["ghs_first", "ghs_second"], clock)
        gh.rest("GET", "/rate_limit")
        answer = fake.__call__
        fake_calls = []

        def once_401(method, url, headers, body):
            fake_calls.append(1)
            if len(fake_calls) == 1:
                return 401, {}, b"{}"
            return answer(method, url, headers, body)
        gh.transport = once_401
        with self.assertRaises(GitHubError):
            gh.rest("GET", "/rate_limit")
        gh.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET")[-1], "Bearer ghs_second")

    def test_a_400_character_token_is_sent_whole(self):
        token = "ghs_" + "A1b2" * 99
        self.assertEqual(len(token), 400)
        gh, fake, _ = app_client([token], Clock())
        gh.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET"), ["Bearer " + token])

    def test_an_unconfigured_client_sends_the_personal_token(self):
        for partial in ({}, {"LOOM_GITHUB_APP_ID": "123", "LOOM_GITHUB_APP_INSTALLATION_ID": "456"},
                        {"LOOM_GITHUB_APP_ID": "123", "LOOM_GITHUB_APP_KEY_SSM": "/x/key"},
                        {"LOOM_GITHUB_APP_INSTALLATION_ID": "456", "LOOM_GITHUB_APP_KEY_SSM": "/x/key"}):
            env = dict(partial, LOOM_GITHUB_TOKEN="personal-token")
            with mock.patch.dict(os.environ, env, clear=True):
                auth = auth_from_env()
            self.assertEqual(auth, "personal-token")
            fake = FakeGitHub([])
            gh = GitHub(auth, transport=fake)
            gh.rest("GET", "/rate_limit")
            self.assertEqual(fake.bearers(), ["Bearer personal-token"])
            self.assertEqual(gh.auth, "token")

    def test_the_three_settings_make_an_app_token_read_from_ssm(self):
        env = {"LOOM_GITHUB_APP_ID": "123", "LOOM_GITHUB_APP_INSTALLATION_ID": "456",
               "LOOM_GITHUB_APP_KEY_SSM": "/example/key", "LOOM_AWS_REGION": "eu-west-1",
               "LOOM_GITHUB_TOKEN": "personal-token"}
        reads = []
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(github, "read_ssm", lambda n, r: reads.append((n, r)) or KEY):
            auth = auth_from_env()
            self.assertIsInstance(auth, AppToken)
            self.assertEqual(reads, [])  # read only when a token is minted
            auth._sign = fake_sign([])
            gh = GitHub(auth, transport=FakeGitHub(["ghs_first"]))
            gh.rest("GET", "/rate_limit")
        self.assertEqual(reads, [("/example/key", "eu-west-1")])
        with mock.patch.dict(os.environ, dict(env, LOOM_GITHUB_APP_ID="abc"), clear=True):
            with self.assertRaises(GitHubError):
                auth_from_env()


class TokenKeptAcrossPassesTests(unittest.TestCase):
    """The station runs one pass per process; a token kept in a 0600 state
    file lets the next pass reuse it, so last pass's ETags answer 304
    (loom#1306)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "station", "app-token.json")
        self.cache = os.path.join(self._tmp.name, "etags")

    def pass_client(self, tokens, clock, transport):
        """A fresh client, as a new process builds one."""
        app = AppToken("123", "456", lambda: KEY, sign=fake_sign([]), clock=clock)
        app.state_file = self.state
        return GitHub(app, transport=transport, cache_dir=self.cache), app

    def test_a_second_pass_inside_the_lifetime_mints_no_token(self):
        clock = Clock()
        fake = FakeGitHub(["ghs_first", "ghs_second"], clock=clock)
        gh1, app1 = self.pass_client(None, clock, fake)
        gh1.rest("GET", "/rate_limit")
        clock.now = START + 600
        gh2, app2 = self.pass_client(None, clock, fake)
        gh2.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET"), ["Bearer ghs_first"] * 2)
        self.assertEqual((app1.minted, app2.minted), (1, 0))
        self.assertEqual(gh2.kinds.get("app token", 0), 0)

    def test_a_token_near_expiry_or_of_another_app_is_not_reused(self):
        clock = Clock()
        fake = FakeGitHub(["ghs_first", "ghs_second", "ghs_third"], clock=clock)
        gh1, _ = self.pass_client(None, clock, fake)
        gh1.rest("GET", "/rate_limit")
        clock.now = START + 3600 - 299
        gh2, app2 = self.pass_client(None, clock, fake)
        gh2.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET")[-1], "Bearer ghs_second")
        self.assertEqual(app2.minted, 1)
        other = AppToken("123", "999", lambda: KEY, sign=fake_sign([]), clock=clock)
        other.state_file = self.state
        GitHub(other, transport=fake).rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET")[-1], "Bearer ghs_third")

    def test_a_refused_token_is_dropped_from_the_state_file(self):
        clock = Clock()
        fake = FakeGitHub(["ghs_first", "ghs_second"], clock=clock)
        gh, app = self.pass_client(None, clock, fake)
        gh.rest("GET", "/rate_limit")
        self.assertTrue(os.path.exists(self.state))
        gh._check_status("GET", "/x", 401, "", (401,))
        self.assertFalse(os.path.exists(self.state))
        gh3, _ = self.pass_client(None, clock, fake)
        gh3.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET")[-1], "Bearer ghs_second")

    def test_a_cached_etag_from_the_last_pass_is_sent_and_a_304_is_not_counted(self):
        clock = Clock()
        seen = []

        def transport(method, url, headers, body):
            if method == "POST":
                return FakeGitHub(["ghs_first"], clock=clock)(method, url, headers, body)
            seen.append(headers.get("If-None-Match"))
            # GitHub's ETags belong to the token that read them.
            tag = '"etag-for-%s"' % headers["Authorization"][len("Bearer "):]
            if headers.get("If-None-Match") == tag:
                return 304, {"ETag": tag}, b""
            return 200, {"ETag": tag}, json.dumps([{"number": 7}]).encode()

        gh1, _ = self.pass_client(None, clock, transport)
        self.assertEqual(gh1.rest("GET", "/repos/o/r/issues/7").data, [{"number": 7}])
        gh2, _ = self.pass_client(None, clock, transport)
        answer = gh2.rest("GET", "/repos/o/r/issues/7")
        self.assertEqual(seen, [None, '"etag-for-ghs_first"'])
        self.assertEqual(answer.status, 304)
        self.assertEqual(answer.data, [{"number": 7}])
        self.assertEqual(gh2.not_modified, 1)
        self.assertEqual(gh2.kinds, {})

    def test_the_token_is_written_only_to_a_0600_state_file(self):
        clock = Clock()
        fake = FakeGitHub(["ghs_secret_token"], clock=clock)
        gh, app = self.pass_client(None, clock, fake)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            gh.rest("GET", "/rate_limit")
            repr(app)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)
        self.assertNotIn("ghs_secret_token", buf.getvalue() + repr(app))
        holders = []
        for root, _, names in os.walk(self._tmp.name):
            for name in names:
                with open(os.path.join(root, name), errors="replace") as fh:
                    if "ghs_secret_token" in fh.read():
                        holders.append(os.path.join(root, name))
        self.assertEqual(holders, [self.state])
        with open(self.state) as fh:
            self.assertNotIn(KEY_MARK, fh.read())

    def test_a_state_file_open_to_others_is_ignored(self):
        clock = Clock()
        fake = FakeGitHub(["ghs_first", "ghs_second"], clock=clock)
        gh1, _ = self.pass_client(None, clock, fake)
        gh1.rest("GET", "/rate_limit")
        os.chmod(self.state, 0o644)
        gh2, app2 = self.pass_client(None, clock, fake)
        gh2.rest("GET", "/rate_limit")
        self.assertEqual(fake.bearers("GET")[-1], "Bearer ghs_second")
        self.assertEqual(app2.minted, 1)

    def test_without_a_state_file_nothing_is_written(self):
        clock = Clock()
        gh, fake, app = app_client(["ghs_first"], clock)
        gh.rest("GET", "/rate_limit")
        self.assertIsNone(app.state_file)
        self.assertFalse(os.path.exists(self.state))


class KeyNeverLeaksTests(unittest.TestCase):
    def _capture(self, run):
        """Every exception text, stdout, stderr and log line from run()."""
        out, err, logs = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        texts = []
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    run()
                except Exception as exc:  # noqa: BLE001 - its text is checked
                    texts.append(str(exc))
                    texts.append(repr(exc))
                    texts.append(str(getattr(exc, "body", "")))
                    texts.append(str(exc.__cause__ or exc.__context__ or ""))
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        return "\n".join(texts + [out.getvalue(), err.getvalue(), logs.getvalue()])

    def test_the_key_never_appears_in_an_error_or_log_line(self):
        # The failures a key read or a signing is expected to have, each
        # carrying the key in its text.
        expected = (lambda: OSError("bad parameter " + KEY),
                    lambda: subprocess.TimeoutExpired(["aws", KEY], 30, output=KEY.encode()),
                    lambda: subprocess.CalledProcessError(1, ["openssl", KEY], stderr=KEY.encode()))

        def raising(make):
            def fail(*_):
                raise make()
            return fail

        cases = []
        for make in expected:
            app = AppToken("123", "456", lambda: KEY, sign=raising(make), clock=Clock())
            cases.append(GitHub(app, transport=FakeGitHub(["t"])))
            cases.append(GitHub(AppToken("123", "456", raising(make), sign=fake_sign([])),
                                transport=FakeGitHub(["t"])))
        cases.append(app_client(["t"], Clock(), mint_status=401)[0])
        cases.append(app_client(["t"], Clock())[0])
        for gh in cases:
            text = self._capture(lambda: gh.rest("GET", "/rate_limit"))
            self.assertNotIn(KEY_MARK, text)
            self.assertNotIn(KEY_MARK, repr(gh.app))
        # openssl failing: its own stderr is never passed on.
        failed = subprocess.CompletedProcess([], 1, b"", ("bad key " + KEY).encode())
        with mock.patch.object(github.subprocess, "run", return_value=failed):
            text = self._capture(lambda: github.openssl_sign(b"data", KEY))
        self.assertIn("exit 1", text)
        self.assertNotIn(KEY_MARK, text)

    def test_an_unexpected_error_in_the_key_read_or_signing_is_not_masked(self):
        def broken_reader():
            raise TypeError("reader bug")

        def broken_signer(data, key):
            raise AttributeError("signer bug")
        gh = GitHub(AppToken("123", "456", broken_reader, sign=fake_sign([])),
                    transport=FakeGitHub(["t"]))
        with self.assertRaisesRegex(TypeError, "reader bug"):
            gh.rest("GET", "/rate_limit")
        gh = GitHub(AppToken("123", "456", lambda: KEY, sign=broken_signer),
                    transport=FakeGitHub(["t"]))
        with self.assertRaisesRegex(AttributeError, "signer bug"):
            gh.rest("GET", "/rate_limit")

    def test_a_refused_mint_names_githubs_reason_and_never_falls_back(self):
        gh, fake, _ = app_client(["t"], Clock(), mint_status=401)
        with self.assertRaises(GitHubError) as caught:
            gh.rest("GET", "/rate_limit")
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("-> 401 (Bad credentials)", str(caught.exception))
        # Nothing was sent with any other token.
        self.assertEqual([m for m, _, _ in fake.calls], ["POST"])

    def test_openssl_gets_the_key_on_stdin_never_on_argv_or_disk(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["input"] = kwargs.get("input")
            data_path = argv[-1]
            self.assertTrue(data_path.startswith("/dev/fd/"))
            fd = int(data_path.rsplit("/", 1)[1])
            self.assertIn(fd, kwargs.get("pass_fds", ()))
            seen["data"] = os.read(fd, 4096)
            return subprocess.CompletedProcess(argv, 0, b"sig", b"")

        real_open = open
        written = []

        def watching_open(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                written.append(path)
            return real_open(path, mode, *args, **kwargs)
        with mock.patch.object(github.subprocess, "run", fake_run), \
                mock.patch("builtins.open", watching_open), \
                mock.patch.object(github.os, "open", side_effect=AssertionError("no files")):
            self.assertEqual(github.openssl_sign(b"header.claims", KEY), b"sig")
        self.assertEqual(written, [])
        self.assertEqual(seen["input"], KEY.encode())
        self.assertEqual(seen["data"], b"header.claims")
        self.assertNotIn(KEY_MARK, " ".join(seen["argv"]))
        self.assertEqual(seen["argv"][:5], ["openssl", "dgst", "-sha256", "-binary", "-sign"])

    @unittest.skipUnless(shutil.which("openssl"), "the openssl CLI is not installed")
    def test_openssl_signs_a_jwt_that_verifies(self):
        key = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA",
                              "-pkeyopt", "rsa_keygen_bits:2048"],
                             capture_output=True, check=True, timeout=60).stdout.decode()
        public = subprocess.run(["openssl", "pkey", "-pubout"], input=key.encode(),
                                capture_output=True, check=True, timeout=60).stdout
        app = AppToken("123", "456", lambda: key, clock=Clock())
        header, claims, signature = app.jwt().split(".")
        with tempfile.TemporaryDirectory() as tmp:
            paths = {}
            for name, raw in (("data", (header + "." + claims).encode()),
                              ("sig", unb64(signature)), ("pub", public)):
                paths[name] = os.path.join(tmp, name)
                with open(paths[name], "wb") as fh:
                    fh.write(raw)
            check = subprocess.run(["openssl", "dgst", "-sha256", "-verify", paths["pub"],
                                    "-signature", paths["sig"], paths["data"]],
                                   capture_output=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


class DashboardAppTests(unittest.TestCase):
    ENV = {"LOOM_REPOS": "example-owner/example-repo",
           "LOOM_DASHBOARD_GITHUB_TOKEN": "dashboard-token",
           "LOOM_GITHUB_APP_ID": "123", "LOOM_GITHUB_APP_INSTALLATION_ID": "456",
           "LOOM_GITHUB_APP_KEY_SSM": "/example/key"}

    def test_the_dashboard_uses_the_app_only_where_it_can_mint(self):
        from dashboard import github as dash
        tools = {"openssl": "/usr/bin/openssl", "aws": "/usr/bin/aws"}
        with mock.patch.dict(os.environ, self.ENV, clear=True), \
                mock.patch.object(dash.shutil, "which", tools.get):
            store = dash.TicketStore()
        self.assertEqual(store.auth, "app")
        self.assertIsInstance(store.gh.app, AppToken)
        for missing in ("openssl", "aws"):
            with mock.patch.dict(os.environ, self.ENV, clear=True), \
                    mock.patch.object(dash.shutil, "which",
                                      lambda tool, m=missing: None if tool == m else tools[tool]), \
                    self.assertLogs(dash.LOG, "WARNING") as logged:
                store = dash.TicketStore()
            self.assertEqual(store.auth, "token")
            self.assertIsNone(store.gh.app)
            self.assertIn(missing, "\n".join(logged.output))
        env = {k: v for k, v in self.ENV.items() if not k.startswith("LOOM_GITHUB_APP")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(dash.TicketStore().auth, "token")

    def test_a_token_dashboard_reads_the_trusted_writers_status_events(self):
        # Without the writers, a token client showed a status comment as one
        # opaque comment, so a pushed fix or a lane failure left the timeline.
        from dashboard import github as dash
        env = {"LOOM_REPOS": "example-owner/example-repo",
               "LOOM_DASHBOARD_GITHUB_TOKEN": "dashboard-token",
               "LOOM_BRIEF_AUTHORS": "Alice-Example,bob-example"}
        with mock.patch.dict(os.environ, env, clear=True):
            store = dash.TicketStore()
        self.assertEqual(store.auth, "token")
        self.assertEqual(store.gh.status_authors, {"alice-example", "bob-example"})

    FILE_ENV = {"LOOM_REPOS": "example-owner/example-repo",
                "LOOM_DASHBOARD_GITHUB_TOKEN": "dashboard-token",
                "LOOM_GITHUB_APP_ID": "123", "LOOM_GITHUB_APP_INSTALLATION_ID": "456"}

    def test_a_mounted_key_file_makes_the_dashboard_send_an_installation_token(self):
        from dashboard import github as dash
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app-key.pem")
            with open(path, "w") as fh:
                fh.write(KEY)
            env = dict(self.FILE_ENV, LOOM_GITHUB_APP_KEY_FILE=path)
            # No aws CLI in the pod: the key file needs only openssl.
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(dash.shutil, "which",
                                      lambda tool: "/usr/bin/openssl" if tool == "openssl" else None):
                store = dash.TicketStore()
            self.assertEqual(store.auth, "app")
            app = store.gh.app
            signed = []
            app._sign = fake_sign(signed)
            fake = FakeGitHub(["ghs_from_file"])
            gh = GitHub(app, transport=fake)
            gh.rest("GET", "/rate_limit")
            self.assertEqual(fake.bearers("GET"), ["Bearer ghs_from_file"])
            self.assertEqual([key for _, key in signed], [KEY])
            # The file is read at each mint, so a rotated key is used next time.
            with open(path, "w") as fh:
                fh.write(KEY.replace("FAKEKEY", "ROTATED"))
            app.invalidate()
            GitHub(app, transport=FakeGitHub(["ghs_next"])).rest("GET", "/rate_limit")
            self.assertIn("ROTATED", signed[-1][1])

    def test_an_unreadable_key_file_keeps_the_token_and_names_only_the_file(self):
        from dashboard import github as dash
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "missing-key.pem")
            env = dict(self.FILE_ENV, LOOM_GITHUB_APP_KEY_FILE=path)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(dash.shutil, "which", lambda tool: "/usr/bin/" + tool), \
                    self.assertLogs(dash.LOG, "WARNING") as logged:
                store = dash.TicketStore()
            self.assertEqual(store.auth, "token")
            self.assertIsNone(store.gh.app)
            self.assertEqual(len(logged.output), 1)
            self.assertIn(path, logged.output[0])
            # A file that exists but cannot be read: still only its name.
            secret = os.path.join(tmp, "app-key.pem")
            with open(secret, "w") as fh:
                fh.write(KEY)
            env = dict(self.FILE_ENV, LOOM_GITHUB_APP_KEY_FILE=secret)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(dash.shutil, "which", lambda tool: "/usr/bin/" + tool), \
                    mock.patch("builtins.open", side_effect=PermissionError("denied")), \
                    self.assertLogs(dash.LOG, "WARNING") as logged:
                store = dash.TicketStore()
            self.assertEqual(store.auth, "token")
            self.assertIn(secret, logged.output[0])
            self.assertNotIn(KEY_MARK, "\n".join(logged.output))


class ReadSsmProxyTests(unittest.TestCase):
    """read_ssm passes only https_proxy to the AWS CLI."""

    def _capture_env(self, env_vars):
        """Run read_ssm with mocked subprocess and capture the env passed."""
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "fake-value", "")

        with mock.patch.dict(os.environ, env_vars, clear=True), \
                mock.patch.object(github.subprocess, "run", fake_run):
            github.read_ssm("/test/param", "us-east-1")
        return captured["env"]

    def test_only_https_proxy_is_passed_when_all_proxies_set(self):
        env = {
            "http_proxy": "http://192.0.2.1:3128",
            "HTTP_PROXY": "http://192.0.2.1:3128",
            "https_proxy": "http://192.0.2.2:3128",
            "HTTPS_PROXY": "http://192.0.2.2:3128",
            "all_proxy": "socks5://192.0.2.3:1080",
            "ALL_PROXY": "socks5://192.0.2.3:1080",
        }
        passed_env = self._capture_env(env)
        # Only https_proxy (lowercase) should be present
        self.assertIn("https_proxy", passed_env)
        self.assertEqual(passed_env["https_proxy"], "http://192.0.2.2:3128")
        self.assertNotIn("http_proxy", passed_env)
        self.assertNotIn("HTTP_PROXY", passed_env)
        self.assertNotIn("HTTPS_PROXY", passed_env)
        self.assertNotIn("all_proxy", passed_env)
        self.assertNotIn("ALL_PROXY", passed_env)

    def test_only_HTTPS_PROXY_is_passed_when_only_uppercase_set(self):
        env = {
            "HTTPS_PROXY": "http://192.0.2.2:3128",
        }
        passed_env = self._capture_env(env)
        self.assertIn("HTTPS_PROXY", passed_env)
        self.assertEqual(passed_env["HTTPS_PROXY"], "http://192.0.2.2:3128")
        self.assertNotIn("https_proxy", passed_env)

    def test_no_proxy_vars_when_none_set(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/user"}
        passed_env = self._capture_env(env)
        self.assertNotIn("http_proxy", passed_env)
        self.assertNotIn("HTTP_PROXY", passed_env)
        self.assertNotIn("https_proxy", passed_env)
        self.assertNotIn("HTTPS_PROXY", passed_env)
        self.assertNotIn("all_proxy", passed_env)
        self.assertNotIn("ALL_PROXY", passed_env)
        # Other env vars should still be passed
        self.assertEqual(passed_env["PATH"], "/usr/bin")
        self.assertEqual(passed_env["HOME"], "/home/user")

    def test_https_proxy_lowercase_preferred_when_both_set(self):
        env = {
            "https_proxy": "http://192.0.2.2:3128",
            "HTTPS_PROXY": "http://198.51.100.2:3128",
        }
        passed_env = self._capture_env(env)
        # Lowercase should win
        self.assertIn("https_proxy", passed_env)
        self.assertEqual(passed_env["https_proxy"], "http://192.0.2.2:3128")
        self.assertNotIn("HTTPS_PROXY", passed_env)


if __name__ == "__main__":
    unittest.main()
