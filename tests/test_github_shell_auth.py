"""Loom's shell wrappers with a stand-in for the real gh executable."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "lanes" / "github-bin"


class GitHubShellAuthTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.token = self.root / "token"
        self.real = self.root / "gh"
        self.real.write_text('#!/bin/sh\nprintf "%s\\n" "$GH_TOKEN"\n')
        self.real.chmod(0o755)
        self.env = dict(os.environ, GH_CONFIG_DIR=str(self.config),
                        LOOM_REAL_GH=str(self.real), LOOM_APP_TOKEN_FILE=str(self.token),
                        GH_TOKEN="ghp_fake_personal")

    def call(self, executable, *args, input=None):
        return subprocess.run(["bash", str(BIN / executable), *args], env=self.env,
                              input=input, capture_output=True, text=True, timeout=5)

    def test_empty_token_stops_gh_before_it_can_use_a_personal_login(self):
        self.token.touch()
        result = self.call("gh", "api", "user")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("no GitHub App token", result.stderr)

    def test_gh_disables_auth_commands(self):
        self.token.write_text("ghs_fake_app\n")
        for command in ("login", "refresh", "setup-git", "switch", "token"):
            with self.subTest(command=command):
                result = self.call("gh", "auth", command)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("disabled", result.stderr)

    def test_gh_uses_the_app_token_and_refuses_a_stored_login(self):
        self.token.write_text("ghs_fake_app\n")
        result = self.call("gh", "api", "user")
        self.assertEqual((result.returncode, result.stdout), (0, "ghs_fake_app\n"))
        (self.config / "hosts.yml").write_text("stored login")
        refused = self.call("gh", "api", "user")
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(refused.stdout, "")

    def test_git_credentials_are_read_only_and_scoped_to_github_https(self):
        self.token.write_text("ghs_fake_app\n")
        github = "protocol=https\nhost=github.com\n\n"
        result = self.call("git-credential-loom", "get", input=github)
        self.assertEqual(result.stdout, "username=x-access-token\npassword=ghs_fake_app\n")
        for operation, request in (("store", github), ("erase", github),
                                   ("get", github.replace("github.com", "example.com")),
                                   ("get", github.replace("https", "http"))):
            self.assertEqual(self.call("git-credential-loom", operation, input=request).stdout, "")
        self.token.unlink()
        refused = self.call("git-credential-loom", "get", input=github)
        self.assertEqual(refused.stdout, "quit=true\n")

    def test_git_uses_only_the_installed_helper(self):
        settings = self.root / "gitconfig"
        settings.write_text('[credential "https://github.com"]\n'
                            '	helper = "!echo username=person; echo password=personal"\n')
        env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}",
                   GIT_CONFIG_GLOBAL=str(settings))
        env.pop("GH_TOKEN", None)
        env.pop("GH_CONFIG_DIR", None)
        script = """
. "$1/github_auth.sh" || exit 1
github_auth_init "$1" || exit 1
trap github_auth_cleanup EXIT
if [ "$2" = with-token ]; then printf '%s\n' ghs_fake_app > "$LOOM_APP_TOKEN_FILE"; fi
printf 'protocol=https\nhost=github.com\n\n' | git credential fill
"""
        for mode, want in (("with-token", 0), ("empty", 128)):
            with self.subTest(mode=mode):
                result = subprocess.run(["bash", "-c", script, "auth-test", str(BIN.parent), mode],
                                        env=env, cwd=self.root, capture_output=True, text=True,
                                        timeout=10)
                self.assertEqual(result.returncode, want, result.stderr)
                self.assertNotIn("personal", result.stdout)
                if want == 0:
                    self.assertIn("password=ghs_fake_app", result.stdout)
                else:
                    self.assertEqual(result.stdout, "")
