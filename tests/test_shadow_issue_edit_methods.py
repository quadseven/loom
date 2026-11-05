"""Issue edit requests stay held in shadow across REST methods."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from bus import shadow
from bus.github import GitHub, ShadowHeld


REPO = "/".join(("example-owner", "example-repo"))


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, method, url, headers, body):
        self.sent.append((method, url, json.loads(body) if body else None))
        return 200, {}, b"{}"


class IssueEditMethodsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("LOOM_SHADOW")}
        env.update(LOOM_SHADOW="1", LOOM_SHADOW_REPOS=REPO,
                   LOOM_SHADOW_LEDGER=os.path.join(self.tmp.name, "ledger.jsonl"))
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def entries(self):
        return shadow.read(os.environ["LOOM_SHADOW_LEDGER"])

    def test_gh_guard_holds_post_input_close_and_main_mode_allows_it(self):
        with open(os.path.join(self.tmp.name, "close.json"), "w", encoding="utf-8") as fh:
            json.dump({"state": "closed"}, fh)
        argv = ["api", "repos/%s/issues/41" % REPO, "--input", "close.json"]
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(shadow.gh_guard(self.tmp.name, argv), 1)
            entry, = self.entries()
            self.assertEqual(entry["action"], shadow.CLOSE)
            self.assertEqual(entry["repo"], REPO)
            with mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}):
                self.assertEqual(shadow.gh_guard(self.tmp.name, argv), 0)

    def test_rest_holds_post_close_in_shadow_and_sends_it_outside_shadow(self):
        path = "/repos/%s/issues/42" % REPO
        body = {"state": "closed"}
        transport = Recorder()
        with self.assertRaises(ShadowHeld):
            GitHub("token", transport=transport)._rest("POST", path, body)
        self.assertFalse(any(method == "POST" for method, _, _ in transport.sent))
        self.assertEqual(self.entries()[0]["action"], shadow.CLOSE)

        with mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}):
            GitHub("token", transport=transport)._rest("POST", path, body)
        self.assertEqual(sum(method == "POST" for method, _, _ in transport.sent), 1)


if __name__ == "__main__":
    unittest.main()
