"""A lane mints a fresh App token before a late GitHub write (loom#3571).

A run mints its installation token when it starts, and the token lasts an
hour. A setup or base check longer than that left the factory incident
filed with the expired token: `HTTP 401: Bad credentials`, the ticket held
as `wait:incident` with no incident behind it. Filing an incident, closing
one, and posting an answer now mint again first once the token is older
than LOOM_APP_TOKEN_MAX_AGE, and a mint that fails is the reason given.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests import sandbox_caps
from tests import test_run_lane as base

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import station  # noqa: E402

RUNNER = ROOT / "lanes" / "run_lane.sh"
FACTORY = "quadseven/loom"


def functions(*names: str) -> str:
    text = RUNNER.read_text()
    found = []
    for name in names:
        match = re.search(r"^%s\(\) \{.*?^\}\n" % re.escape(name), text, re.S | re.M)
        if match is None:
            raise AssertionError("lanes/run_lane.sh has no %s()" % name)
        found.append(match.group(0))
    stale = re.search(r"^APP_TOKEN_STALE_WHY=.*$", text, re.M)
    return "\n".join(found) + ("\n" + stale.group(0) + "\n" if stale else "")


# The mint counts its tokens: the run started with ghs_old, the first mint
# after it is ghs_mint1. GitHub takes only $VALID_TOKEN and answers any
# other with the 401 the lanes saw. Each GitHub write logs its token.
STUBS = r'''
github_auth_mint() {
  unset GH_TOKEN
  [ -z "${MINT_FAIL:-}" ] || return 1
  local n
  n=$(( $(cat "$MINTS" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$MINTS"
  export GH_TOKEN="ghs_mint$n"
}
repo_is_public() { return 0; }
incident_subject() { printf 'example/repo'; }
incident_ticket() { printf 'T042'; }
incident_text_clean() { return 0; }
open_issue() { :; }
upsert_sticky_comment() { :; }
sign_tail() { :; }
signed() { printf '%s' "$1"; }
python3() { return 0; }
gh() {
  printf '%s %s %s\n' "$1" "$2" "${GH_TOKEN:-none}" >> "$USED"
  if [ "${GH_TOKEN:-}" != "$VALID_TOKEN" ]; then
    echo "HTTP 401: Bad credentials (https://api.github.com/graphql)" >&2
    return 1
  fi
  [ "$1 $2" != "issue create" ] || printf 'https://github.com/%s/issues/77\n' "$FACTORY_REPO"
  return 0
}
'''


class LateWriteToken(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "lane.log"
        self.log.touch()
        self.mints = self.tmp / "mints"
        self.used = self.tmp / "used"

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, call: str, *, age: int, valid: str, mint_fail: bool = False) -> list[str]:
        """Run `call` with a token minted `age` seconds ago as ghs_old."""
        script = (functions("lane_app_token", "fresh_app_token", "incident_file",
                            "incident_filing_failed", "file_factory_incident",
                            "note_factory_incident", "resolve_factory_incidents")
                  + STUBS
                  + 'export GH_TOKEN=ghs_old\n'
                  + 'APP_TOKEN_AT=$(( $(date +%%s) - %d ))\n' % age
                  + call + '\nprintf "%s\\n" "$GH_TOKEN"\n')
        env = {"PATH": "/usr/bin:/bin", "LOG": str(self.log), "TMPDIR": str(self.tmp),
               "INCIDENT_DIR": str(self.tmp / "incidents"), "REPO": "example/repo",
               "FACTORY_REPO": FACTORY, "TICKET": "T042",
               "INCIDENT_MARKER": "<!-- loom:factory-incident v1 -->",
               "INCIDENT_WITHHELD": "withheld", "INCIDENT_REPEAT_MARKER": "<!-- r -->",
               "MINTS": str(self.mints), "USED": str(self.used), "VALID_TOKEN": valid}
        if mint_fail:
            env["MINT_FAIL"] = "1"
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def note_incident(self, **kw) -> tuple[str, str]:
        out = self.run_script('SETUP_REASON="interpreter x is not on PATH"\n'
                              'note_factory_incident environment "interpreter: x" SETUP_REASON\n'
                              'printf "%s\\n" "$SETUP_REASON"', **kw)
        return out[0], out[1]

    def writes(self) -> list[str]:
        return self.used.read_text().splitlines() if self.used.exists() else []

    def test_an_incident_after_an_hour_long_setup_is_filed_with_a_new_token(self):
        reason, token = self.note_incident(age=3700, valid="ghs_mint1")
        self.assertIn("factory incident %s#%d" % (FACTORY, 77), reason)
        self.assertEqual(self.writes(), ["issue create ghs_mint1"])
        # The new token stays for the rest of the run, not only the filing.
        self.assertEqual(token, "ghs_mint1")
        self.assertIsNotNone(station.HELD_FOR_RE.search(reason))

    def test_a_young_token_files_without_another_mint(self):
        reason, _ = self.note_incident(age=60, valid="ghs_old")
        self.assertIn("factory incident %s#%d" % (FACTORY, 77), reason)
        self.assertFalse(self.mints.exists())
        self.assertEqual(self.writes(), ["issue create ghs_old"])

    def test_a_failed_mint_is_the_reason_not_the_401(self):
        reason, _ = self.note_incident(age=3700, valid="ghs_mint1", mint_fail=True)
        self.assertIn("factory incident filing failed (token mint: could not mint a fresh "
                      "GitHub App token", reason)
        self.assertNotIn("401", reason)
        self.assertNotIn("Bad credentials", reason)
        # The station still reads it as no incident and releases the hold.
        self.assertIsNone(station.HELD_FOR_RE.search(reason))

    def test_a_failed_mint_still_files_while_the_old_token_works(self):
        reason, _ = self.note_incident(age=3000, valid="ghs_old", mint_fail=True)
        self.assertIn("factory incident %s#%d" % (FACTORY, 77), reason)

    def test_closing_an_incident_after_a_long_base_check_uses_a_new_token(self):
        incidents = self.tmp / "incidents"
        incidents.mkdir()
        (incidents / "example_repo-environment").write_text("12\n")
        self.run_script("resolve_factory_incidents environment", age=3700, valid="ghs_mint1")
        self.assertEqual(self.writes(), ["issue close ghs_mint1"])
        self.assertFalse((incidents / "example_repo-environment").exists())


class LateWriteTokenRun(base.LaneRunnerTest):
    """A whole lane run whose setup outlasts its token files the incident
    with a token minted after setup, not the one minted when it started."""

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_setup_incident_is_filed_with_a_token_minted_after_setup(self):
        counter = Path(self._tmp.name) / "mint-count"
        tokens = Path(self._tmp.name) / "gh-tokens"
        # The contract names an interpreter the lane host lacks: an
        # environment incident during setup, before any agent runs.
        self.contract_repo()
        self.drop_brief()
        result = self.run_lane(
            LOOM_GITHUB_APP_ID="124",
            LOOM_APP_TOKEN_CMD=self.app_token_cmd(
                f"n=$(( $(cat '{counter}' 2>/dev/null || echo 0) + 1 )); echo $n > '{counter}'\n"
                'echo "mint ghs_t$n" >> "$FAKE_LOG"; echo "ghs_t$n"\n'),
            LOOM_APP_TOKEN_MAX_AGE="0", FAKE_GH_TOKENS=str(tokens))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("interpreter loom-test-python is not on PATH", self.lane_log())
        gh_calls = [c for c in calls if c.startswith("gh ")]
        used = dict(zip(range(len(gh_calls)), tokens.read_text().split()))
        creates = [i for i, c in enumerate(gh_calls) if c.startswith("gh issue create")]
        self.assertTrue(creates, calls)
        create_at = calls.index(gh_calls[creates[0]])
        mints = [c.split()[1] for c in calls[:create_at] if c.startswith("mint ")]
        self.assertGreater(len(mints), 1, calls)
        self.assertEqual(used[creates[0]], mints[-1])
        self.assertNotEqual(used[creates[0]], "ghs_t1")


for _name in dir(base.LaneRunnerTest):
    if _name.startswith("test_"):
        setattr(LateWriteTokenRun, _name, None)


if __name__ == "__main__":
    unittest.main()
