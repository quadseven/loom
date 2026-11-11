"""Timeout retry behavior for GitHub App key reads."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import github  # noqa: E402


KEY = "FAKEKEYMATERIAL-not-a-real-key"


class SsmRetryTests(unittest.TestCase):
    def test_app_key_read_retries_a_timeout_and_mints_jwt(self):
        timeout = subprocess.TimeoutExpired(["aws"], 30, output=KEY.encode())
        outcomes = [timeout, KEY]
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return subprocess.CompletedProcess(argv, 0, outcome, "")

        def sign(data, key):
            self.assertEqual(key, KEY)
            return b"signature"

        with mock.patch.object(github.shutil, "which", return_value="/example/aws"), \
                mock.patch.object(github.subprocess, "run", fake_run):
            app = github.AppToken("123", "456",
                                  lambda: github.read_ssm("/example/app-key", "us-east-1"),
                                  sign=sign)
            self.assertTrue(app.jwt())
        self.assertEqual(len(calls), 2)

    def test_two_timeouts_report_retry_without_key_value(self):
        outcomes = [subprocess.TimeoutExpired(["aws"], 30, output=KEY.encode())
                    for _ in range(2)]

        def fake_run(argv, **kwargs):
            raise outcomes.pop(0)

        with mock.patch.object(github.shutil, "which", return_value="/example/aws"), \
                mock.patch.object(github.subprocess, "run", fake_run), \
                self.assertRaises(github.SsmReadError) as raised:
            github.read_ssm("/example/app-key", "us-east-1")
        self.assertEqual(str(raised.exception), "SSM read timed out after 30s (retried once)")
        self.assertNotIn(KEY, str(raised.exception))

    def test_non_timeout_ssm_errors_are_not_retried(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 255, "", "ParameterNotFound")

        with mock.patch.object(github.shutil, "which", return_value="/example/aws"), \
                mock.patch.object(github.subprocess, "run", fake_run), \
                self.assertRaises(github.SsmReadError):
            github.read_ssm("/example/app-key", "us-east-1")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
