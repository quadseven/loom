"""`python3 -m bus app manifest` and `python3 -m bus app ids` (loom#679)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import app  # noqa: E402
from bus.__main__ import main  # noqa: E402

KEY = ("-----BEGIN PRIVATE KEY-----\n"
       "FAKEKEYMATERIALnotarealkeyFAKEKEYMATERIAL0123456789abcdefABCDEF\n"
       "-----END PRIVATE KEY-----\n")
KEY_MARK = "FAKEKEYMATERIAL"
FULL = {"contents": "write", "issues": "write", "pull_requests": "write",
        "checks": "read", "actions": "read", "metadata": "read"}


def run(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, **kw)
    return code, out.getvalue(), err.getvalue()


class Manifest(unittest.TestCase):
    def test_names_exactly_the_permissions_and_no_webhook(self):
        m = app.manifest("acme")
        self.assertEqual(m["default_permissions"], FULL)
        self.assertNotIn("hook_attributes", m)
        self.assertEqual(m["default_events"], [])
        self.assertFalse(m["public"])
        self.assertEqual(m["name"], "loom-acme")

    def test_create_url_for_a_user_and_an_organisation(self):
        self.assertEqual(app.create_url("acme", org=False),
                         "https://github.com/settings/apps/new")
        self.assertEqual(app.create_url("acme", org=True),
                         "https://github.com/organizations/acme/settings/apps/new")

    def test_command_prints_the_url_and_the_manifest(self):
        code, out, _ = run(["app", "manifest", "--owner", "acme", "--org"], gh=object())
        self.assertEqual(code, 0)
        self.assertIn("https://github.com/organizations/acme/settings/apps/new", out)
        body = out[out.index("{"):]
        self.assertEqual(json.loads(body)["default_permissions"], FULL)

    def test_form_is_one_submit(self):
        code, out, _ = run(["app", "manifest", "--owner", "acme", "--form"], gh=object())
        self.assertEqual(code, 0)
        self.assertIn('action="https://github.com/settings/apps/new"', out)
        self.assertIn('name="manifest"', out)
        self.assertIn("&quot;default_permissions&quot;", out)

    def test_owner_must_be_a_login(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["app", "manifest", "--owner", "a/b"])


class FakeApi:
    """GET /app/installations with the JWT, POST for an installation token,
    GET /installation/repositories with that token."""

    def __init__(self, installations, repos):
        self.installations = installations
        self.repos = repos
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers.get("Authorization", "")))
        path = url.split("api.github.com", 1)[1].split("?")[0]
        if method == "GET" and path == "/app/installations":
            return 200, {}, json.dumps(self.installations).encode()
        if method == "POST" and path.startswith("/app/installations/"):
            inst = path.split("/")[3]
            return 201, {}, json.dumps({"token": "inst-token-" + inst,
                                        "expires_at": "2099-01-01T00:00:00Z"}).encode()
        if method == "GET" and path == "/installation/repositories":
            inst = headers["Authorization"].rsplit("-", 1)[1]
            names = self.repos[inst]
            data = {"total_count": len(names),
                    "repositories": [{"full_name": n} for n in names]}
            return 200, {}, json.dumps(data).encode()
        return 404, {}, b'{"message": "Not Found"}'


def installation(num, login="acme", permissions=None, selection="selected"):
    return {"id": num, "account": {"login": login},
            "repository_selection": selection,
            "permissions": dict(FULL if permissions is None else permissions)}


class Ids(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.key = os.path.join(self.dir, "app.pem")
        with open(self.key, "w", encoding="ascii") as fh:
            fh.write(KEY)
        self.signed = []

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def sign(self, data, key):
        self.signed.append(key)
        return b"sig"

    def ids(self, fake, repos="acme/a acme/b", extra=()):
        env = {"LOOM_GITHUB_APP_KEY_FILE": self.key, "LOOM_REPOS": repos}
        before = set(os.listdir(self.dir))
        cwd_before = set(os.listdir(os.getcwd()))
        with mock.patch.dict(os.environ, env, clear=False):
            code, out, err = run(["app", "ids", "--app-id", "42", *extra],
                                 app_transport=fake, app_sign=self.sign)
        # The key is read and used to sign, never printed or written to disk.
        self.assertTrue(self.signed)
        self.assertTrue(all(KEY_MARK in k for k in self.signed))
        self.assertNotIn(KEY_MARK, out + err)
        self.assertNotIn("inst-token", out + err)
        self.assertEqual(set(os.listdir(self.dir)), before)
        self.assertEqual(set(os.listdir(os.getcwd())), cwd_before)
        return code, out, err

    def test_prints_the_two_setting_lines(self):
        fake = FakeApi([installation(7)], {"7": ["acme/a", "acme/b"]})
        code, out, err = self.ids(fake)
        self.assertEqual(code, 0, err)
        self.assertIn("LOOM_GITHUB_APP_ID=42\n", out)
        self.assertIn("LOOM_GITHUB_APP_INSTALLATION_ID=7\n", out)
        self.assertIn("installation 7 on acme (selected): acme/a, acme/b", out)
        self.assertNotIn("warning", out + err)
        # The listing is read with the App's JWT, the repos with the token.
        self.assertTrue(fake.calls[0][2].startswith("Bearer "))
        self.assertEqual(fake.calls[0][2].count("."), 2)

    def test_warns_about_a_loom_repo_the_installation_misses(self):
        fake = FakeApi([installation(7)], {"7": ["acme/a"]})
        code, out, err = self.ids(fake)
        self.assertEqual(code, 0, err)
        self.assertIn("LOOM_GITHUB_APP_INSTALLATION_ID=7\n", out)
        self.assertIn("warning: installation 7 does not cover acme/b, which is in LOOM_REPOS",
                      err)

    def test_warns_about_a_repo_outside_loom_repos(self):
        fake = FakeApi([installation(7)], {"7": ["acme/a", "acme/b", "acme/c"]})
        _, _, err = self.ids(fake)
        self.assertIn("warning: installation 7 covers acme/c, which is not in LOOM_REPOS", err)

    def test_warns_about_a_missing_or_weaker_permission(self):
        perms = dict(FULL, issues="read")
        del perms["checks"]
        fake = FakeApi([installation(7, permissions=perms)], {"7": ["acme/a", "acme/b"]})
        _, _, err = self.ids(fake)
        self.assertIn("warning: installation 7 has issues: read; Loom needs write", err)
        self.assertIn("warning: installation 7 has no checks permission; Loom needs read", err)

    def test_picks_the_installation_covering_the_most_loom_repos(self):
        fake = FakeApi([installation(5, login="other"), installation(9)],
                       {"5": ["other/x"], "9": ["acme/a", "acme/b"]})
        code, out, err = self.ids(fake)
        self.assertEqual(code, 0, err)
        self.assertIn("LOOM_GITHUB_APP_INSTALLATION_ID=9\n", out)
        self.assertIn("installation 5 on other (selected): other/x", out)

    def test_no_installation_fails(self):
        code, out, err = self.ids(FakeApi([], {}))
        self.assertEqual(code, 1)
        self.assertNotIn("LOOM_GITHUB_APP_INSTALLATION_ID", out)
        self.assertIn("no installations", err)

    def test_a_refused_listing_is_an_error_without_the_key(self):
        def refuse(method, url, headers, body):
            return 401, {}, b'{"message": "A JSON web token could not be decoded"}'
        code, out, err = self.ids(refuse)
        self.assertEqual(code, 2)
        self.assertIn("GET /app/installations -> 401", err)

    def test_needs_a_key_setting(self):
        with mock.patch.dict(os.environ, {"LOOM_GITHUB_APP_KEY_FILE": "",
                                          "LOOM_GITHUB_APP_KEY_SSM": ""}):
            code, _, err = run(["app", "ids", "--app-id", "42"],
                               app_transport=FakeApi([], {}), app_sign=self.sign)
        self.assertEqual(code, 2)
        self.assertIn("LOOM_GITHUB_APP_KEY_FILE or LOOM_GITHUB_APP_KEY_SSM", err)


if __name__ == "__main__":
    unittest.main()
