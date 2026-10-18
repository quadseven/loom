"""A station pin pass against a stateful fake GitHub API."""
import base64
import hashlib
import json
import os
import unittest
from types import SimpleNamespace

from bus import hunt_plan, pin
from bus.github import GitHubError

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
        self.files = [{"filename": "dashboard/server.py"}]
        self.labels = []

    def rest(self, method, path, body=None, ok=()):
        self.calls.append((method, path, body))
        if "/check-runs?" in path:
            return SimpleNamespace(data={"check_runs": [{"conclusion": "success" if self.green else "failure"}]})
        if path == "/repos/%s" % TARGET:
            return SimpleNamespace(data={"default_branch": "main"})
        if path.startswith("/repos/%s/contents/" % TARGET):
            return self.contents(method, path, body)
        if path.startswith("/repos/%s/git/ref/heads/" % TARGET) or path == "/repos/%s/git/refs" % TARGET:
            return self.refs(path)
        if "/compare/" in path:
            return SimpleNamespace(data={"commits": [{"sha": "e" * 40}], "files": self.files})
        if path == "/repos/%s/issues/7/labels" % TARGET and method == "POST":
            self.labels += body["labels"]
            return SimpleNamespace(data=[])
        if path.endswith("/pulls") and "/commits/" in path:
            return SimpleNamespace(data=[{"title": "Fix dashboard card", "merged_at": "2026-01-01T00:00:00Z"}])
        if path.startswith("/repos/%s/pulls" % TARGET):
            return self.pulls(method, path, body)
        raise AssertionError((method, path, body))

    def contents(self, method, path, body):
        if method == "GET":
            value = self.branch if "?ref=" in path and self.branch is not None else self.base
            return SimpleNamespace(data={"content": base64.b64encode(value.encode()).decode(),
                                         "sha": hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()})
        self.branch = base64.b64decode(body["content"]).decode()
        return SimpleNamespace(data={})

    def refs(self, path):
        if path == "/repos/%s/git/ref/heads/main" % TARGET:
            return SimpleNamespace(data={"object": {"sha": "d" * 40}})
        if path.startswith("/repos/%s/git/ref/heads/" % TARGET):
            return SimpleNamespace(status=200 if self.branch else 404,
                                   data={"object": {"sha": "d" * 40}} if self.branch else None)
        if path == "/repos/%s/git/refs" % TARGET:
            self.branch = self.base
            return SimpleNamespace(data={})

    def pulls(self, method, path, body):
        if path == "/repos/%s/pulls?state=open&per_page=100" % TARGET:
            return SimpleNamespace(data=[self.pull] if self.pull else [])
        if path == "/repos/%s/pulls" % TARGET and method == "POST":
            if body.get("base") != "main" or not body.get("head") or self.branch is None:
                raise GitHubError("Validation Failed", 422)
            if hunt_plan.problems(body.get("body", "")):
                raise GitHubError("Validation Failed", 422)
            self.created += 1
            self.pull = {"number": 7, "head": {"ref": body["head"]}, "body": body["body"]}
            return SimpleNamespace(data=self.pull)
        if path == "/repos/%s/pulls/7" % TARGET and method == "PATCH":
            if hunt_plan.problems(body.get("body", "")):
                raise GitHubError("Validation Failed", 422)
            self.pull["body"] = body["body"]
            return SimpleNamespace(data=self.pull)
        raise AssertionError((method, path, body))


