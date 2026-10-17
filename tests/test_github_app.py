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
        def raising(exc):
            def sign(data, key):
                raise exc(key)
            return sign

        def reader_raises():
            raise OSError("bad parameter " + KEY)

        cases = []
        for exc in (ValueError, OSError, RuntimeError, GitHubError):
            app = AppToken("123", "456", lambda: KEY, sign=raising(exc), clock=Clock())
            cases.append(GitHub(app, transport=FakeGitHub(["t"])))
        cases.append(GitHub(AppToken("123", "456", reader_raises, sign=fake_sign([])),
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


if __name__ == "__main__":
    unittest.main()
