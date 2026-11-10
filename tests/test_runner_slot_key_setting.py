"""A tracked opencode-go slot's key setting, named for the slot alone.

run_lane.sh reads a lane's key setting slot first, then provider
(lane_setting KEY_SSM: LOOM_<SLOT>_KEY_SSM, then LOOM_<LANE>_KEY_SSM), so a
slot whose own settings name its key parameter holds a working key. The
station's dispatch guard and the runner job both read the provider name,
LOOM_OPENCODE_GO_KEY_SSM, so a dispatch built only from the station's
environment parks a slot that names the key only for itself.
"""
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

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class RunnerSlotKeySettingTest(unittest.TestCase):
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

    def dispatch(self, env):
        runner = runners.Runners(
            self.gh, str(self.home), "example/project", [self.slot], env,
            lambda _repo: True, self.said.append, actor="example-app[bot]",
            now=1_800_000_000)
        runner.tend()
        return self.gh.posts()

    def test_dispatch_carries_the_key_named_only_in_the_slots_own_settings(self):
        # The station's environment names no LOOM_OPENCODE_GO_KEY_SSM; the
        # slot's own setting, LOOM_OPENCODE_GO_1_KEY_SSM, names the key.
        posts = self.dispatch({"LOOM_REPO_OWNERS": "example",
                               "LOOM_OPENCODE_GO_1_KEY_SSM": "/example/opencode-go-key"})

        self.assertEqual(len(posts), 1)
        settings = posts[0][2]["inputs"]["settings"]
        self.assertIn("LOOM_OPENCODE_GO_KEY_SSM=/example/opencode-go-key\n", settings)
        self.assertFalse((self.home / "briefs" / self.slot / "failed"
                          / (self.ticket + ".md")).exists())

    def test_lane_settings_carry_the_slots_own_key_under_the_provider_name(self):
        settings = runners.lane_settings(
            "opencode-go-2", {"LOOM_OPENCODE_GO_2_KEY_SSM": "/example/opencode-go-key"})
        self.assertEqual(settings["LOOM_OPENCODE_GO_KEY_SSM"], "/example/opencode-go-key")

    def test_the_provider_setting_wins_when_both_names_are_set(self):
        settings = runners.lane_settings(
            "opencode-go-1", {"LOOM_OPENCODE_GO_KEY_SSM": "/example/provider-key",
                              "LOOM_OPENCODE_GO_1_KEY_SSM": "/example/slot-key"})
        self.assertEqual(settings["LOOM_OPENCODE_GO_KEY_SSM"], "/example/provider-key")


if __name__ == "__main__":
    unittest.main()
