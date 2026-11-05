"""A roll can pin text fields and a submodule gitlink in the same PR."""
import json
import unittest
from types import SimpleNamespace

from bus import roll_pin
from tests.test_roll_pin import FakeGitHub, SOURCE, DEPLOY, FIRST, OLD, GOAL, DOC


class MixedGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.doc = ("# Ship\n```loom-roll-pins\n" + json.dumps({
            "deployment_repo": DEPLOY, "build_workflow": "Build image", "sources": [{
                "repo": SOURCE, "pins": [
                    {"path": "src/pins.env", "key": "MODULE_SHA"},
                    {"path": "src/module", "kind": "gitlink"},
                    {"path": "src/drift.py", "kind": "dict_dev", "entry": "MODULE"}]}]}) + "\n```\n")
        self.base = {
            "src/pins.env": "MODULE_SHA=%s\nOTHER=keep\n" % OLD,
            "src/drift.py": 'DEV_AHEAD = {"MODULE": {"dev": "%s", "deployed_dev": "%s"}}\n'
                            % (OLD, OLD)}
        self.gitlink = OLD

    def rest(self, method, path, body=None, ok=()):
        if path in ("/repos/%s/git/commits/%s" % (DEPLOY, "e" * 40),
                    "/repos/%s/git/commits/%s" % (DEPLOY, "f" * 40)):
            return SimpleNamespace(status=200, data={"tree": {"sha": "t" * 40}})
        if path == "/repos/%s/git/trees/%s?recursive=1" % (DEPLOY, "t" * 40):
            return SimpleNamespace(status=200, data={"tree": [{"path": "src/module",
                "mode": "160000", "sha": self.gitlink}]})
        if path == "/repos/%s/git/trees" % DEPLOY and method == "POST":
            self.gitlink = body["tree"][0]["sha"]
            self.writes.append((method, path, body))
            return SimpleNamespace(status=201, data={"sha": "u" * 40})
        if path == "/repos/%s/git/commits" % DEPLOY and method == "POST":
            self.writes.append((method, path, body))
            return SimpleNamespace(status=201, data={"sha": "e" * 40})
        if path.startswith("/repos/%s/git/refs/heads/loom%%2Froll-pin-" % DEPLOY) \
                and method == "PATCH":
            self.writes.append((method, path, body))
            return SimpleNamespace(status=200, data={})
        return super().rest(method, path, body, ok)


class MixedPinTest(unittest.TestCase):
    def test_merge_moves_env_dev_field_and_gitlink_only(self):
        gh = MixedGitHub()
        gh.merges[SOURCE] = [{"number": 1, "merged_at": "2030-01-01T00:00:00Z",
                              "merge_commit_sha": FIRST}]
        roll_pin.run(gh, [{"goal": GOAL, "repo": "example-owner/project", "path": DOC}])
        self.assertEqual(gh.gitlink, FIRST)
        self.assertEqual(gh.branch["src/pins.env"], "MODULE_SHA=%s\nOTHER=keep\n" % FIRST)
        self.assertEqual(gh.branch["src/drift.py"],
                         'DEV_AHEAD = {"MODULE": {"dev": "%s", "deployed_dev": "%s"}}\n'
                         % (FIRST, OLD))
        self.assertEqual(len(gh.pulls), 1)


if __name__ == "__main__":
    unittest.main()
