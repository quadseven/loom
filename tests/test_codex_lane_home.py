"""A codex lane run reads a lane-only Codex home, not the operator's.

A codex lane run inherited the operator's user-level Codex instructions:
the user-level AGENTS.md in the operator's Codex home told the run to read
a personal startup file and do a chat duty at task start, so the run
stopped with no commit and the ticket was parked as the ticket's own work
(the failure this module covers, loom#3227). The lane command and
environment that lanes/agent_cli.sh builds are the seam: a lane run points
CODEX_HOME at a lane-only home that holds the operator's model and
provider config and its sign-in, and no user-level AGENTS.md, while a
brain run keeps the operator's home as before.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_CLI = ROOT / "lanes" / "agent_cli.sh"

PERSONAL_AGENTS = "personal startup duty: read the startup file first\n"
CONFIG = 'model = "gpt-test"\nmodel_provider = "test-provider"\n'
OPERATOR_AUTH = "operator-auth-for-test\n"
CLUSTER_AUTH = "cluster-auth-for-test\n"

FAKE_CODEX = """#!/usr/bin/env bash
{
  printf 'args=%s\\n' "$*"
  printf 'codex_home=%s\\n' "${CODEX_HOME:-unset}"
  printf 'home=%s\\n' "${HOME:-unset}"
  if [ -n "${CODEX_HOME:-}" ] && [ -f "$CODEX_HOME/AGENTS.md" ]; then
    printf 'agents=%s\\n' "$(cat "$CODEX_HOME/AGENTS.md")"
  elif [ -f "$HOME/.codex/AGENTS.md" ] && [ -z "${CODEX_HOME:-}" ]; then
    printf 'agents=%s\\n' "$(cat "$HOME/.codex/AGENTS.md")"
  else
    printf 'agents=ABSENT\\n'
  fi
  if [ -n "${CODEX_HOME:-}" ] && [ -f "$CODEX_HOME/config.toml" ]; then
    printf 'config=%s\\n' "$(cat "$CODEX_HOME/config.toml" | tr '\\n' ' ')"
  else
    printf 'config=ABSENT\\n'
  fi
  if [ -n "${CODEX_HOME:-}" ] && [ -f "$CODEX_HOME/auth.json" ]; then
    printf 'auth=%s\\n' "$(cat "$CODEX_HOME/auth.json")"
  else
    printf 'auth=ABSENT\\n'
  fi
} > "$FAKE_CODEX_SEEN"
cat > /dev/null
"""


class CodexLaneHomeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.operator_home = root / "operator-home"
        self.codex_src = self.operator_home / ".codex"
        self.codex_src.mkdir(parents=True)
        (self.codex_src / "AGENTS.md").write_text(PERSONAL_AGENTS)
        (self.codex_src / "config.toml").write_text(CONFIG)
        (self.codex_src / "auth.json").write_text(OPERATOR_AUTH)
        self.agent_home = root / "agent-home"
        self.lane_codex_home = self.agent_home / ".codex"
        self.bin = root / "bin"
        self.bin.mkdir()
        codex = self.bin / "codex"
        codex.write_text(FAKE_CODEX)
        codex.chmod(0o755)
        self.seen = root / "seen.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def run_agent_cli(self, mode, **extra_env):
        script = (
            '. "$1"; MODEL="gpt-test"; AGENT_TIMEOUT=30; '
            'agent_cli codex %s "do the brief"' % mode
        )
        env = {
            "PATH": "%s:/usr/bin:/bin" % self.bin,
            "HOME": str(self.operator_home),
            "AGENT_HOME": str(self.agent_home),
            "FAKE_CODEX_SEEN": str(self.seen),
        }
        env.update(extra_env)
        result = subprocess.run(
            ["/bin/bash", "-c", script, "run", str(AGENT_CLI)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(
            line.split("=", 1) for line in self.seen.read_text().splitlines()
            if "=" in line
        )

    def test_a_codex_lane_run_points_codex_at_a_lane_only_home(self):
        seen = self.run_agent_cli("lane")
        self.assertEqual(seen["codex_home"], str(self.lane_codex_home))
        self.assertNotEqual(seen["codex_home"], str(self.codex_src))
        self.assertIn("exec", seen["args"])
        self.assertIn("--sandbox workspace-write", seen["args"])
        self.assertIn("--model gpt-test", seen["args"])

    def test_the_lane_home_holds_no_user_level_agents_file(self):
        seen = self.run_agent_cli("lane")
        self.assertEqual(seen["agents"], "ABSENT")
        self.assertFalse((self.lane_codex_home / "AGENTS.md").exists())
        # The operator's own file is untouched: only the lane run is moved.
        self.assertEqual((self.codex_src / "AGENTS.md").read_text(), PERSONAL_AGENTS)

    def test_the_lane_home_still_loads_model_provider_config_and_sign_in(self):
        seen = self.run_agent_cli("lane")
        self.assertIn('model_provider = "test-provider"', seen["config"])
        self.assertIn('model = "gpt-test"', seen["config"])
        self.assertEqual(seen["auth"], OPERATOR_AUTH.strip())
        self.assertEqual((self.lane_codex_home / "config.toml").read_text(), CONFIG)

    def test_a_cluster_sign_in_file_wins_over_the_operator_sign_in(self):
        cluster_auth = Path(self._tmp.name) / "codex-auth.json"
        cluster_auth.write_text(CLUSTER_AUTH)
        seen = self.run_agent_cli("lane", LOOM_CODEX_AUTH_FILE=str(cluster_auth))
        self.assertEqual(seen["auth"], CLUSTER_AUTH.strip())

    def test_a_stale_agents_file_in_the_lane_home_is_removed_before_the_run(self):
        self.lane_codex_home.mkdir(parents=True)
        (self.lane_codex_home / "AGENTS.md").write_text("planted by an earlier run\n")
        seen = self.run_agent_cli("lane")
        self.assertEqual(seen["agents"], "ABSENT")
        self.assertFalse((self.lane_codex_home / "AGENTS.md").exists())

    def test_a_codex_brain_run_keeps_the_operator_home(self):
        seen = self.run_agent_cli("brain")
        self.assertNotEqual(seen.get("codex_home"), str(self.lane_codex_home))
        self.assertIn("--sandbox danger-full-access", seen["args"])
        self.assertFalse(self.lane_codex_home.exists())


if __name__ == "__main__":
    unittest.main()
