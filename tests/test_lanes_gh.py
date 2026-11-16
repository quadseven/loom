"""Tests for the Loom lanes GitHub authentication wrapper (loom#3083)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus.github import AppToken, GitHub, GitHubError, auth_from_env  # noqa: E402


KEY = ("-----BEGIN PRIVATE KEY-----\n"
       "FAKEKEYMATERIALnotarealkeyFAKEKEYMATERIAL0123456789abcdefABCDEF\n"
       "-----END PRIVATE KEY-----\n")
KEY_MARK = "FAKEKEYMATERIAL"


class LanesGhTests(unittest.TestCase):
    """Tests for the lanes/gh wrapper and lanes/gh-token helper."""

    def setUp(self):
        self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.lanes_dir = os.path.join(self.repo_root, "lanes")
        self.gh_token = os.path.join(self.lanes_dir, "gh-token")
        self.gh_wrapper = os.path.join(self.lanes_dir, "gh")
        self.git_cred = os.path.join(self.lanes_dir, "git-credential-loom")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # Write a fake key file for testing
        self.key_file = os.path.join(self.tmpdir.name, "app-key.pem")
        with open(self.key_file, "w") as f:
            f.write(KEY)

    def test_gh_token_script_exists_and_executable(self):
        self.assertTrue(os.path.exists(self.gh_token))
        self.assertTrue(os.access(self.gh_token, os.X_OK))

    def test_gh_wrapper_script_exists_and_executable(self):
        self.assertTrue(os.path.exists(self.gh_wrapper))
        self.assertTrue(os.access(self.gh_wrapper, os.X_OK))

    def test_git_credential_script_exists_and_executable(self):
        self.assertTrue(os.path.exists(self.git_cred))
        self.assertTrue(os.access(self.git_cred, os.X_OK))

    def test_gh_token_fails_without_app_settings(self):
        """gh-token fails when App settings are not configured."""
        env = {"PATH": "/usr/bin:/bin", "HOME": self.tmpdir.name}
        result = subprocess.run([self.gh_token], capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gh-token:", result.stderr)

    def test_gh_wrapper_refuses_auth_login(self):
        """gh wrapper refuses 'gh auth login'."""
        with tempfile.TemporaryDirectory() as homedir:
            env = {"PATH": "/usr/bin:/bin", "HOME": homedir, "LOOM_HOME": os.path.join(homedir, "loom")}
            with tempfile.TemporaryDirectory() as bindir:
                fake_gh = os.path.join(bindir, "gh")
                with open(fake_gh, "w") as f:
                    f.write("#!/bin/bash\necho 'real gh'\n")
                os.chmod(fake_gh, 0o755)
                env["PATH"] = bindir + ":" + env["PATH"]
                result = subprocess.run([self.gh_wrapper, "auth", "login"], capture_output=True, text=True, env=env, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("auth login is not permitted", result.stderr)

    def test_gh_wrapper_auth_token_calls_gh_token(self):
        """gh wrapper 'auth token' calls gh-token and prints the token."""
        with tempfile.TemporaryDirectory() as homedir:
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": homedir,
                "LOOM_HOME": os.path.join(homedir, "loom"),
            }
            with tempfile.TemporaryDirectory() as bindir:
                # Create a fake gh that the wrapper will find
                fake_gh = os.path.join(bindir, "gh")
                with open(fake_gh, "w") as f:
                    f.write("#!/bin/bash\necho 'real gh'\n")
                os.chmod(fake_gh, 0o755)
                # Replace gh-token with a mock that prints a fake token
                original_gh_token = self.gh_token
                mock_gh_token = os.path.join(bindir, "gh-token")
                with open(mock_gh_token, "w") as f:
                    f.write("#!/bin/bash\necho 'ghs_fake_token'\n")
                os.chmod(mock_gh_token, 0o755)
                # Temporarily replace the gh-token in lanes dir
                os.rename(original_gh_token, original_gh_token + ".bak")
                try:
                    os.symlink(mock_gh_token, original_gh_token)
                    env["PATH"] = bindir + ":" + env["PATH"]
                    result = subprocess.run([self.gh_wrapper, "auth", "token"], capture_output=True, text=True, env=env, timeout=30)
                finally:
                    os.remove(original_gh_token)
                    os.rename(original_gh_token + ".bak", original_gh_token)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "ghs_fake_token")

    def test_gh_wrapper_other_commands_use_gh_token(self):
        """gh wrapper sets GH_TOKEN from gh-token for other commands."""
        with tempfile.TemporaryDirectory() as homedir:
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": homedir,
                "LOOM_HOME": os.path.join(homedir, "loom"),
            }
            with tempfile.TemporaryDirectory() as bindir:
                # Create a fake gh that prints GH_TOKEN
                fake_gh = os.path.join(bindir, "gh")
                with open(fake_gh, "w") as f:
                    f.write("#!/bin/bash\necho GH_TOKEN=$GH_TOKEN\n")
                os.chmod(fake_gh, 0o755)
                # Replace gh-token with a mock
                original_gh_token = self.gh_token
                mock_gh_token = os.path.join(bindir, "gh-token")
                with open(mock_gh_token, "w") as f:
                    f.write("#!/bin/bash\necho 'ghs_fake_token'\n")
                os.chmod(mock_gh_token, 0o755)
                os.rename(original_gh_token, original_gh_token + ".bak")
                try:
                    os.symlink(mock_gh_token, original_gh_token)
                    env["PATH"] = bindir + ":" + env["PATH"]
                    result = subprocess.run([self.gh_wrapper, "api", "/rate_limit"], capture_output=True, text=True, env=env, timeout=30)
                finally:
                    os.remove(original_gh_token)
                    os.rename(original_gh_token + ".bak", original_gh_token)
            self.assertEqual(result.returncode, 0)
            self.assertIn("GH_TOKEN=ghs_fake_token", result.stdout)

    def test_git_credential_helper_outputs_token(self):
        """git-credential-loom outputs credential with App token."""
        with tempfile.TemporaryDirectory() as homedir:
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": homedir,
                "LOOM_HOME": os.path.join(homedir, "loom"),
            }
            with tempfile.TemporaryDirectory() as bindir:
                # Replace gh-token with a mock
                original_gh_token = self.gh_token
                mock_gh_token = os.path.join(bindir, "gh-token")
                with open(mock_gh_token, "w") as f:
                    f.write("#!/bin/bash\necho 'ghs_fake_token'\n")
                os.chmod(mock_gh_token, 0o755)
                os.rename(original_gh_token, original_gh_token + ".bak")
                try:
                    os.symlink(mock_gh_token, original_gh_token)
                    env["PATH"] = bindir + ":" + env["PATH"]
                    input_data = "protocol=https\nhost=github.com\n\n"
                    result = subprocess.run([self.git_cred], input=input_data, capture_output=True, text=True, env=env, timeout=30)
                finally:
                    os.remove(original_gh_token)
                    os.rename(original_gh_token + ".bak", original_gh_token)
        self.assertEqual(result.returncode, 0)
        self.assertIn("protocol=https", result.stdout)
        self.assertIn("host=github.com", result.stdout)
        self.assertIn("username=loom", result.stdout)
        self.assertIn("password=ghs_fake_token", result.stdout)

    def test_git_credential_helper_ignores_non_github(self):
        """git-credential-loom ignores non-GitHub hosts."""
        env = {"PATH": "/usr/bin:/bin", "HOME": self.tmpdir.name}
        input_data = "protocol=https\nhost=gitlab.com\n\n"
        result = subprocess.run([self.git_cred], input=input_data, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_git_credential_helper_ignores_non_https(self):
        """git-credential-loom ignores non-HTTPS protocols."""
        env = {"PATH": "/usr/bin:/bin", "HOME": self.tmpdir.name}
        input_data = "protocol=ssh\nhost=github.com\n\n"
        result = subprocess.run([self.git_cred], input=input_data, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class LanePathSetupTests(unittest.TestCase):
    """Tests that lane scripts set up PATH correctly."""

    def setUp(self):
        self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_run_lane_sh_has_lanes_dir_in_path(self):
        """run_lane.sh prepends lanes directory to PATH."""
        path = os.path.join(self.repo_root, "lanes", "run_lane.sh")
        with open(path) as f:
            content = f.read()
        self.assertIn('LANES_DIR="$(cd "$(dirname "$0")" && pwd)"', content)
        self.assertIn('PATH="$LANES_DIR:$PATH', content)

    def test_brain_sh_has_lanes_dir_in_path(self):
        """mac/brain.sh prepends lanes directory to PATH."""
        path = os.path.join(self.repo_root, "lanes", "mac", "brain.sh")
        with open(path) as f:
            content = f.read()
        self.assertIn('LANES_DIR="$(cd "$(dirname "$0")/.." && pwd)"', content)
        self.assertIn('PATH="$LANES_DIR:$PATH', content)

    def test_station_sh_has_lanes_dir_in_path(self):
        """mac/station.sh prepends lanes directory to PATH."""
        path = os.path.join(self.repo_root, "lanes", "mac", "station.sh")
        with open(path) as f:
            content = f.read()
        self.assertIn('LANES_DIR="$(cd "$(dirname "$0")/.." && pwd)"', content)
        self.assertIn('PATH="$LANES_DIR:$PATH"', content)

    def test_run_lane_sh_configures_git_credential_helper(self):
        """run_lane.sh configures git credential helper."""
        path = os.path.join(self.repo_root, "lanes", "run_lane.sh")
        with open(path) as f:
            content = f.read()
        self.assertIn('git config --global credential.helper', content)
        self.assertIn('git-credential-loom', content)

    def test_brain_sh_configures_git_credential_helper(self):
        """mac/brain.sh configures git credential helper."""
        path = os.path.join(self.repo_root, "lanes", "mac", "brain.sh")
        with open(path) as f:
            content = f.read()
        self.assertIn('git config --global credential.helper', content)
        self.assertIn('git-credential-loom', content)

    def test_station_sh_configures_git_credential_helper(self):
        """mac/station.sh configures git credential helper."""
        path = os.path.join(self.repo_root, "lanes", "mac", "station.sh")
        with open(path) as f:
            content = f.read()
        self.assertIn('git config --global credential.helper', content)
        self.assertIn('git-credential-loom', content)


if __name__ == "__main__":
    unittest.main()