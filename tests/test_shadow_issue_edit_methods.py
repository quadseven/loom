"""Tests for issue edit (close/reopen) via POST and PUT in shadow mode.

Shadow mode must hold an issue close or reopen sent as POST or PUT (including
gh api --input), not only PATCH. GitHub's REST API accepts POST on PATCH
endpoints, and gh api sends POST whenever a field or --input is given.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import shadow  # noqa: E402
from bus.github import GitHub, ShadowHeld  # noqa: E402

import sys


REPO = "example-owner/example-repo"


class Recorder:
    """A transport that answers GETs from `items` and records other requests."""

    def __init__(self, items=None, fail_reads=False):
        self.items = items or {}
        self.fail_reads = fail_reads
        self.sent = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1]
        if method == "GET":
            if self.fail_reads:
                return 502, {}, b"{}"
            data = self.items.get(path.split("?", 1)[0])
            return (200, {}, json.dumps(data).encode()) if data is not None else (404, {}, b"{}")
        self.sent.append((method, path, json.loads(body) if body else None))
        return 200, {}, json.dumps({"merged": True}).encode()


def issue(number, labels=(), pr=False):
    found = {"number": number, "state": "open", "body": "",
             "labels": [{"name": n} for n in labels]}
    if pr:
        found["pull_request"] = {"url": "x"}
    return found


class ShadowCase(unittest.TestCase):
    """Shadow on, every repo, with a ledger of the test's own."""

    ENV = {"LOOM_SHADOW": "1"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = os.path.join(tmp.name, "state", "ledger.jsonl")
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_SHADOW")}
        env.update(self.ENV, LOOM_SHADOW_LEDGER=self.ledger)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def entries(self):
        return shadow.read(self.ledger)


class IssueEditMethodsTest(ShadowCase):
    """_issue_edit must be classified under POST and PUT, not only PATCH."""

    def test_gh_guard_holds_gh_api_input_with_closed_state_in_shadow(self):
        """gh api --input with {"state": "closed"} is held in shadow."""
        with tempfile.TemporaryDirectory() as here:
            with open(os.path.join(here, "close.json"), "w") as fh:
                json.dump({"state": "closed"}, fh)
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
                    mock.patch.object(shadow, "_gh_item", return_value=issue(5, ("loom:ticket",))):
                code = shadow.main(["gh-guard", here, "api", "-X", "POST",
                                    "repos/%s/issues/5" % REPO, "--input", "close.json"])
                self.assertEqual(code, 1)
                self.assertIn("would close", err.getvalue())
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], shadow.CLOSE)
        self.assertEqual(entries[0]["number"], 5)

    def test_gh_guard_holds_gh_api_post_with_closed_state_in_shadow(self):
        """gh api POST with -f state=closed is held in shadow."""
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
                mock.patch.object(shadow, "_gh_item", return_value=issue(5, ("loom:ticket",))):
            code = shadow.main(["gh-guard", os.getcwd(), "api",
                                "repos/%s/issues/5" % REPO, "-f", "state=closed"])
            self.assertEqual(code, 1)
            self.assertIn("would close", err.getvalue())
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], shadow.CLOSE)

    def test_gh_guard_holds_gh_api_put_with_closed_state_in_shadow(self):
        """gh api PUT with -f state=closed is held in shadow."""
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
                mock.patch.object(shadow, "_gh_item", return_value=issue(5, ("loom:ticket",))):
            code = shadow.main(["gh-guard", os.getcwd(), "api", "-X", "PUT",
                                "repos/%s/issues/5" % REPO, "-f", "state=closed"])
            self.assertEqual(code, 1)
            self.assertIn("would close", err.getvalue())
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], shadow.CLOSE)

    def test_github_rest_post_issue_edit_held_in_shadow(self):
        """POST to /repos/OWNER/REPO/issues/N with state=closed is held in shadow."""
        fake = Recorder({"/repos/%s/issues/5" % REPO: issue(5, ("loom:ticket",))})
        gh = GitHub("t", transport=fake)
        with self.assertRaises(ShadowHeld) as caught:
            gh.rest("POST", "/repos/%s/issues/5" % REPO, {"state": "closed"})
        self.assertEqual(fake.sent, [])
        self.assertEqual(caught.exception.entry["action"], shadow.CLOSE)

    def test_github_rest_put_issue_edit_held_in_shadow(self):
        """PUT to /repos/OWNER/REPO/issues/N with state=closed is held in shadow."""
        fake = Recorder({"/repos/%s/issues/5" % REPO: issue(5, ("loom:ticket",))})
        gh = GitHub("t", transport=fake)
        with self.assertRaises(ShadowHeld) as caught:
            gh.rest("PUT", "/repos/%s/issues/5" % REPO, {"state": "closed"})
        self.assertEqual(fake.sent, [])
        self.assertEqual(caught.exception.entry["action"], shadow.CLOSE)

    def test_github_rest_post_issue_edit_sent_outside_shadow(self):
        """POST to issue path with state=closed is sent when repo is not in shadow."""
        with mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}):
            fake = Recorder({"/repos/%s/issues/5" % REPO: issue(5, ("loom:ticket",))})
            gh = GitHub("t", transport=fake)
            gh.rest("POST", "/repos/%s/issues/5" % REPO, {"state": "closed"})
            self.assertEqual([m for m, _, _ in fake.sent], ["POST"])
        self.assertEqual(self.entries(), [])

    def test_github_rest_put_issue_edit_sent_outside_shadow(self):
        """PUT to issue path with state=closed is sent when repo is not in shadow."""
        with mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}):
            fake = Recorder({"/repos/%s/issues/5" % REPO: issue(5, ("loom:ticket",))})
            gh = GitHub("t", transport=fake)
            gh.rest("PUT", "/repos/%s/issues/5" % REPO, {"state": "closed"})
            self.assertEqual([m for m, _, _ in fake.sent], ["PUT"])
        self.assertEqual(self.entries(), [])

    def test_github_rest_post_issue_reopen_held_in_shadow(self):
        """POST to issue path with state=open is held in shadow."""
        fake = Recorder({"/repos/%s/issues/5" % REPO: issue(5, ("loom:ticket",))})
        gh = GitHub("t", transport=fake)
        with self.assertRaises(ShadowHeld) as caught:
            gh.rest("POST", "/repos/%s/issues/5" % REPO, {"state": "open"})
        self.assertEqual(fake.sent, [])
        self.assertEqual(caught.exception.entry["action"], shadow.REOPEN)

    def test_github_rest_put_issue_reopen_held_in_shadow(self):
        """PUT to issue path with state=open is held in shadow."""
        fake = Recorder({"/repos/%s/issues/5" % REPO: issue(5, ("loom:ticket",))})
        gh = GitHub("t", transport=fake)
        with self.assertRaises(ShadowHeld) as caught:
            gh.rest("PUT", "/repos/%s/issues/5" % REPO, {"state": "open"})
        self.assertEqual(fake.sent, [])
        self.assertEqual(caught.exception.entry["action"], shadow.REOPEN)

    def test_gh_guard_lets_through_post_to_other_endpoint_in_shadow(self):
        """POST to a non-issue path (e.g., comments) is not held as issue edit."""
        with mock.patch("sys.stderr", new_callable=io.StringIO), \
                mock.patch.object(shadow, "_gh_item", return_value=issue(5, ("loom:ticket",))):
            code = shadow.main(["gh-guard", os.getcwd(), "api",
                                "repos/%s/issues/5/comments" % REPO, "-f", "body=hello"])
            self.assertEqual(code, 0)
        self.assertEqual(self.entries(), [])


if __name__ == "__main__":
    unittest.main()