"""Public write protection for the shared GitHub REST boundary."""
import os
from pathlib import Path
import tempfile
import subprocess
import traceback
import unittest
from unittest import mock

from bus.github import GitHub, GitHubError, Response


class PublicWriteGuardTest(unittest.TestCase):
    def client(self, target_private=False, private_repos=()):
        calls = []
        gh = GitHub("test-token")
        gh.app = object()  # Bot writes use the App path.

        def rest(method, path, body=None, etag=None, ok=()):
            calls.append((method, path, body))
            if method == "GET" and path.startswith("/repos/"):
                repo = path.removeprefix("/repos/")
                private = target_private if repo == "quadseven/loom" else repo in private_repos
                return Response(200, {"private": private})
            return Response(201, {"id": 1})

        gh._rest = rest
        return gh, calls

    def test_public_comment_scrubs_private_repo_reference_and_deny_list_host(self):
        private_repo = "example-owner/hidden-repo"
        private_ref = private_repo + "#42"
        denied_host = "denied-host.example.com"
        gh, calls = self.client(private_repos={private_repo})
        body = ("Evidence from " + private_ref + " was checked at "
                + denied_host + "; the result is safe to share.")
        with mock.patch("bus.github.read_ssm", return_value=denied_host):
            gh.rest("POST", "/repos/quadseven/loom/issues/12/comments", {"body": body})

        posted = [c[2]["body"] for c in calls if c[0] == "POST"]
        self.assertEqual(len(posted), 1)
        self.assertNotIn(private_repo, posted[0])
        self.assertIn("issue 42 of a private repo", posted[0])
        self.assertNotIn(denied_host, posted[0])
        self.assertIn("<private identifier>", posted[0])

    def test_private_target_body_is_unchanged_and_does_not_read_deny_list(self):
        private_repo = "example-owner/hidden-repo"
        body = private_repo + "#42 denied-host.example.com"
        gh, calls = self.client(target_private=True, private_repos={private_repo})
        with mock.patch("bus.github.read_ssm") as read:
            gh.rest("POST", "/repos/quadseven/loom/issues/12/comments", {"body": body})

        posted = [c[2]["body"] for c in calls if c[0] == "POST"]
        self.assertEqual(posted, [body])
        read.assert_not_called()

    def test_public_write_fails_closed_when_deny_list_cannot_be_read(self):
        gh, calls = self.client()
        with mock.patch("bus.github.read_ssm", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(GitHubError, "deny-list"):
                gh.rest("POST", "/repos/quadseven/loom/issues/12/comments",
                        {"body": "A harmless public note."})
        self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_missing_aws_names_the_cli_and_configuration_without_writing(self):
        gh, calls = self.client()
        with mock.patch.dict(os.environ, {"PATH": ""}, clear=True):
            with self.assertRaisesRegex(GitHubError, "aws.*LOOM_AWS_CLI"):
                gh.rest("POST", "/repos/quadseven/loom/issues/12/comments",
                        {"body": "A harmless public note."})
        self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_configured_aws_outside_path_and_path_aws_preserve_scrubbing(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "aws"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' 'denied-host.example.com'\n")
            executable.chmod(0o700)
            environments = (
                {"PATH": "", "LOOM_AWS_CLI": str(executable)},
                {"PATH": directory},
            )
            for environment in environments:
                with self.subTest(configured="LOOM_AWS_CLI" in environment):
                    gh, calls = self.client()
                    with mock.patch.dict(os.environ, environment, clear=True):
                        gh.rest("POST", "/repos/quadseven/loom/issues/12/comments",
                                {"body": "Checked denied-host.example.com."})
                    posted = [c[2]["body"] for c in calls if c[0] == "POST"]
                    self.assertEqual(posted, ["Checked <private identifier>."])

    def test_invalid_configured_aws_does_not_fall_back_or_reveal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "aws"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' 'denied-host.example.com'\n")
            executable.chmod(0o700)
            private_path = str(Path(directory) / "private-cli-location")
            gh, calls = self.client()
            with mock.patch.dict(os.environ, {
                    "PATH": directory, "LOOM_AWS_CLI": private_path}, clear=True):
                with self.assertRaisesRegex(GitHubError, "LOOM_AWS_CLI") as caught:
                    gh.rest("POST", "/repos/quadseven/loom/issues/12/comments",
                            {"body": "A harmless public note."})
            self.assertNotIn(private_path, str(caught.exception))
            self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_aws_execution_error_is_actionable_without_private_path(self):
        gh, calls = self.client()
        with mock.patch("bus.github.shutil.which", return_value="/example/aws"), \
                mock.patch("bus.github.subprocess.run",
                           side_effect=PermissionError("private-executable-path")):
            with self.assertRaisesRegex(GitHubError, "cannot execute aws") as caught:
                gh.rest("POST", "/repos/quadseven/loom/issues/12/comments",
                        {"body": "A harmless public note."})
        self.assertNotIn("private-executable-path", str(caught.exception))
        self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_aws_timeout_traceback_does_not_reveal_command_or_output(self):
        gh, calls = self.client()
        error = subprocess.TimeoutExpired(["private-cli-location"], 30,
                                          output="private-output", stderr="private-error")
        with mock.patch("bus.github.shutil.which", return_value="/example/aws"), \
                mock.patch("bus.github.subprocess.run", side_effect=error):
            try:
                gh.rest("POST", "/repos/quadseven/loom/issues/12/comments",
                        {"body": "A harmless public note."})
            except GitHubError:
                trace = traceback.format_exc()
            else:
                self.fail("timeout allowed a public write")
        for private in ("private-cli-location", "private-output", "private-error"):
            self.assertNotIn(private, trace)
        self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_public_write_fails_closed_when_target_visibility_is_unknown(self):
        gh, calls = self.client()
        def unavailable_visibility(method, path, body=None, etag=None, ok=()):
            calls.append((method, path, body))
            if method == "GET" and path.startswith("/repos/"):
                return Response(404, {"message": "Not Found"})
            return Response(201, {"id": 1})
        gh._rest = unavailable_visibility
        with self.assertRaisesRegex(GitHubError, "could not determine target"):
            gh.rest("POST", "/repos/quadseven/loom/issues/12/comments",
                    {"body": "A harmless public note."})
        self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_status_body_edit_uses_the_same_public_guard(self):
        private_repo = "example-owner/hidden-repo"
        gh, calls = self.client(private_repos={private_repo})
        body = "status detail: " + private_repo + "#7"
        with mock.patch("bus.github.read_ssm", return_value="safe-term.invalid"):
            gh.rest("PATCH", "/repos/quadseven/loom/issues/12", {"body": body})

        patch_bodies = [c[2]["body"] for c in calls if c[0] == "PATCH"]
        self.assertEqual(patch_bodies, ["status detail: issue 7 of a private repo"])

    def test_metrics_comment_edit_uses_the_same_public_guard(self):
        private_repo = "example-owner/hidden-repo"
        gh, calls = self.client(private_repos={private_repo})
        body = "metrics cite " + private_repo + "#19"
        with mock.patch("bus.github.read_ssm", return_value="safe-term.example.com"):
            gh.rest("PATCH", "/repos/quadseven/loom/issues/comments/99",
                    {"body": body})
        patch_bodies = [c[2]["body"] for c in calls if c[0] == "PATCH"]
        self.assertEqual(patch_bodies, ["metrics cite issue 19 of a private repo"])

    def test_public_review_reply_uses_same_guard(self):
        private_repo = "example-owner/hidden-repo"
        # A review-thread reply has no REST target path, so the GraphQL guard
        # must resolve the thread's pull request repository before posting.
        gh, calls = self.client(private_repos={private_repo})
        def graphql_transport(method, path, body=None, etag=None, ok=()):
            calls.append((method, path, body))
            if method == "GET" and path.startswith("/repos/"):
                repo = path.removeprefix("/repos/")
                return Response(200, {"private": repo == private_repo})
            if path == "/graphql" and isinstance(body, dict) and body.get("query", "").startswith("query($id:ID!)"):
                return Response(200, {"data": {"node": {"pullRequest": {
                    "repository": {"nameWithOwner": "quadseven/loom"}}}}})
            return Response(200, {"data": {"addPullRequestReviewThreadReply": {
                "comment": {"id": "reply-id"}}}})
        gh._rest = graphql_transport
        variables = {"id": "thread-id", "body": "See " + private_repo + "#42"}
        with mock.patch("bus.github.read_ssm", return_value="private-service.invalid"):
            gh.rest("POST", "/graphql", {
                "query": "mutation($id:ID!,$body:String!) { addPullRequestReviewThreadReply }",
                "variables": variables,
            })

        mutations = [c[2] for c in calls if c[1] == "/graphql" and
                     c[2].get("query", "").startswith("mutation")]
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0]["variables"]["body"],
                         "See issue 42 of a private repo")

    def test_private_review_reply_is_left_unchanged(self):
        gh, calls = self.client()
        private_repo = "example-owner/hidden-repo"
        def graphql_transport(method, path, body=None, etag=None, ok=()):
            calls.append((method, path, body))
            if method == "GET" and path.startswith("/repos/"):
                return Response(200, {"private": path.endswith(private_repo)})
            if isinstance(body, dict) and body.get("query", "").startswith("query($id:ID!)"):
                return Response(200, {"data": {"node": {"pullRequest": {
                    "repository": {"nameWithOwner": private_repo}}}}})
            return Response(200, {"data": {"addPullRequestReviewThreadReply": {
                "comment": {"id": "reply-id"}}}})
        gh._rest = graphql_transport
        body = (private_repo + "#42 "
                + "denied-host.example.com")
        with mock.patch("bus.github.read_ssm") as read:
            gh.graphql("mutation { addPullRequestReviewThreadReply }",
                       {"id": "thread-id", "body": body})
        sent = [c[2]["variables"]["body"] for c in calls
                if c[1] == "/graphql" and
                c[2].get("query", "").startswith("mutation")]
        self.assertEqual(sent, [body])
        read.assert_not_called()

    def test_review_reply_fails_closed_when_destination_is_unknown(self):
        gh, calls = self.client()
        with mock.patch.object(gh, "_rest", return_value=Response(200, {"data": {"node": None}})):
            with self.assertRaisesRegex(GitHubError, "could not identify target"):
                gh.graphql("mutation { addPullRequestReviewThreadReply }",
                           {"id": "thread-id", "body": "A reply."})
        self.assertEqual(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
