"""Status marks a pull request READY only when merge's review-check rule passes.

An app-code lane pull request whose head has a completed neutral review check
used to read READY in status while merge refused it (the ticket this module
guards). These tests call the same _merge_refusals status uses, so status and
merge agree on the review-check rule.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
CHECK = "Grug - Elder"
BLOCKER = "review check %s not success on head" % CHECK


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class Fake:
    """Replies in order, recording the calls made."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        return self.replies.pop(0)


def ready_node(rollup="SUCCESS"):
    return {"headRefOid": "head1",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": rollup}}}]}}


class StatusReviewCheckReadyTest(unittest.TestCase):
    def make_pr(self):
        return {"number": 8, "title": "Change",
                "head": {"ref": "loom/4-work", "sha": "head1",
                         "repo": {"full_name": REPO}},
                "user": {"login": "example-brain"}}

    def merge_replies(self, check_runs=None):
        # The reads _merge_refusals makes for a READY app-code lane PR: its
        # ticket, its mergeable state, its files, the two cap counts, and the
        # review check on its head (last, only when the check is consulted).
        out = [
            reply(200, {"number": 4, "state": "open",
                        "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}),
            reply(200, {"mergeable": True, "mergeable_state": "clean"}),
            reply(200, [{"filename": "bus/a.py", "patch": "+x"}]),
            reply(200, {"total_count": 0}),
            reply(200, {"total_count": 0}),
        ]
        if check_runs is not None:
            out.append(reply(200, {"check_runs": check_runs}))
        return out

    def refusals(self, check_runs=None, node=None):
        gh = GitHub("t", transport=Fake(*self.merge_replies(check_runs)))
        return brain._merge_refusals(gh, REPO, self.make_pr(), (REPO, 4),
                                     check=CHECK, node=node)

    def test_neutral_review_check_blocks_an_app_code_pull_request(self):
        blockers = self.refusals(
            check_runs=[{"name": CHECK, "status": "completed", "conclusion": "neutral"}],
            node=ready_node())
        self.assertIn(BLOCKER, blockers)

    def test_successful_review_check_does_not_block(self):
        blockers = self.refusals(
            check_runs=[{"name": CHECK, "status": "completed", "conclusion": "success"}],
            node=ready_node())
        self.assertNotIn(BLOCKER, blockers)

    def test_clean_luna_verdict_does_not_block(self):
        # A clean Luna verdict stands in for the review check on this head,
        # just as it does in the merge path, so no review-check read is needed.
        with mock.patch.object(brain, "_luna_stands_in", return_value=True) as luna:
            blockers = self.refusals(node=ready_node(rollup="FAILURE"))
        self.assertTrue(luna.called)
        self.assertNotIn(BLOCKER, blockers)


if __name__ == "__main__":
    unittest.main()
