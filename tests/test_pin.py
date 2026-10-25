"""A station pin pass against a stateful fake GitHub API."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from bus import hunt_plan, pin
from bus.github import GitHubError

SOURCE = "example-owner/loom"
TARGET = "example-owner/deploy"
OLD = "a" * 40
NEW = "b" * 40
NEXT = "c" * 40
PATH = "dashboard/pin.env"
BRANCH = "loom/pin-" + hashlib.sha256(PATH.encode()).hexdigest()[:12]
SPEC = {"repo": TARGET, "path": PATH, "revision_var": "LOOM_REVISION",
        "sha256_var": "LOOM_SHA256"}


def refused(method, path, *errors):
    """GitHub's 422 for a write it will not take, raised as the real client
    raises it: the status line as the message and GitHub's JSON answer, with
    its message and errors, as the body."""
    answer = {"message": "Validation Failed",
              "errors": [{"resource": "PullRequest", "code": "custom", "message": text}
                         for text in errors],
              "documentation_url": "https://docs.github.com/rest/pulls/pulls",
              "status": "422"}
    return GitHubError("%s %s -> 422" % (method, path), 422, json.dumps(answer))


class FakeGitHub:
    def __init__(self):
        self.base = 'LOOM_REVISION="%s"\nLOOM_SHA256="%s"\n' % (OLD, "0" * 64)
        self.base_sha = "d" * 40
        self.branch = None
        self.branch_sha = None
        self.pull = None
        self.created = 0
        self.calls = []
        self.green = True
        self.files = [{"filename": "dashboard/server.py"}]
        self.labels = []
        self.list_hides_pull = False

    def rest(self, method, path, body=None, ok=()):
        self.calls.append((method, path, body))
        if "/check-runs?" in path:
            return SimpleNamespace(data={"check_runs": [{"conclusion": "success" if self.green else "failure"}]})
        if path == "/repos/%s" % TARGET:
            return SimpleNamespace(data={"default_branch": "main"})
        if path.startswith("/repos/%s/contents/" % TARGET):
            return self.contents(method, path, body)
        if (path.startswith("/repos/%s/git/ref/heads/" % TARGET)
                or path.startswith("/repos/%s/git/refs/heads/" % TARGET)
                or path == "/repos/%s/git/refs" % TARGET):
            return self.refs(method, path, body)
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

    def refs(self, method, path, body=None):
        if path == "/repos/%s/git/ref/heads/main" % TARGET:
            return SimpleNamespace(data={"object": {"sha": self.base_sha}})
        if path.startswith("/repos/%s/git/refs/heads/" % TARGET) and method == "PATCH":
            # Like GitHub: a ref is updated at git/refs/<ref>.
            if self.branch_sha is None:
                return SimpleNamespace(status=404, data=None)
            self.branch_sha = body.get("sha", self.branch_sha)
            return SimpleNamespace(data={"object": {"sha": self.branch_sha}})
        if path.startswith("/repos/%s/git/ref/heads/" % TARGET):
            # Like GitHub: a ref is read at git/ref/<ref>, and a PATCH there
            # answers 404 (loom#1405).
            if method == "PATCH":
                raise AssertionError("PATCH to the git/ref read path answers 404: %s" % path)
            if self.branch_sha is None:
                return SimpleNamespace(status=404, data=None)
            return SimpleNamespace(status=200, data={"object": {"sha": self.branch_sha}})
        if path == "/repos/%s/git/refs" % TARGET and method == "POST":
            self.branch = self.base
            self.branch_sha = self.base_sha
            return SimpleNamespace(data={})
        raise AssertionError((method, path, body))

    def pulls(self, method, path, body):
        if path == "/repos/%s/pulls?state=open&per_page=100" % TARGET:
            return SimpleNamespace(data=[self.pull] if self.pull and not self.list_hides_pull else [])
        if path == "/repos/%s/pulls" % TARGET and method == "POST":
            return self.create(method, path, body)
        if path == "/repos/%s/pulls/7" % TARGET and method == "PATCH":
            if hunt_plan.problems(body.get("body", "")):
                raise refused(method, path, "body fails the Hunt Plan")
            self.pull["body"] = body["body"]
            return SimpleNamespace(data=self.pull)
        raise AssertionError((method, path, body))

    def create(self, method, path, body):
        if body.get("base") != "main" or not body.get("head") or self.branch is None:
            raise refused(method, path, "base, head and a pushed head branch are required")
        if hunt_plan.problems(body.get("body", "")):
            raise refused(method, path, "body fails the Hunt Plan")
        if self.pull and self.pull["head"]["ref"] == body["head"]:
            raise refused(method, path, "A pull request already exists for %s." % body["head"])
        self.created += 1
        self.pull = {"number": 7, "head": {"ref": body["head"]}, "body": body["body"]}
        return SimpleNamespace(data=self.pull)


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

    def test_a_refused_create_raises_githubs_422_and_labels_nothing(self):
        gh = FakeGitHub()
        pin.run(gh, SOURCE, NEW, "unittest", [SPEC], lambda _: b"archive", lambda _: None)
        # The open pull request sits past the first page of 100, so the pass
        # does not see it and asks GitHub to create a second one.
        gh.list_hides_pull = True
        logs = []
        with self.assertRaises(GitHubError) as caught:
            pin.run(gh, SOURCE, NEXT, "unittest", [SPEC], lambda _: b"archive2", logs.append)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(str(caught.exception), "POST /repos/%s/pulls -> 422" % TARGET)
        answer = json.loads(caught.exception.body)
        self.assertEqual(answer["message"], "Validation Failed")
        self.assertIn("A pull request already exists", answer["errors"][0]["message"])
        self.assertEqual(gh.created, 1)
        self.assertEqual(gh.labels, [pin.PIN_LABEL])
        self.assertFalse(any("opened pull request" in line for line in logs), logs)

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


class PinRebuildTest(unittest.TestCase):
    """The pin branch is rebuilt from the default branch's head on every
    update so its pull request never goes stale when the default branch moves."""

    def pin_text(self, revision):
        return 'LOOM_REVISION="%s"\nLOOM_SHA256="%s"\n' % (revision, "0" * 64)

    def ref_path(self):
        return "/repos/%s/git/refs/heads/%s" % (TARGET, quote(BRANCH, safe=""))

    def assert_rebuilt(self, gh, revision):
        patches = [c for c in gh.calls if c[0] == "PATCH" and c[1] == self.ref_path()]
        self.assertEqual(len(patches), 1, gh.calls)
        self.assertEqual(patches[0][2], {"sha": "d" * 40, "force": True})
        puts = [c for c in gh.calls if c[0] == "PUT" and "/contents/" in c[1]
                and c[2].get("branch") == BRANCH]
        self.assertEqual(len(puts), 1, gh.calls)
        self.assertLess(gh.calls.index(patches[0]), gh.calls.index(puts[0]))
        content = base64.b64decode(puts[0][2]["content"]).decode()
        self.assertIn('LOOM_REVISION="%s"' % revision, content)

    def test_rebuilds_branch_when_its_base_is_behind_default(self):
        gh = FakeGitHub()
        gh.base = self.pin_text(NEW)          # default branch moved on
        gh.branch = self.pin_text(OLD)        # pin branch still names the old revision
        gh.base_sha = "d" * 40
        gh.branch_sha = "e" * 40
        gh.pull = {"number": 7, "head": {"ref": BRANCH},
                   "mergeable_state": "dirty", "body": ""}
        pin.pin_one(gh, SOURCE, NEXT, SPEC, lambda: "digest", lambda _: None)
        self.assert_rebuilt(gh, NEXT)

    def test_rebuilds_when_pull_request_is_dirty(self):
        gh = FakeGitHub()
        gh.base = self.pin_text(NEXT)
        gh.branch = self.pin_text(NEXT)
        gh.base_sha = "d" * 40
        gh.branch_sha = "d" * 40
        gh.pull = {"number": 7, "head": {"ref": BRANCH},
                   "mergeable_state": "dirty", "body": ""}
        pin.pin_one(gh, SOURCE, NEXT, SPEC, lambda: "digest", lambda _: None)
        self.assert_rebuilt(gh, NEXT)

    def test_returns_when_pull_request_is_clean(self):
        gh = FakeGitHub()
        gh.base = self.pin_text(NEXT)
        gh.branch = self.pin_text(NEXT)
        gh.base_sha = "d" * 40
        gh.branch_sha = "d" * 40
        gh.pull = {"number": 7, "head": {"ref": BRANCH},
                   "mergeable_state": "clean", "body": ""}
        before = len([c for c in gh.calls if c[0] in ("PUT", "PATCH", "POST")])
        pin.pin_one(gh, SOURCE, NEXT, SPEC, lambda: "digest", lambda _: None)
        after = len([c for c in gh.calls if c[0] in ("PUT", "PATCH", "POST")])
        self.assertEqual(after, before)
        self.assertFalse(any(c[0] == "PATCH" and "/git/ref" in c[1] for c in gh.calls))
        self.assertFalse(any(c[0] == "PUT" and "/contents/" in c[1] for c in gh.calls))


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


class PinMainTest(unittest.TestCase):
    """The pin pass entry point: main() must fail cleanly, never crash."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = self.tmp / "state" / "station"
        self.state_dir.mkdir(parents=True)
        self.deployed = self.state_dir / "deployed.json"
        self._saved = {}
        for name in ("LOOM_PIN_TARGETS", "LOOM_SELF_REPO", "LOOM_HOME"):
            self._saved[name] = os.environ.get(name)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _env(self, revision="a" * 40):
        os.environ["LOOM_PIN_TARGETS"] = json.dumps([SPEC])
        os.environ["LOOM_SELF_REPO"] = SOURCE
        os.environ["LOOM_HOME"] = str(self.tmp)
        self.deployed.write_text(json.dumps({"revision": revision}))

    def test_main_returns_one_when_git_rev_parse_fails(self):
        """git rev-parse HEAD failing returns 1, never an uncaught traceback."""
        self._env()
        with patch("bus.pin.subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, ["git", "rev-parse", "HEAD"])):
            result = pin.main()
        self.assertEqual(result, 1)

    def test_main_runs_git_from_repo_root(self):
        """git rev-parse HEAD runs from the repo root, not the caller's cwd."""
        self._env()
        expected_cwd = os.path.dirname(os.path.dirname(os.path.abspath(pin.__file__)))
        with patch("bus.pin.subprocess.check_output", return_value="a" * 40) as mock:
            with patch("bus.pin._pin_client"):
                with patch("bus.pin.run"):
                    pin.main()
        self.assertEqual(mock.call_args.kwargs.get("cwd"), expected_cwd)


if __name__ == "__main__":
    unittest.main()
