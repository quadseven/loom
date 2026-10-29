"""bus.lane push_branch against a fake GitHub (loom#2160).

The seam is push_branch itself, with two fakes at its public boundaries: the
GitHub client (an object with .rest) and the working copy (a real temp git
repo the test chdirs into, since the lane's git calls run in the cwd).

The fake's ref reads reproduce the real API's two shapes. The singular
/git/ref/heads/{branch} answers the one exact ref object, or 404. The plural
/git/refs/heads/{branch} prefix-matches: it answers a list whenever earlier
attempt branches (the -r3/-r4 shape) share the branch name as a prefix, the
single object only when the exact ref is the sole match, and 404 when
nothing matches. Blobs, trees and commits are created with git plumbing in
the test repo, so the SHAs the fake hands back are the real git SHAs and the
lane's own SHA checks run for real.

What this catches: a push that reads the wrong ref (a prefix sibling treated
as the target), crashes on the list answer, writes to a sibling, loses its
idempotency, or turns an API failure or a malformed payload into a success.
What it misses: the HTTP layer of bus.github.GitHub itself, which its own
tests cover.
"""

import base64
import os
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import lane, tickets  # noqa: E402
from bus.github import GitHubError  # noqa: E402

REPO = "example/repo"  # leak-guard-allow: fixture repo
NUMBER = 42
TITLE = "Fix the thing"
BRANCH = tickets.branch_for(NUMBER, TITLE)
EXACT = "refs/heads/" + BRANCH
SIBLING_R3 = EXACT + "-r3-unvalidated"
SIBLING_R4 = EXACT + "-r4-timedout"
WHEN = "2026-10-29T03:00:00+00:00"

_UNSET = object()


class _Response:
    def __init__(self, data):
        self.status = 200
        self.data = data
        self.etag = None
        self.headers = {}


