"""Tests for the station's overdue hourly step handling (loom#3439).

When claim_new carries over every pass, the step rotation never reaches
check_invariants. This test verifies that overdue hourly steps run ahead
of the rotation and log their overdue wait time.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import station  # noqa: E402
from bus.github import GitHub, TargetReached  # noqa: E402
from bus import invariants  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
AUTHORS = ["alice-example"]
OWNERS = ["example-owner"]


class FakeGitHub:
    """A minimal GitHub fake for testing the step rotation."""

    def __init__(self):
        self.calls = []
        self.target = station.PASS_REQUEST_TARGET
        self.requests = 0
        self.issues = {}
        self.comments = {}
        self.events = {}
        self.pulls = []
        self.search_items = []
        self.bot = AUTHORS[0]

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        self.requests += 1
        if self.target and self.requests > self.target:
            raise TargetReached("request target reached")
        from urllib.parse import urlsplit, parse_qs
        parts = urlsplit(url)
        path = parts.path
        if method == "GET" and path == "/repos/%s/issues" % REPO:
            # Return empty list for briefed tickets query
            return 200, {}, b"[]"
        if method == "GET" and path.startswith("/repos/%s/issues/" % REPO):
            # Return empty for individual issue reads
            return 200, {}, b'{"number": 1, "state": "open", "labels": [], "body": ""}'
        if method == "GET" and "/search/issues" in path:
            return 200, {}, b'{"items": []}'
        if method == "GET" and "/repos/%s" % REPO in path and "/issues" not in path:
            return 200, {}, b'{"full_name": "%s", "private": true, "default_branch": "main"}' % REPO.encode()
        if method == "POST" and "/graphql" in path:
            return 200, {}, b'{"data": {}}'
        return 200, {}, b"{}"

    def rest(self, method, path, data=None):
        return self(method, "https://api.github.com" + path, {}, json.dumps(data).encode() if data else None)


class OverdueHourlyTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "loom")
        self.fake = FakeGitHub()
        self.settings = station.Settings(self.home, [REPO], list(AUTHORS), list(OWNERS),
                                         disk_free_min_gb=0)
        # Set up a lane heartbeat so claim_new has something to do
        d = os.path.join(self.home, "state", "usage")
        os.makedirs(d, exist_ok=True)
        beat = {"lane": "codex", "at": int(time.time()), "available": True, "reason": ""}
        with open(os.path.join(d, "codex.json"), "w") as fh:
            json.dump(beat, fh)

    def write_invariants_state(self, checked_at: float):
        """Write the invariants.json state with a specific checked timestamp."""
        path = os.path.join(self.home, "state", "station", "invariants.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        state = {"checked": checked_at}
        with open(path, "w") as fh:
            json.dump(state, fh)

    def rotation_next(self):
        """Read the rotation file and return the next step index."""
        path = os.path.join(self.home, "state", "station", station.STEP_ROTATION_FILE)
        try:
            with open(path) as fh:
                data = json.load(fh)
            return data.get("next", 0)
        except FileNotFoundError:
            return 0

    def test_check_invariants_runs_when_overdue_despite_claim_new_carryover(self):
        """When claim_new carries over every pass, check_invariants should still
        run within two passes if its last run was more than an hour ago."""
        # Set last check_invariants run to 2 hours ago
        two_hours_ago = time.time() - 2 * 3600
        self.write_invariants_state(two_hours_ago)

        # Make claim_new raise TargetReached on every pass
        def claim_new_carryover(station_self):
            raise TargetReached("claim_new read carried over at request target")

        claim_new_carryover.__name__ = "claim_new"

        err = io.StringIO()
        with mock.patch.object(station.Station, "claim_new", claim_new_carryover), \
             contextlib.redirect_stderr(err):
            # First pass
            st = station.Station(GitHub("token", transport=self.fake), self.settings)
            st._run_steps()
            # Second pass
            st = station.Station(GitHub("token", transport=self.fake), self.settings)
            st._run_steps()

        # check_invariants should have run (it's called from _run_due_steps now)
        # Verify the invariants.json checked timestamp was updated
        path = os.path.join(self.home, "state", "station", "invariants.json")
        with open(path) as fh:
            state = json.load(fh)
        self.assertGreater(state["checked"], two_hours_ago,
                           "check_invariants should have run and updated the checked timestamp")

        # Verify the log contains the overdue message
        log_output = err.getvalue()
        self.assertIn("check_invariants", log_output,
                      "Log should mention check_invariants ran")
        self.assertIn("overdue", log_output.lower() or "waited" in log_output.lower(),
                      "Log should mention how long it waited: %s" % log_output)

    def test_check_invariants_logs_overdue_wait_time(self):
        """The overdue run of check_invariants logs one line naming check_invariants
        and how long it waited."""
        # Set last check_invariants run to 90 minutes ago
        ninety_min_ago = time.time() - 90 * 60
        self.write_invariants_state(ninety_min_ago)

        def claim_new_carryover(station_self):
            raise TargetReached("claim_new read carried over at request target")

        claim_new_carryover.__name__ = "claim_new"

        err = io.StringIO()
        with mock.patch.object(station.Station, "claim_new", claim_new_carryover), \
             contextlib.redirect_stderr(err):
            st = station.Station(GitHub("token", transport=self.fake), self.settings)
            st._run_steps()

        log_output = err.getvalue()
        # Should contain a line like "step check_invariants ran (overdue by 90m)"
        self.assertIn("check_invariants", log_output)
        # Should mention the wait time (approximately 90 minutes / 1.5 hours)
        self.assertTrue(any(word in log_output.lower() for word in ["overdue", "waited", "hour", "min"]),
                        "Log should mention wait time: %s" % log_output)


if __name__ == "__main__":
    unittest.main()