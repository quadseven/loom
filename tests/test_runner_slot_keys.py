"""Configuration and dispatch guards for tracked runner slot keys."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


class RunnerSlotKeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.slot = "opencode-go-1"
        self.ticket = "example.ticket-1"
        inbox = self.home / "briefs" / self.slot
        inbox.mkdir(parents=True)
        (inbox / (self.ticket + ".md")).write_text(
            "# Example ticket\n\nRepo: example/project\nBase: main\n"
            "Branch: example/ticket-1\nLane: opencode-go\n\nDo the work.\n",
            encoding="utf-8")
        self.gh = FakeGitHub()
        self.said = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_station_env_keeps_tracked_provider_key_setting_without_enabled_slot(self):
        env = {"LOOM_OPENCODE_GO_KEY_SSM": "/example/parameter",
               "LOOM_RUNNER_SLOTS": ""}

        settings = runners.station_env(env)

        self.assertEqual(settings.get("LOOM_OPENCODE_GO_KEY_SSM"), "/example/parameter")

    def test_tracked_slot_without_provider_key_is_not_dispatched(self):
        runner = runners.Runners(
            self.gh, str(self.home), "example/project", [self.slot],
            {"LOOM_REPO_OWNERS": "example"}, lambda _repo: True,
            self.said.append, actor="example-app[bot]", now=1_800_000_000)

        runner.tend()

        self.assertFalse(any(call[0] == "POST" for call in self.gh.calls))
        self.assertTrue(any("LOOM_OPENCODE_GO_KEY_SSM" in line for line in self.said))
        self.assertTrue((self.home / "briefs" / self.slot / "failed"
                         / (self.ticket + ".md")).exists())


if __name__ == "__main__":
    unittest.main()
