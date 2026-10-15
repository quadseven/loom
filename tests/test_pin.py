"""A station pin pass against a stateful fake GitHub API."""
import base64
import hashlib
import unittest
from types import SimpleNamespace

from bus import hunt_plan, pin

SOURCE = "example-owner/loom"
TARGET = "example-owner/deploy"
OLD = "a" * 40
NEW = "b" * 40
NEXT = "c" * 40
PATH = "dashboard/pin.env"
SPEC = {"repo": TARGET, "path": PATH, "revision_var": "LOOM_REVISION",
        "sha256_var": "LOOM_SHA256"}


class FakeGitHub:
    def __init__(self):
        self.base = 'LOOM_REVISION="%s"\nLOOM_SHA256="%s"\n' % (OLD, "0" * 64)
        self.branch = None
        self.pull = None
        self.created = 0
        self.calls = []
        self.green = True

    def rest(self, method, path, body=None, ok=()):
        self.calls.append((method, path, body))
        if "/check-runs?" in path:
            return SimpleNamespace(data={"check_runs": [{"conclusion": "success" if self.green else "failure"}]})
        if path == "/repos/%s" % TARGET:
            return SimpleNamespace(data={"default_branch": "main"})
        if path.startswith("/repos/%s/contents/" % TARGET):
            if method == "GET":
                value = self.branch if "?ref=" in path and self.branch is not None else self.base
                return SimpleNamespace(data={"content": base64.b64encode(value.encode()).decode(), "sha": hashlib.sha1(value.encode()).hexdigest()})
            value = base64.b64decode(body["content"]).decode()
            self.branch = value
            return SimpleNamespace(data={})
        if path == "/repos/%s/git/ref/heads/main" % TARGET:
            return SimpleNamespace(data={"object": {"sha": "d" * 40}})
        if path.startswith("/repos/%s/git/ref/heads/" % TARGET):
            return SimpleNamespace(status=200 if self.branch else 404,
                                   data={"object": {"sha": "d" * 40}} if self.branch else None)
        if path == "/repos/%s/git/refs" % TARGET:
            self.branch = self.base
            return SimpleNamespace(data={})
        if "/compare/" in path:
            return SimpleNamespace(data={"commits": [{"sha": "e" * 40}]})
        if path.endswith("/pulls") and "/commits/" in path:
            return SimpleNamespace(data=[{"title": "Fix dashboard card", "merged_at": "2026-01-01T00:00:00Z"}])
        if path == "/repos/%s/pulls?state=open&per_page=100" % TARGET:
            return SimpleNamespace(data=[self.pull] if self.pull else [])
        if path == "/repos/%s/pulls" % TARGET and method == "POST":
            self.created += 1
            self.pull = {"number": 7, "head": {"ref": body["head"]}, "body": body["body"]}
            return SimpleNamespace(data=self.pull)
        if path == "/repos/%s/pulls/7" % TARGET and method == "PATCH":
            self.pull["body"] = body["body"]
            return SimpleNamespace(data=self.pull)
        raise AssertionError((method, path, body))


class PinTest(unittest.TestCase):
    def test_green_revision_opens_and_then_updates_one_pull_request(self):
        gh = FakeGitHub()
        logs = []
        pin.run(gh, SOURCE, NEW, "unittest", [SPEC], lambda _: b"archive", logs.append)
        self.assertEqual(gh.created, 1)
        self.assertIn('LOOM_REVISION="%s"' % NEW, gh.branch)
        self.assertIn('LOOM_SHA256="%s"' % hashlib.sha256(b"archive").hexdigest(), gh.branch)
        self.assertEqual(hunt_plan.problems(gh.pull["body"]), [])
        self.assertIn("Fix dashboard card", gh.pull["body"])
        pin.run(gh, SOURCE, NEXT, "unittest", [SPEC], lambda _: b"archive2", logs.append)
        self.assertEqual(gh.created, 1)
        self.assertIn('LOOM_REVISION="%s"' % NEXT, gh.branch)
        self.assertTrue(any(call[0] == "PATCH" for call in gh.calls))
        writes = len([call for call in gh.calls if call[0] in ("PUT", "PATCH", "POST")])
        pin.run(gh, SOURCE, NEXT, "unittest", [SPEC], lambda _: b"archive2", logs.append)
        self.assertEqual(len([call for call in gh.calls if call[0] in ("PUT", "PATCH", "POST")]), writes)

    def test_changed_tarball_or_red_ci_opens_nothing_and_logs_reason(self):
        for red in (False, True):
            with self.subTest(red=red):
                gh = FakeGitHub()
                gh.green = not red
                reads = iter((b"first", b"second"))
                logs = []
                pin.run(gh, SOURCE, NEW, "unittest", [SPEC], lambda _: next(reads), logs.append)
                self.assertEqual(gh.created, 0)
                self.assertIsNone(gh.branch)
                reason = "CI" if red else "tarball"
                self.assertTrue(any(reason in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
