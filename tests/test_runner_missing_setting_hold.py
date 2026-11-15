"""A slot refused for a missing required setting is held, not left healthy.

Before the hold, the refusal parked the brief but the heartbeat still said
`available: true`, so the factory kept assigning work to a slot that refuses
every run (loom#2910).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bus import runners
from bus.github import Response

KEY = "LOOM_OPENCODE_GO_KEY_SSM"


class FakeGitHub:
    def __init__(self):
        self.posts = []

    def rest(self, method, path, body=None, etag=None, ok=()):
        if method == "POST" and path.endswith("/dispatches"):
            self.posts.append(path)
            return Response(200, {"workflow_run_id": 1})
        raise AssertionError("unexpected request %s %s" % (method, path))


class MissingSettingHoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.slot = "opencode-go-1"
        self.gh = FakeGitHub()
        self.said = []
        self.now = 1_800_000_000

    def tearDown(self):
        self.tmp.cleanup()

    def brief(self, ticket):
        inbox = self.home / "briefs" / self.slot
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / (ticket + ".md")).write_text(
            "# Example\n\nRepo: example/project\nBase: main\n"
            "Branch: example/%s\nLane: opencode-go\n\nDo the work.\n" % ticket,
            encoding="utf-8")

    def tend(self, env, later=0):
        runners.Runners(
            self.gh, str(self.home), "example/project", [self.slot], env,
            lambda _repo: True, self.said.append, actor="example-app[bot]",
            now=self.now + later).tend()

    def beat(self):
        path = self.home / "state" / "usage" / (self.slot + ".json")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_a_refusal_for_a_missing_setting_marks_the_slot_unavailable(self):
        self.brief("one")
        self.tend({"HOME": str(self.home), "LOOM_REPO_OWNERS": "example"})
        beat = self.beat()
        self.assertFalse(beat["available"])
        self.assertIn(KEY, beat["reason"])

    def test_the_slot_becomes_available_once_the_setting_is_present(self):
        self.brief("one")
        env = {"HOME": str(self.home), "LOOM_REPO_OWNERS": "example"}
        self.tend(env)
        self.assertFalse(self.beat()["available"])
        env[KEY] = "/example/opencode-go-key"
        self.tend(env, later=60)
        self.assertTrue(self.beat()["available"])

    def test_a_held_slot_dispatches_nothing_until_the_setting_is_present(self):
        self.brief("one")
        env = {"HOME": str(self.home), "LOOM_REPO_OWNERS": "example"}
        self.tend(env)
        self.brief("two")
        self.tend(env, later=60)
        self.assertEqual(self.gh.posts, [])
        self.assertTrue((self.home / "briefs" / self.slot / "two.md").exists())


if __name__ == "__main__":
    unittest.main()
