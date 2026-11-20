"""Status gives a pull request whose head merge refuses the same blocker."""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example"
BLOCKER = "pull request head is not a loom branch"
ENV = {"LOOM_BRIEF_AUTHORS": "example-brain"}


class StubGitHub:
    def rest(self, method, path, *args, **kwargs):
        if "/issues/" in path:
            return SimpleNamespace(data={"state": "open", "labels": [
                {"name": "loom:ticket"}, {"name": "loom:pr"}]})
        return SimpleNamespace(data={"mergeable": True, "mergeable_state": "clean"})


def refusals(head, login, ticket=None):
    pr = {"number": 8, "body": "", "head": {"ref": head, "sha": "abc"},
          "user": {"login": login}}
    with mock.patch.dict(os.environ, ENV), \
            mock.patch.object(brain, "_hold_box_reason", return_value=""), \
            mock.patch.object(brain, "_risk_tier", return_value=("app-code", [])), \
            mock.patch.object(brain, "_repo_cap_refusal", return_value=""):
        return brain._merge_refusals(StubGitHub(), REPO, pr, ticket)


class ForeignHead(unittest.TestCase):
    def test_a_non_loom_head_by_a_stranger_is_blocked_and_loom_heads_are_not(self):
        self.assertIn(BLOCKER, refusals("sol/x-1", "example-stranger"))
        self.assertNotIn(BLOCKER, refusals("loom/1-x", "example-stranger"))
        # A non-loom head by a trusted author with a linked ticket merges.
        pr_body_ref = (REPO, 9)
        pr = {"number": 8, "body": "Refs #9", "head": {"ref": "sol/x-1", "sha": "abc"},
              "user": {"login": "example-brain"}}
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(brain, "_hold_box_reason", return_value=""), \
                mock.patch.object(brain, "_risk_tier", return_value=("app-code", [])), \
                mock.patch.object(brain, "_repo_cap_refusal", return_value=""):
            found = brain._merge_refusals(StubGitHub(), REPO, pr, pr_body_ref)
        self.assertNotIn(BLOCKER, found)


if __name__ == "__main__":
    unittest.main()
