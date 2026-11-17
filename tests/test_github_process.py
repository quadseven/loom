"""The shared gh wrapper and GitHub-only git credential helper."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANES = ROOT / "lanes"


class GithubProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "real-gh.log"
        self.real_gh = self.root / "real-gh"
        self.real_gh.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*|${GH_TOKEN:-}|${GH_CONFIG_DIR:-}\" >> \"$FAKE_LOG\"\n")
        self.real_gh.chmod(0o755)
        self.config = self.root / "gh-config"
        self.config.mkdir()
        self.git_log = self.root / "git-app.log"
        self.git_bin = self.root / "bin"
        self.git_bin.mkdir()
        fake_git = self.git_bin / "git"
        fake_git.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_GIT_LOG\"\n")
        fake_git.chmod(0o755)

    def run_wrapper(self, *args, token="ghs_app-token", config=None, **extra):
        env = dict(os.environ, LOOM_REAL_GH=str(self.real_gh),
                   GH_CONFIG_DIR=str(config or self.config), GH_TOKEN=token,
                   FAKE_LOG=str(self.log))
        env.update(extra)
        return subprocess.run([str(LANES / "gh"), *args], env=env,
                              capture_output=True, text=True)

    def test_wrapper_refuses_to_run_without_a_token(self):
        result = self.run_wrapper("api", "rate_limit", token="")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no valid process GitHub App token", result.stderr)
        self.assertFalse(self.log.exists())

    def test_wrapper_rejects_a_personal_token(self):
        result = self.run_wrapper("api", "rate_limit", token="ghp_personal")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no valid process GitHub App token", result.stderr)
        self.assertFalse(self.log.exists())

    def test_wrapper_rejects_enterprise_credentials_and_hosts(self):
        cases = ({"GH_ENTERPRISE_TOKEN": "enterprise-token"},
                 {"GITHUB_ENTERPRISE_TOKEN": "enterprise-token"},
                 {"GH_HOST": "your-server.internal"})
        for settings in cases:
            with self.subTest(settings=tuple(settings)):
                result = self.run_wrapper("api", "rate_limit", **settings)
                self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.log.exists())

    def test_wrapper_blocks_authentication_mutations(self):
        for command in ("login", "logout", "refresh", "setup-git", "switch", "token"):
            with self.subTest(command=command):
                result = self.run_wrapper("auth", command)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("authentication changes are disabled", result.stderr)
        self.assertFalse(self.log.exists())

    def test_wrapper_calls_gh_with_the_app_token_and_empty_config(self):
        result = self.run_wrapper("api", "rate_limit")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("api rate_limit|ghs_app-token|%s\n" % self.config,
                         self.log.read_text())

    def test_wrapper_refuses_a_nonempty_config_directory(self):
        (self.config / "hosts.yml").write_text("oauth_token: stored\n")
        result = self.run_wrapper("api", "rate_limit")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("GH_CONFIG_DIR is not empty", result.stderr)
        self.assertFalse(self.log.exists())

    def test_git_credential_helper_answers_only_for_https_github(self):
        env = dict(os.environ, GH_TOKEN="ghs_app-token")
        helper = str(LANES / "git-credential-app")
        github = subprocess.run([helper, "get"], input="protocol=https\nhost=github.com\n\n",
                                env=env, capture_output=True, text=True, check=True)
        self.assertEqual("username=x-access-token\npassword=ghs_app-token\n\n", github.stdout)
        other = subprocess.run([helper, "get"], input="protocol=https\nhost=example.com\n\n",
                               env=env, capture_output=True, text=True, check=True)
        self.assertEqual("", other.stdout)

    def test_git_app_attaches_only_the_app_credential_helper(self):
        helper = self.root / "credential helper"
        env = dict(os.environ, GH_TOKEN="ghs_app-token", FAKE_GIT_LOG=str(self.git_log),
                   LOOM_GIT_CREDENTIAL_HELPER_ESCAPED="'" + str(helper) + "'",
                   PATH=str(self.git_bin) + ":" + os.environ["PATH"])
        run = subprocess.run([str(LANES / "git-app"), "push", "origin", "main"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)
        args = self.git_log.read_text()
        self.assertIn("-c credential.helper=", args)
        self.assertIn("credential.helper=!" + env["LOOM_GIT_CREDENTIAL_HELPER_ESCAPED"], args)
        self.assertIn("push origin main", args)

    def test_shared_setup_uses_a_fresh_empty_config_and_puts_wrapper_first(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake_gh = bin_dir / "gh"
        fake_gh.write_text("#!/bin/sh\nexit 0\n")
        fake_gh.chmod(0o755)
        run_dir = self.root / "run"
        command = (
            '. "$1/lanes/github_process.sh"; '
            'loom_github_start "$1" "$2" || exit; '
            'printf "%s\\n%s\\n%s\\n" "$GH_CONFIG_DIR" "${PATH%%:*}" "$(ls -A "$GH_CONFIG_DIR")"; '
            'loom_github_cleanup'
        )
        env = {"PATH": str(bin_dir) + ":/usr/bin:/bin", "HOME": str(self.root)}
        result = subprocess.run(["bash", "-c", command, "bash", str(ROOT), str(run_dir)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        config, first_path, contents = result.stdout.splitlines()
        self.assertEqual(str(run_dir), str(Path(config).parent))
        self.assertEqual(str(LANES), first_path)
        self.assertEqual("", contents)


if __name__ == "__main__":
    unittest.main()
