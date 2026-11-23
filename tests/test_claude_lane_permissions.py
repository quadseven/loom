"""A claude lane run on a cluster pod can edit the tree.

A pod has no operator settings file that allows edits, and `claude -p`
cannot answer a permission prompt, so the CLI denied its Edit and Write
calls, exited 0 and the run ended "agent made no commit". The fake CLI here
edits the tree only when it was started with a permission mode that allows
edits, as the real one does in print mode.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_CLI = ROOT / "lanes" / "agent_cli.sh"

FAKE_CLAUDE = """#!/usr/bin/env bash
case "$*" in
  *"--permission-mode acceptEdits"*) echo changed > "$FAKE_TREE/edited.txt" ;;
  *) echo "Claude requested permissions to write edited.txt, but you haven't granted it yet." ;;
esac
"""


class ClaudeLanePermissionsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tree = self.root / "tree"
        self.tree.mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("claude", FAKE_CLAUDE),
                           ("timeout", '#!/bin/sh\nshift\nexec "$@"\n')):
            tool = bin_dir / name
            tool.write_text(body)
            tool.chmod(0o755)
        self.token = self.root / "token"
        self.token.write_text("token-for-test\n")

    def tearDown(self):
        self._tmp.cleanup()

    def run_lane(self, auth_file=True):
        script = '. "$1"; MODEL=claude-test; AGENT_TIMEOUT=30; agent_cli claude lane "do the brief"'
        env = {"PATH": "%s:/usr/bin:/bin" % (self.root / "bin"),
               "HOME": str(self.root), "FAKE_TREE": str(self.tree)}
        if auth_file:
            env["LOOM_CLAUDE_AUTH_FILE"] = str(self.token)
        result = subprocess.run(["/bin/bash", "-c", script, "run", str(AGENT_CLI)],
                                capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_pod_run_with_the_cluster_token_can_edit_the_tree(self):
        self.run_lane()
        self.assertTrue((self.tree / "edited.txt").exists())

    def test_a_pod_run_leaves_no_permission_refusal(self):
        self.assertNotIn("haven't granted", self.run_lane())
