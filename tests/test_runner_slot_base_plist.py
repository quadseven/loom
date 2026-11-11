"""The first tracked opencode-go slot uses the base lane's installed plist."""
from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bus import runners
from bus.github import Response


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def rest(self, method, path, body=None, etag=None, ok=()):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/dispatches"):
            return Response(200, {"workflow_run_id": 1})
        raise AssertionError("unexpected request %s %s" % (method, path))


class RunnerSlotBasePlistTest(unittest.TestCase):
    def test_dispatch_reads_base_opencode_go_plist_when_slot_plist_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            slot = "opencode-go-1"
            ticket = "example.ticket-1"
            inbox = home / "briefs" / slot
            inbox.mkdir(parents=True)
            (inbox / (ticket + ".md")).write_text(
                "# Example ticket\n\nRepo: example/project\nBase: main\n"
                "Branch: example/ticket-1\nLane: opencode-go\n\nDo the work.\n",
                encoding="utf-8")
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            with (agents / "com.loom.opencode-go.plist").open("wb") as stream:
                plistlib.dump({"EnvironmentVariables": {
                    "LOOM_OPENCODE_GO_KEY_SSM": "/example/base-key"}}, stream)
            github = FakeGitHub()
            said = []
            runner = runners.Runners(
                github, str(home), "example/project", [slot],
                {"HOME": str(home), "LOOM_REPO_OWNERS": "example"},
                lambda _repo: True, said.append, actor="example-app[bot]",
                now=1_800_000_000)
            with patch.object(runners.Runners, "public_key", return_value="fake-key"):
                runner.tend()

            posts = [call for call in github.calls if call[0] == "POST"]
            self.assertEqual(len(posts), 1, said)
            settings = posts[0][2]["inputs"]["settings"]
            self.assertIn("LOOM_OPENCODE_GO_KEY_SSM=/example/base-key\n", settings)
            self.assertFalse((inbox / "failed" / (ticket + ".md")).exists())


if __name__ == "__main__":
    unittest.main()
