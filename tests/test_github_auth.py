"""Loom calls GitHub only with its App token and fails closed without it
(loom#3083): lanes/fleet-token, the gh wrapper in lanes/github-bin, git's
credential helper there, lanes/github_auth.sh, and the station's pass."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bus import station
from bus.github import AppToken, GitHubError

LANES = Path(__file__).resolve().parent.parent / "lanes"
FLEET_TOKEN = LANES / "fleet-token"
WRAPPER = LANES / "github-bin" / "gh"
HELPER = LANES / "github-bin" / "git-credential-loom"
AUTH = LANES / "github_auth.sh"

# A real gh that would answer from a stored login: it records each call and
# the token it was handed, and succeeds.
REAL_GH = """#!/usr/bin/env bash
printf 'gh %s token=%s\\n' "$*" "${GH_TOKEN:-}" >> "$FAKE_LOG"
exit 0
"""


class GithubAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.write(self.bin / "gh", REAL_GH)
        self.config = self.tmp / "gh-config"
        self.config.mkdir()

    @staticmethod
    def write(path, text):
        path.write_text(text)
        path.chmod(0o755)
        return str(path)

    def base_env(self, **env):
        full = {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(self.tmp),
                "FAKE_LOG": str(self.log), "TMPDIR": str(self.tmp)}
        full.update(env)
        return full

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def token_cmd(self, script):
        return self.write(self.tmp / "mint", "#!/usr/bin/env bash\n" + script + "\n")

    def fleet_token(self, **env):
        return subprocess.run([str(FLEET_TOKEN)], env=self.base_env(**env),
                              capture_output=True, text=True, timeout=60)

    # fleet-token

    def test_fleet_token_prints_only_the_minted_token(self):
        run = self.fleet_token(LOOM_GITHUB_APP_ID="124",
                               LOOM_APP_TOKEN_CMD=self.token_cmd("echo ghs_lanes.tok-en"))
        self.assertEqual((run.returncode, run.stdout), (0, "ghs_lanes.tok-en\n"))

    def test_fleet_token_fails_with_empty_output_on_every_failure(self):
        cases = {
            "no App": dict(LOOM_APP_TOKEN_CMD=self.token_cmd("echo ghs_would.mint")),
            "GitHub refused": dict(LOOM_GITHUB_APP_ID="124", LOOM_APP_TOKEN_CMD=self.token_cmd(
                "echo 'error: POST -> 401 (Bad credentials)' >&2; exit 2")),
            "no token in the answer": dict(LOOM_GITHUB_APP_ID="124",
                                           LOOM_APP_TOKEN_CMD=self.token_cmd("echo")),
            "a personal token": dict(LOOM_GITHUB_APP_ID="124",
                                     LOOM_APP_TOKEN_CMD=self.token_cmd("echo ghp_personal")),
        }
        for name, env in cases.items():
            with self.subTest(name):
                run = self.fleet_token(**env)
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertEqual(run.stdout, "")
                self.assertIn("fleet-token: ", run.stderr)

    def test_fleet_token_with_a_missing_id_or_an_empty_key_prints_nothing(self):
        # The real `python3 -m bus app token`, which never reaches GitHub here.
        empty_key = self.tmp / "key.pem"
        empty_key.write_text("")
        python = Path(shutil.which("python3") or "/usr/bin/python3")
        os.symlink(python, self.bin / "python3")
        for name, env in {
            "no installation id": dict(LOOM_GITHUB_APP_ID="124",
                                       LOOM_GITHUB_APP_KEY_FILE=str(empty_key)),
            "an empty key": dict(LOOM_GITHUB_APP_ID="124", LOOM_GITHUB_APP_INSTALLATION_ID="77",
                                 LOOM_GITHUB_APP_KEY_FILE=str(empty_key)),
        }.items():
            with self.subTest(name):
                run = self.fleet_token(**env)
                self.assertEqual((run.returncode, run.stdout), (1, ""), run.stderr)

    def test_an_empty_app_key_is_refused_before_signing(self):
        signed = []
        app = AppToken("124", "77", lambda: "  \n", sign=lambda *a: signed.append(a) or b"")
        with self.assertRaises(GitHubError) as err:
            app.jwt()
        self.assertIn("the key is empty", str(err.exception))
        self.assertEqual(signed, [])

    # the gh wrapper

    def wrapped(self, *args, **env):
        full = self.base_env(LOOM_REAL_GH=str(self.bin / "gh"), GH_CONFIG_DIR=str(self.config))
        full.update(env)
        return subprocess.run([str(WRAPPER), *args], env=full, capture_output=True,
                              text=True, timeout=60)

    def test_the_wrapper_runs_gh_with_the_app_token(self):
        run = self.wrapped("pr", "list", GH_TOKEN="ghs_app", GITHUB_TOKEN="ghp_personal")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.calls(), ["gh pr list token=ghs_app"])

    def test_gh_exits_non_zero_when_the_token_is_empty(self):
        # Plain gh with an empty GH_TOKEN answers from any stored login.
        for token in ("", "ghp_personal"):
            with self.subTest(token=token):
                run = self.wrapped("api", "user", GH_TOKEN=token)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("refusing to run gh", run.stderr)
        run = self.wrapped("api", "user")
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_the_wrapper_refuses_a_config_directory_with_a_stored_login(self):
        (self.config / "hosts.yml").write_text("github.com:\n    oauth_token: gho_person\n")
        run = self.wrapped("api", "user", GH_TOKEN="ghs_app")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("stored login", run.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_wrapper_refuses_gh_auth_login_and_its_siblings(self):
        for verb in ("login", "refresh", "setup-git", "switch", "token"):
            for args in (("auth", verb), ("--hostname", "github.com", "auth", verb, "-h", "x")):
                with self.subTest(args=args):
                    run = self.wrapped(*args, GH_TOKEN="ghs_app")
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn("gh auth %s is refused" % verb, run.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.wrapped("auth", "status", GH_TOKEN="ghs_app").returncode, 0)

    # github_auth.sh and git's credential helper

    def sourced(self, script, **env):
        body = '. %s\ngithub_auth_init %s || exit 7\n%s' % (AUTH, LANES, script)
        return subprocess.run(["bash", "-c", body], env=self.base_env(**env),
                              capture_output=True, text=True, timeout=60)

    def test_init_stops_on_an_ambient_token_or_a_used_config_directory(self):
        (self.config / "hosts.yml").write_text("github.com:\n    user: person\n")
        for name, env, said in (
                ("ambient token", dict(GH_TOKEN="gho_person"), "ambient GH_TOKEN"),
                ("empty ambient token", dict(GH_TOKEN=""), "ambient GH_TOKEN"),
                ("stored login", dict(GH_CONFIG_DIR=str(self.config)), "not an empty directory")):
            with self.subTest(name):
                run = self.sourced("echo ran", **env)
                self.assertEqual(run.returncode, 7, run.stderr)
                self.assertIn(said, run.stderr)
                self.assertNotIn("ran", run.stdout)

    def test_init_gives_a_fresh_empty_config_directory_and_the_wrapper_first(self):
        run = self.sourced('printf "%s\\n" "$GH_CONFIG_DIR" "$(command -v gh)" "$LOOM_REAL_GH"\n'
                           'ls -A "$GH_CONFIG_DIR" | wc -l\ngithub_auth_cleanup\n'
                           '[ ! -e "$GH_CONFIG_DIR" ] && echo removed')
        self.assertEqual(run.returncode, 0, run.stderr)
        config, gh, real, entries, removed = run.stdout.split()
        self.assertTrue(config.startswith(str(self.tmp)), config)
        self.assertEqual(Path(gh), WRAPPER)
        self.assertEqual(real, str(self.bin / "gh"))
        self.assertEqual((entries, removed), ("0", "removed"))

    def test_mint_failure_leaves_no_token_and_gh_refuses(self):
        run = self.sourced('github_auth_mint && echo minted\n'
                           'echo "token=${GH_TOKEN-unset}"\ngh api user || echo "gh=$?"',
                           LOOM_GITHUB_APP_ID="124",
                           LOOM_APP_TOKEN_CMD=self.token_cmd("echo nope >&2; exit 2"))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.split(), ["token=unset", "gh=1"])
        self.assertEqual(self.calls(), [])

    def credential_fill(self, mint):
        # The person's own helper, in their global git config, would answer.
        (self.tmp / ".gitconfig").write_text(
            '[credential]\n\thelper = "!f() { echo username=person; echo password=gho_person; }; f"\n')
        return self.sourced('github_auth_mint\n'
                            'printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill',
                            LOOM_GITHUB_APP_ID="124", LOOM_APP_TOKEN_CMD=self.token_cmd(mint))

    def test_git_asks_only_the_app_token_for_github(self):
        run = self.credential_fill("echo ghs_app")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("username=x-access-token\npassword=ghs_app\n", run.stdout)
        self.assertNotIn("gho_person", run.stdout)

    def test_git_gets_no_credential_without_the_app_token(self):
        run = self.credential_fill("exit 2")
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("gho_person", run.stdout)

    def test_the_helper_answers_nothing_for_another_host(self):
        run = subprocess.run([str(HELPER), "get"], input="protocol=https\nhost=example.com\n\n",
                             env=self.base_env(GH_TOKEN="ghs_app"), capture_output=True,
                             text=True, timeout=60)
        self.assertEqual((run.returncode, run.stdout), (0, ""))


class StationPassAppOnlyTest(unittest.TestCase):
    def test_a_station_pass_without_its_app_refuses_before_any_client(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        settings = station.Settings(home, ["example/repo"], ["operator"], ["example"])
        built = []

        def no_client(auth, *_args, **_kwargs):
            built.append(auth)
            raise AssertionError("the pass built a GitHub client from %r" % type(auth).__name__)

        env = {"HOME": home, "PATH": os.environ.get("PATH", ""),
               "LOOM_GITHUB_TOKEN": "ghp_personal", "GITHUB_TOKEN": "ghp_personal"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(station, "GitHub", no_client):
            with self.assertRaises(GitHubError) as err:
                station.run(settings, None)
        self.assertIn("never falls back to a personal login", str(err.exception))
        self.assertEqual(built, [])


if __name__ == "__main__":
    unittest.main()
