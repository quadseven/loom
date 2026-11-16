#!/usr/bin/env python3
"""
Fail-closed GitHub App token helper for Loom processes.

This helper mints installation tokens for the process's GitHub App and provides
a fail-closed interface where any failure path results in a non-zero exit code
with empty output, ensuring Loom processes cannot reach a stored personal login.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from bus.github import AppToken, GitHub, GitHubError, urllib_transport
from bus.app import _key_reader, _LEVEL


def print_token() -> int:
    """
    Fail-closed token helper.

    Prints the installation token to stdout (only the token, no error messages).
    Returns 0 on success, non-zero on failure with empty stderr output.
    """
    try:
        # Check for required App settings
        if not os.environ.get("LOOM_GITHUB_APP_ID"):
            return 1
        if not os.environ.get("LOOM_GITHUB_APP_INSTALLATION_ID"):
            return 1
        if not (os.environ.get("LOOM_GITHUB_APP_KEY_SSM") or os.environ.get("LOOM_GITHUB_APP_KEY_FILE")):
            return 1

        # Check for ambient GH_TOKEN (fail if present)
        if os.environ.get("GH_TOKEN"):
            return 1

        # Check if config directory is non-empty (fail if present)
        gh_config_dir = os.environ.get("GH_CONFIG_DIR")
        if gh_config_dir and os.path.isdir(gh_config_dir):
            # Check if hosts.yml exists and has content
            hosts_file = os.path.join(gh_config_dir, "hosts.yml")
            if os.path.isfile(hosts_file) and os.path.getsize(hosts_file) > 0:
                return 1

        # Create the AppToken
        key_reader = _key_reader(os.environ)
        token_obj = AppToken(
            os.environ["LOOM_GITHUB_APP_ID"],
            os.environ["LOOM_GITHUB_APP_INSTALLATION_ID"],
            key_reader
        )

        # Get the installation token
        gh = GitHub(token_obj, transport=urllib_transport)
        token = gh.app_token()

        # Print only the token (no error messages)
        sys.stdout.write(token + "\n")
        return 0

    except (GitHubError, ValueError, KeyError, OSError):
        # All failure paths return non-zero with empty output
        return 1


if __name__ == "__main__":
    sys.exit(print_token())