class FakeGitHub:
    """A git-database stand-in over the test's own repository."""

    def __init__(self, repo_dir, refs):
        self.repo_dir = repo_dir
        self.refs = dict(refs)
        self.calls = []
        self.ref_error = None
        self.ref_payload = _UNSET

    def _git(self, *args, data=None, env=None):
        full = dict(os.environ)
        if env:
            full.update(env)
        result = subprocess.run(["git", *args], cwd=self.repo_dir, input=data,
                                capture_output=True, text=True, env=full)
        if result.returncode:
            raise AssertionError("git %s failed: %s" % (args[0], result.stderr))
        return result.stdout.strip()

    def writes(self):
        return [call for call in self.calls if call[0] in ("POST", "PATCH")]

    def rest(self, method, path, body=None):
        self.calls.append((method, path, body))
        rest = path[len("/repos/" + REPO):]
        if method == "GET":
            return self._get(rest)
        if method == "POST":
            return self._post(rest, body)
        if method == "PATCH" and rest.startswith("/git/refs/heads/"):
            return self._update_ref(unquote(rest[len("/git/refs/heads/"):]), body)
        raise AssertionError("unexpected call: %s %s" % (method, path))

    def _get(self, rest):
        if rest == "/issues/%d" % NUMBER:
            return _Response({"number": NUMBER, "title": TITLE})
        if rest == "":
            return _Response({"default_branch": "main"})
        if rest.startswith("/git/refs/heads/"):
            return self._read_refs(unquote(rest[len("/git/refs/heads/"):]))
        if rest.startswith("/git/ref/heads/"):
            return self._read_ref(unquote(rest[len("/git/ref/heads/"):]))
        raise AssertionError("unexpected call: GET /repos/%s%s" % (REPO, rest))

    def _post(self, rest, body):
        if rest == "/git/blobs":
            sha = self._git("hash-object", "-w", "--stdin",
                            data=base64.b64decode(body["content"]).decode())
            return _Response({"sha": sha})
        if rest == "/git/trees":
            return _Response({"sha": self._make_tree(body)})
        if rest == "/git/commits":
            return _Response({"sha": self._make_commit(body)})
        if rest == "/git/refs":
            return self._create_ref(body)
        raise AssertionError("unexpected call: POST /repos/%s%s" % (REPO, rest))

    def _create_ref(self, body):
        if body["ref"] in self.refs:
            raise GitHubError("reference already exists", 422)
        self.refs[body["ref"]] = body["sha"]
        return _Response({"ref": body["ref"],
                          "object": {"sha": body["sha"], "type": "commit"}})

    def _ref_object(self, name):
        return {"ref": name, "object": {"sha": self.refs[name], "type": "commit"}}

    def _read_ref(self, branch):
        if self.ref_error is not None:
            raise self.ref_error
        if self.ref_payload is not _UNSET:
            return _Response(self.ref_payload)
        name = "refs/heads/" + branch
        if name not in self.refs:
            raise GitHubError("not found", 404)
        return _Response(self._ref_object(name))

    def _read_refs(self, branch):
        if self.ref_error is not None:
            raise self.ref_error
        if self.ref_payload is not _UNSET:
            return _Response(self.ref_payload)
        prefix = "refs/heads/" + branch
        matches = sorted(name for name in self.refs
                         if name == prefix or name.startswith(prefix))
        if not matches:
            raise GitHubError("not found", 404)
        if matches == [prefix]:
            return _Response(self._ref_object(prefix))
        return _Response([self._ref_object(name) for name in matches])

    def _make_tree(self, body):
        index = os.path.join(self.repo_dir, ".git", "test-index")
        env = {"GIT_INDEX_FILE": index}
        try:
            self._git("read-tree", body["base_tree"], env=env)
            for update in body["tree"]:
                if update["sha"] is None:
                    self._git("update-index", "--force-remove", "--",
                              update["path"], env=env)
                else:
                    self._git("update-index", "--add", "--cacheinfo",
                              "%s,%s,%s" % (update["mode"], update["sha"],
                                            update["path"]), env=env)
            return self._git("write-tree", env=env)
        finally:
            if os.path.exists(index):
                os.unlink(index)

    def _make_commit(self, body):
        author, committer = body["author"], body["committer"]
        env = {"GIT_AUTHOR_NAME": author["name"],
               "GIT_AUTHOR_EMAIL": author["email"],
               "GIT_AUTHOR_DATE": author["date"],
               "GIT_COMMITTER_NAME": committer["name"],
               "GIT_COMMITTER_EMAIL": committer["email"],
               "GIT_COMMITTER_DATE": committer["date"]}
        args = ["commit-tree", body["tree"]]
        for parent in body["parents"]:
            args += ["-p", parent]
        args += ["-m", body["message"]]
        return self._git(*args, env=env)

    def _update_ref(self, branch, body):
        name = "refs/heads/" + branch
        if name not in self.refs:
            raise GitHubError("not found", 404)
        if not body.get("force"):
            check = subprocess.run(
                ["git", "merge-base", "--is-ancestor", self.refs[name],
                 body["sha"]], cwd=self.repo_dir, capture_output=True)
            if check.returncode:
                raise GitHubError("update is not a fast-forward", 422)
        self.refs[name] = body["sha"]
        return _Response(self._ref_object(name))


class PushBranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        env = {"GIT_AUTHOR_DATE": WHEN, "GIT_COMMITTER_DATE": WHEN}
        self._git("init", "-q", "-b", "main")
        self._git("-c", "user.name=Lane Test",
                  "-c", "user.email=lane-test@example.com",
                  "commit", "-q", "--allow-empty", "-m", "Base", env=env)
        # A real file at the base too, so the pushed diff has a deletion-free
        # add and a modify available; one add is all the fixtures need.
        self._write("base.txt", "base\n")
        self._git("add", "base.txt")
        self._git("-c", "user.name=Lane Test",
                  "-c", "user.email=lane-test@example.com",
                  "commit", "-q", "-m", "Base files", env=env)
        self.base = self._git("rev-parse", "HEAD")
        self._git("update-ref", "refs/remotes/origin/main", self.base)
        self._git("checkout", "-q", "-b", BRANCH)
        self._write("work.txt", "work\n")
        self._git("add", "work.txt")
        self._git("-c", "user.name=Lane Test",
                  "-c", "user.email=lane-test@example.com",
                  "commit", "-q", "-m", "Work on the thing", env=env)
        self.head = self._git("rev-parse", "HEAD")
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self.cwd)

    def _git(self, *args, env=None):
        full = dict(os.environ)
        if env:
            full.update(env)
        result = subprocess.run(["git", *args], cwd=self.dir,
                                capture_output=True, text=True, env=full)
        if result.returncode:
            raise AssertionError("git %s failed: %s" % (args[0], result.stderr))
        return result.stdout.strip()

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as handle:
            handle.write(text)

    def push(self, refs):
        gh = FakeGitHub(self.dir, refs)
        return gh, lane.push_branch(gh, REPO, NUMBER)

    def assert_only_the_singular_ref_was_read(self, gh):
        reads = [path for method, path, _ in gh.calls
                 if method == "GET" and "/git/ref" in path]
        self.assertEqual(reads, ["/repos/%s/git/ref/heads/%s"
                                 % (REPO, quote(BRANCH, safe=""))])

    def test_a_prefix_sibling_does_not_crash_an_up_to_date_push(self):
        gh, sha = self.push({EXACT: self.head, SIBLING_R3: self.base,
                             SIBLING_R4: self.base})
        self.assertEqual(sha, self.head)
        self.assertEqual(gh.writes(), [])
        reads = [path for method, path, _ in gh.calls
                 if method == "GET" and "/git/ref" in path]
        self.assertTrue(any("/git/ref/heads/" in path for path in reads), reads)

    def test_prefix_siblings_without_a_target_create_only_the_target(self):
        gh, sha = self.push({SIBLING_R3: self.base, SIBLING_R4: self.base})
        self.assertEqual(sha, self.head)
        self.assertEqual(gh.refs[EXACT], self.head)
        self.assertEqual(gh.refs[SIBLING_R3], self.base)
        self.assertEqual(gh.refs[SIBLING_R4], self.base)
        creates = [call for call in gh.writes()
                   if call[0] == "POST" and call[1].endswith("/git/refs")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][2]["ref"], EXACT)
        self.assert_only_the_singular_ref_was_read(gh)

    def test_prefix_siblings_with_a_behind_target_update_only_the_target(self):
        gh, sha = self.push({EXACT: self.base, SIBLING_R3: self.base,
                             SIBLING_R4: self.base})
        self.assertEqual(sha, self.head)
        self.assertEqual(gh.refs[EXACT], self.head)
        self.assertEqual(gh.refs[SIBLING_R3], self.base)
        self.assertEqual(gh.refs[SIBLING_R4], self.base)
        updates = [call for call in gh.writes() if call[0] == "PATCH"]
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0][1].endswith("/git/refs/heads/" +
                                               quote(BRANCH, safe="")),
                        updates[0][1])
        creates = [call for call in gh.writes()
                   if call[0] == "POST" and call[1].endswith("/git/refs")]
        self.assertEqual(creates, [])
        self.assert_only_the_singular_ref_was_read(gh)

    def test_a_missing_target_with_no_siblings_is_created(self):
        gh, sha = self.push({})
        self.assertEqual(sha, self.head)
        self.assertEqual(gh.refs[EXACT], self.head)

    def test_an_up_to_date_target_is_a_no_op(self):
        gh, sha = self.push({EXACT: self.head})
        self.assertEqual(sha, self.head)
        self.assertEqual(gh.writes(), [])

    def test_a_ref_read_failure_stops_the_push(self):
        gh = FakeGitHub(self.dir, {EXACT: self.base})
        gh.ref_error = GitHubError("server error", 500)
        with self.assertRaises(GitHubError):
            lane.push_branch(gh, REPO, NUMBER)
        self.assertEqual(gh.writes(), [])
        self.assertEqual(gh.refs[EXACT], self.base)

    def test_a_ref_payload_that_is_not_a_ref_object_stops_the_push(self):
        payloads = [
            [{"ref": EXACT, "object": {"sha": self.base}}],  # a list, not a ref
            None,                                            # an empty body
            {},                                              # no object at all
            {"object": {}},                                  # an object, no commit
            {"object": "not-a-ref-object"},                  # a non-object object
            {"ref": SIBLING_R3,                              # a sibling's ref
             "object": {"sha": self.base, "type": "commit"}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                gh = FakeGitHub(self.dir, {EXACT: self.base})
                gh.ref_payload = payload
                with self.assertRaises(GitHubError):
                    lane.push_branch(gh, REPO, NUMBER)
                self.assertEqual(gh.writes(), [])
                self.assertEqual(gh.refs[EXACT], self.base)


if __name__ == "__main__":
    unittest.main()
