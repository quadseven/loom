"""Tests for handling factory class failures on cluster runners."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from bus import station, brain, factory, runners

REPO = "example-owner/example-repo"
TICKET = "example-owner.example-repo-7"


class TestClusterRunnerFactoryFailures(unittest.TestCase):
    """Tests for handling factory failures on cluster runners."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "loom"
        self.inbox = self.home / "briefs"
        self.state = self.home / "state"

        # Create the directory structure
        self.inbox.mkdir(parents=True)
        (self.state / "station").mkdir(parents=True)

        # Create a fake brief for the ticket
        brief_path = self.inbox / (TICKET + ".md")
        brief_content = f"""# Ticket

Repo: {REPO}
Branch: example-owner.example-repo-7
Lane: free-gemma

Do the thing.

Fix-PR: {TICKET}
"""
        brief_path.write_text(brief_content)

        # Create the station
        self.gh = self._create_fake_github()
        self.said = []
        self.public = {REPO: True}
        self.now = 1_800_000_000.0
        self.env = {
            "LOOM_RUNNER_SLOTS": "free-gemma",
            "LOOM_FREE_GEMMA_MODEL": "free/gemini",
            "LOOM_GITHUB_APP_KEY_SSM": "/station/app",
            "LOOM_LANES_GITHUB_APP_ID": "42",
            "LOOM_REPO_OWNERS": "example-owner",
            "HOME": "/nowhere",
        }

        self.station = station.Station(
            self.gh,
            str(self.home),
            REPO,
            runners.runner_slots(self.env),
            self.env,
            self.public,
            self.said.append,
            actor=APP,
            now=self.now,
        )

    def _create_fake_github(self):
        class FakeGitHub:
            def __init__(self):
                self.calls = []
                self.runs = {}
                self.artifacts = {}
                self.zips = {}
                self.next_run = 900
                self.heads = {REPO: "abc123"}
                self.refs = {}

            def rest(self, method, path, body=None, etag=None, ok=()):
                self.calls.append((method, path, body))
                if method == "POST" and path.endswith("/dispatches"):
                    self.next_run += 1
                    title = "lane free-gemma %s" % self.next_run
                    self.runs[self.next_run] = {
                        "id": self.next_run,
                        "display_title": title,
                        "head_branch": "main",
                        "path": ".github/workflows/lane.yml",
                        "event": "workflow_dispatch",
                        "triggering_actor": {"login": "loom-station[bot]"},
                    }
                    return type("Response", (), {"data": {"workflow_run_id": self.next_run}})()
                if method == "POST" and path.endswith("/cancel"):
                    return type("Response", (), {"data": {}})()
                parts = path.split("/")
                if path.endswith("/artifacts"):
                    return type("Response", (), {"data": {"artifacts": self.artifacts.get(int(parts[-2]), [])}})()
                if path.endswith("/zip"):
                    return type("Response", (), {"headers": {"location": "https://blob.example.com/%s" % parts[-2]}})()
                if "/actions/runs/" in path:
                    return type("Response", (), {"data": self.runs[int(parts[-1])]})()
                if "/commits/" in path:
                    ref = parts[-1]
                    sha = self.heads["/".join(parts[2:4])] if ref == "HEAD" else self.refs.get(ref)
                    return type("Response", (), {"data": {"sha": sha}})()
                raise AssertionError(f"unexpected request {method} {path}")

            def posts(self, suffix):
                return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]

        return FakeGitHub()

    def test_factory_failure_with_no_incident_filed_on_cluster_runner(self):
        """Test that a factory failure with 'no incident filed on a cluster runner' retries on another lane."""
        # Simulate a factory failure where no incident could be filed on a cluster runner
        reason = "[factory] setup failed: interpreter python3.13 is not on PATH; no incident filed on a cluster runner: another lane takes the ticket"
        bare = "interpreter python3.13 is not on PATH; no incident filed on a cluster runner: another lane takes the ticket"

        # The station should retry this as transient on another lane
        result = self.station._factory(REPO, TICKET, "free-gemma", reason)

        # The station should have attempted to retry the ticket on another lane
        self.assertTrue(result, "station should attempt to retry the ticket")
        # Check that the station tried to hold the ticket for retry
        self.assertIn("transient", self.said[0])

    def test_factory_failure_base_check_exited_on_unmodified_repo(self):
        """Test that a factory failure with base check exit on unmodified repo retries on another lane."""
        # Simulate a factory failure where the base check exited on the unmodified repo
        reason = "[factory] setup failed: base check exited 1 on the unmodified repo; factory incident %s#99" % REPO
        bare = "base check exited 1 on the unmodified repo"

        # The station should retry this as transient on another lane
        result = self.station._factory(REPO, TICKET, "free-gemma", reason)

        # The station should have attempted to retry the ticket on another lane
        self.assertTrue(result, "station should attempt to retry the ticket")
        # Check that the station tried to hold the ticket for retry
        self.assertIn("transient", self.said[0])

    def test_factory_failure_bwrap_narrows_no_network(self):
        """Test that a factory failure with 'bwrap narrows no network' retries on another lane."""
        # Simulate a factory failure where bwrap narrows the network
        reason = "[factory] setup failed: bwrap narrows no network; no incident filed on a cluster runner: another lane takes the ticket"
        bare = "bwrap narrows no network; no incident filed on a cluster runner: another lane takes the ticket"

        # The station should retry this as transient on another lane
        result = self.station._factory(REPO, TICKET, "free-gemma", reason)

        # The station should have attempted to retry the ticket on another lane
        self.assertTrue(result, "station should attempt to retry the ticket")
        # Check that the station tried to hold the ticket for retry
        self.assertIn("transient", self.said[0])

    def test_work_failure_with_base_check_exit(self):
        """Test that a work failure with base check exit triggers retry."""
        # Simulate a work failure that includes a base check exit
        reason = "[work] validation failed: the check exited 1 at abc (base check exited 0); [factory] setup failed: base check exited 1 on the unmodified repo; factory incident %s#99" % REPO
        bare = "validation failed: the check exited 1 at abc (base check exited 0); [factory] setup failed: base check exited 1 on the unmodified repo"

        # The station should retry this as transient on another lane
        result = self.station._factory(REPO, TICKET, "free-gemma", reason)

        # The station should have attempted to retry the ticket on another lane
        self.assertTrue(result, "station should attempt to retry the ticket")


if __name__ == "__main__":
    unittest.main()
