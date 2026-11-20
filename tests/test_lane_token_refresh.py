"""A lane run mints a fresh GitHub App token before a late GitHub write once
its token is older than the margin, and mints no extra one while it is fresh."""

import unittest
from pathlib import Path

from tests import test_run_lane as base


class LaneTokenRefreshTest(base.LaneRunnerTest):
    def mints(self, **env):
        counter = Path(self._tmp.name) / "mints"
        self.drop_brief()
        self.run_lane(LOOM_GITHUB_APP_ID="124",
                      LOOM_APP_TOKEN_CMD=self.app_token_cmd(
                          f"echo minted >> '{counter}'; echo ghs_test"), **env)
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()), self.calls())
        return len(counter.read_text().split())

    def test_a_token_older_than_the_margin_is_minted_again_before_the_push(self):
        fresh = self.mints()
        self.setUp()
        stale = self.mints(LOOM_APP_TOKEN_MAX_AGE="0")
        self.assertGreater(stale, fresh)

    def test_a_fresh_token_is_not_minted_again(self):
        self.assertEqual(self.mints(LOOM_APP_TOKEN_MAX_AGE="3600"), 2)


for _name in dir(base.LaneRunnerTest):
    if _name.startswith("test_"):
        setattr(LaneTokenRefreshTest, _name, None)