class PinTest(unittest.TestCase):
    def test_download_rejects_urls_outside_github_tarballs(self):
        with self.assertRaisesRegex(ValueError, "tarball URL"):
            pin.download("data:text/plain,archive")

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
                pin.run(gh, SOURCE, NEW, "unittest", [SPEC], lambda _, reads=reads: next(reads), logs.append)
                self.assertEqual(gh.created, 0)
                self.assertIsNone(gh.branch)
                reason = "CI" if red else "tarball"
                self.assertTrue(any(reason in line for line in logs), logs)

    def test_a_dashboard_merge_opens_one_labelled_pin_pull_request_naming_commit_and_sha(self):
        gh = FakeGitHub()
        logs = []
        pin.run(gh, SOURCE, NEW, "unittest", [SPEC], lambda _: b"archive", logs.append)
        self.assertEqual(gh.created, 1)
        self.assertIn(NEW, gh.pull["body"])
        self.assertIn(hashlib.sha256(b"archive").hexdigest(), gh.pull["body"])
        self.assertEqual(gh.labels, [pin.PIN_LABEL])
        self.assertEqual(len([c for c in gh.calls if "/compare/" in c[1]]), 1)

    def test_a_merge_touching_nothing_under_dashboard_opens_no_pin_pull_request(self):
        gh = FakeGitHub()
        gh.files = [{"filename": "bus/station.py"}, {"filename": "docs/dashboard.md"},
                    {"filename": "tests/test_dashboard.py"}]
        downloads = []
        logs = []
        pin.run(gh, SOURCE, NEW, "unittest", [SPEC],
                lambda url: downloads.append(url) or b"archive", logs.append)
        self.assertEqual(gh.created, 0)
        self.assertIsNone(gh.branch)
        self.assertEqual(downloads, [])
        self.assertTrue(any("nothing under dashboard/ changed" in line for line in logs), logs)

    def test_a_moved_dashboard_file_or_a_full_file_list_counts_as_a_change(self):
        self.assertTrue(pin.touches({"files": [{"filename": "web/app.py",
                                                "previous_filename": "dashboard/app.py"}]},
                                    "dashboard/"))
        self.assertTrue(pin.touches({"files": [{"filename": "bus/x.py"}] * 300}, "dashboard/"))
        self.assertTrue(pin.touches({}, "dashboard/"))
        self.assertFalse(pin.touches({"files": [{"filename": "dashboards.md"}]}, "dashboard/"))

    def test_a_target_may_watch_its_own_path(self):
        spec = dict(SPEC, watch="lanes/viewer/")
        self.assertEqual(pin.targets(json.dumps([spec]))[0]["watch"], "lanes/viewer/")
        with self.assertRaisesRegex(ValueError, "watch"):
            pin.targets(json.dumps([dict(SPEC, watch="../x")]))
        gh = FakeGitHub()
        pin.run(gh, SOURCE, NEW, "unittest", [spec], lambda _: b"archive", lambda _: None)
        self.assertEqual(gh.created, 0)


class PinAuthTest(unittest.TestCase):
    def test_pin_pass_uses_personal_token_not_app_token(self):
        from bus.github import GitHub, auth_from_env

        names = ("LOOM_GITHUB_APP_ID", "LOOM_GITHUB_APP_INSTALLATION_ID",
                 "LOOM_GITHUB_APP_KEY_SSM", "LOOM_GITHUB_TOKEN")
        saved = {name: os.environ.get(name) for name in names}
        try:
            os.environ["LOOM_GITHUB_APP_ID"] = "1"
            os.environ["LOOM_GITHUB_APP_INSTALLATION_ID"] = "1"
            os.environ["LOOM_GITHUB_APP_KEY_SSM"] = "/example/ssm"
            os.environ["LOOM_GITHUB_TOKEN"] = "personal-token"
            # With the App configured, auth_from_env would send the App
            # installation token, which the deployment repo's collaborator-only
            # trust gate rejects (auth == "app").
            self.assertEqual(GitHub(auth_from_env()).auth, "app")
            # The pin pass must open the pull request with the operator's
            # personal token instead (auth == "token"), so the trust gate
            # passes and the pin pull request can merge.
            self.assertEqual(pin._pin_client().auth, "token")
        finally:
            for name in names:
                if saved[name] is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = saved[name]


if __name__ == "__main__":
    unittest.main()
