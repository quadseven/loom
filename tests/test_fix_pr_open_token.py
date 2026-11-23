"""A fix round mints a fresh App token before it checks its pull request is
still open, and a check GitHub did not answer parks the run as transient
(loom#3465): a round over an hour long checked with its first token, got no
answer, and was parked as work with its committed edits lost."""

import re
import unittest
from pathlib import Path

from tests import test_run_lane as base


class FixPrOpenTokenTest(base.LaneRunnerTest):
    def round(self, **env):
        """A fix round that commits. Its token command and its fake gh both
        log to the call log, so the order of mints and pull request views
        shows. A pull request view made after the agent ran gets no answer
        when GH_NO_ANSWER_LATE is set."""
        ran = Path(self._tmp.name) / "agent-ran"
        real = self.bin / "gh"
        self.write_tool(self.bin / "gh-real", real.read_text())
        self.write_tool(real, f"""#!/usr/bin/env bash
if [ "$1 $2" = "pr view" ] && [ -e '{ran}' ] && [ -n "${{GH_NO_ANSWER_LATE:-}}" ]; then
  printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
  exit 1
fi
exec '{self.bin}/gh-real' "$@"
""")
        agent = self.bin / "codex"
        self.write_tool(self.bin / "codex-real", agent.read_text())
        self.write_tool(agent, f"#!/usr/bin/env bash\ntouch '{ran}'\nexec '{self.bin}/codex-real' \"$@\"\n")
        name, before = self.fix_brief()
        result = self.run_lane(
            LOOM_GITHUB_APP_ID="124",
            LOOM_APP_TOKEN_CMD=self.app_token_cmd("echo 'mint' >> \"$FAKE_LOG\"; echo ghs_test"),
            **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return name, before

    def mints_before_the_open_check(self, **env):
        self.round(**env)
        calls = self.calls()
        views = [i for i, c in enumerate(calls) if c.startswith("gh pr view 7") and "headRefName" in c]
        self.assertTrue(views, calls)
        return calls[:views[-1]].count("mint")

    def test_a_stale_token_is_minted_again_before_the_open_check(self):
        fresh = self.mints_before_the_open_check(LOOM_APP_TOKEN_MAX_AGE="3600")
        self.setUp()
        stale = self.mints_before_the_open_check(LOOM_APP_TOKEN_MAX_AGE="0")
        self.assertGreater(stale, fresh)

    def test_an_open_check_with_no_answer_parks_transient_not_work(self):
        name, _ = self.round(GH_NO_ANSWER_LATE="1")
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        log = (self.loom / "logs" / "codex" / (name + ".log")).read_text()
        self.assertRegex(log, r"failed: \[transient\] could not check pull request #7")
        self.assertNotIn("[work] could not check pull request", log)


for _name in dir(base.LaneRunnerTest):
    if _name.startswith("test_"):
        setattr(FixPrOpenTokenTest, _name, None)


if __name__ == "__main__":
    unittest.main()
