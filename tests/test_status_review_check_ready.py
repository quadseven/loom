"""Status reports the review gate that merge applies to automatic tiers."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import brain, merge_tier  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class Fake:
    def __init__(self, *replies):
        self.replies = list(replies)

    def __call__(self, method, url, headers, body):
        return self.replies.pop(0)


class StatusReviewCheckReadyTests(unittest.TestCase):
    def refusals(self, conclusion, luna=False):
        gh = GitHub("token", transport=Fake(
            reply(200, {"mergeable": True, "mergeable_state": "clean"})))
        pr = {"number": 8, "head": {"sha": "head1"}}
        with mock.patch.object(brain, "_risk_tier", return_value=(merge_tier.APP_CODE, "")), \
                mock.patch.object(brain, "_repo_cap_refusal", return_value=""), \
                mock.patch.object(brain, "_pr_graphql", return_value={"headRefOid": "head1"}), \
                mock.patch.object(brain, "_review_check_passed", return_value=conclusion == "success"), \
                mock.patch.object(brain, "_luna_stands_in", return_value=luna):
            return brain._merge_refusals(gh, "example-owner/example-repo", pr, None,
                                         caps={}, env={})

    def test_neutral_elder_blocks_an_app_code_lane_pull_request(self):
        blockers = self.refusals("neutral")
        self.assertTrue(any("review check Grug - Elder not success on head" in item
                            for item in blockers), blockers)

    def test_successful_elder_does_not_add_a_review_check_blocker(self):
        blockers = self.refusals("success")
        self.assertFalse(any("review check" in item for item in blockers), blockers)

    def test_clean_luna_verdict_does_not_add_a_review_check_blocker(self):
        blockers = self.refusals("neutral", luna=True)
        self.assertFalse(any("review check" in item for item in blockers), blockers)


if __name__ == "__main__":
    unittest.main()
