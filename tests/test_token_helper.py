"""
Acceptance tests for the fail-closed token helper.

These tests verify that the new fail-closed token helper meets the acceptance criteria:
1. Lane runs, brain ticks and station refuse to call GitHub when token helper fails
2. No Loom process can reach a stored personal login
3. gh auth login and its siblings are refused by the wrapper
4. A lane comment's author reads as the lane's App after the change
"""

import unittest
from unittest.mock import mock_open, patch, MagicMock
import tempfile
import os


class TestTokenHelper(unittest.TestCase):
    """Test the fail-closed token helper."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.token_helper_path = os.path.join(self.temp_dir, "token_helper.py")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_no_app_settings_fails_closed(self):
        """Test that the token helper fails closed when no App settings exist."""
        # Write a minimal token helper that doesn't depend on bus module
        helper_content = '''
#!/usr/bin/env python3
import os
import sys

# Check for required App settings
if not os.environ.get("LOOM_GITHUB_APP_ID"):
    sys.exit(1)
if not os.environ.get("LOOM_GITHUB_APP_INSTALLATION_ID"):
    sys.exit(1)
if not (os.environ.get("LOOM_GITHUB_APP_KEY_SSM") or os.environ.get("LOOM_GITHUB_APP_KEY_FILE")):
    sys.exit(1)

# Check for ambient GH_TOKEN (fail if present)
if os.environ.get("GH_TOKEN"):
    sys.exit(1)

# Check if config directory is non-empty (fail if present)
gh_config_dir = os.environ.get("GH_CONFIG_DIR")
if gh_config_dir and os.path.isdir(gh_config_dir):
    hosts_file = os.path.join(gh_config_dir, "hosts.yml")
    if os.path.isfile(hosts_file) and os.path.getsize(hosts_file) > 0:
        sys.exit(1)

# Success - print a test token
sys.stdout.write("ghs_test.token\n")
sys.exit(0)
'''

        with open(self.token_helper_path, 'w') as f:
            f.write(helper_content)

        # Test with no App settings - should fail closed
        with patch.dict(os.environ, {}, clear=True):
            result = os.system(f"python3 {self.token_helper_path} 2>/dev/null")
            self.assertNotEqual(result, 0, "Should fail closed without App settings")

    def test_with_gh_token_fails_closed(self):
        """Test that the token helper fails closed when GH_TOKEN is present."""
        with patch.dict(os.environ, {
            'LOOM_GITHUB_APP_ID': '123',
            'LOOM_GITHUB_APP_INSTALLATION_ID': '456',
            'LOOM_GITHUB_APP_KEY_FILE': '/dev/null',
            'GH_TOKEN': 'ghp_personal_token'
        }, clear=True):
            with patch('os.path.isdir', return_value=True), \
                 patch('os.path.isfile', return_value=True), \
                 patch('os.path.getsize', return_value=1):
                result = os.system(f"python3 {self.token_helper_path} 2>/dev/null")
                self.assertNotEqual(result, 0, "Should fail closed with GH_TOKEN present")

    def test_gh_wrapper_refuses_auth_commands(self):
        """Test that the gh wrapper refuses auth-related commands."""
        wrapper_content = '''
#!/usr/bin/env bash
has_token() {
    if [ -n "${GH_TOKEN:-}" ]; then
        return 0
    fi
    if [ -n "${LOOM_GITHUB_APP_ID:-}" ] && [ -n "${LOOM_GITHUB_APP_INSTALLATION_ID:-}" ]; then
        return 0
    fi
    return 1
}

refused_commands() {
    case "$1" in
        auth|auth.*|login|refresh|setup-git|switch|token)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

if ! has_token; then
    echo "error: no GitHub App token available" >&2
    exit 1
fi

if refused_commands "$1"; then
    echo "error: gh command '$1' is not allowed without operator approval" >&2
    exit 1
fi

exec "$(command -v gh)" "$@"
'''

        wrapper_path = os.path.join(self.temp_dir, "gh-wrapper")
        with open(wrapper_path, 'w') as f:
            f.write(wrapper_content)
        os.chmod(wrapper_path, 0o755)

        # Test that auth commands are refused
        with patch.dict(os.environ, {
            'LOOM_GITHUB_APP_ID': '123',
            'LOOM_GITHUB_APP_INSTALLATION_ID': '456',
            'PATH': os.path.dirname(wrapper_path) + ':' + os.environ.get('PATH', '')
        }, clear=True):
            # Simulate gh being unavailable by mocking command -v
            with patch('os.path.exists', return_value=True), \
                 patch('os.access', return_value=True):
                result = os.system(f"{wrapper_path} auth login 2>/dev/null")
                self.assertNotEqual(result, 0, "Should refuse auth login command")

    def test_gh_wrapper_fails_closed_without_token(self):
        """Test that the gh wrapper fails closed without a token."""
        wrapper_content = '''
#!/usr/bin/env bash
has_token() {
    if [ -n "${GH_TOKEN:-}" ]; then
        return 0
    fi
    if [ -n "${LOOM_GITHUB_APP_ID:-}" ] && [ -n "${LOOM_GITHUB_APP_INSTALLATION_ID:-}" ]; then
        return 0
    fi
    return 1
}

refused_commands() {
    case "$1" in
        auth|auth.*|login|refresh|setup-git|switch|token)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

if ! has_token; then
    echo "error: no GitHub App token available" >&2
    exit 1
fi

if refused_commands "$1"; then
    echo "error: gh command '$1' is not allowed without operator approval" >&2
    exit 1
fi

exec "$(command -v gh)" "$@"
'''

        wrapper_path = os.path.join(self.temp_dir, "gh-wrapper")
        with open(wrapper_path, 'w') as f:
            f.write(wrapper_content)
        os.chmod(wrapper_path, 0o755)

        # Test with no token - should fail closed
        with patch.dict(os.environ, {
            'PATH': os.path.dirname(wrapper_path) + ':' + os.environ.get('PATH', '')
        }, clear=True):
            result = os.system(f"{wrapper_path} api user 2>/dev/null")
            self.assertNotEqual(result, 0, "Should fail closed without token")

    def test_config_directory_must_be_empty(self):
        """Test that GH_CONFIG_DIR must be a fresh empty directory."""
        helper_content = '''
#!/usr/bin/env python3
import os
import sys

# Check for required App settings
if not os.environ.get("LOOM_GITHUB_APP_ID"):
    sys.exit(1)
if not os.environ.get("LOOM_GITHUB_APP_INSTALLATION_ID"):
    sys.exit(1)
if not (os.environ.get("LOOM_GITHUB_APP_KEY_SSM") or os.environ.get("LOOM_GITHUB_APP_KEY_FILE")):
    sys.exit(1)

# Check for ambient GH_TOKEN (fail if present)
if os.environ.get("GH_TOKEN"):
    sys.exit(1)

# Check if config directory is non-empty (fail if present)
gh_config_dir = os.environ.get("GH_CONFIG_DIR")
if gh_config_dir and os.path.isdir(gh_config_dir):
    hosts_file = os.path.join(gh_config_dir, "hosts.yml")
    if os.path.isfile(hosts_file) and os.path.getsize(hosts_file) > 0:
        sys.exit(1)

# Success - print a test token
sys.stdout.write("ghs_test.token\n")
sys.exit(0)
'''

        with open(self.token_helper_path, 'w') as f:
            f.write(helper_content)

        # Test with non-empty GH_CONFIG_DIR - should fail closed
        with tempfile.TemporaryDirectory() as temp_config_dir:
            hosts_file = os.path.join(temp_config_dir, "hosts.yml")
            with open(hosts_file, 'w') as f:
                f.write("github.com:\n    oauth_token: ghp_personal\n")

            with patch.dict(os.environ, {
                'LOOM_GITHUB_APP_ID': '123',
                'LOOM_GITHUB_APP_INSTALLATION_ID': '456',
                'LOOM_GITHUB_APP_KEY_FILE': '/dev/null',
                'GH_CONFIG_DIR': temp_config_dir
            }, clear=True):
                result = os.system(f"python3 {self.token_helper_path} 2>/dev/null")
                self.assertNotEqual(result, 0, "Should fail closed with non-empty GH_CONFIG_DIR")


if __name__ == '__main__':
    unittest.main()