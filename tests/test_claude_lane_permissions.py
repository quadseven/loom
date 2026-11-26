"""A claude lane run on a cluster pod can edit the tree.

A pod has no operator settings file that allows edits, and `claude -p`
cannot answer a permission prompt, so the CLI denied its Edit and Write
calls, exited 0 and the run ended "agent made no commit". The fake CLI here
edits the tree only when it was started with a permission mode that allows
edits, as the real one does in print mode.
"""

import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bus import usage
from dashboard import usage as board

ROOT = Path(__file__).resolve().parents[1]
AGENT_CLI = ROOT / "lanes" / "agent_cli.sh"

FAKE_CLAUDE = """#!/usr/bin/env bash
case "$*" in
  *"--permission-mode acceptEdits"*) echo changed > "$FAKE_TREE/edited.txt" ;;
  *) echo "Claude requested permissions to write edited.txt, but you haven't granted it yet." ;;
esac
case "$*" in
  *"--output-format stream-json"*)
    printf '%s\n' \
      '{"type":"system","debug":"discard-this-metadata"}' \
      '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":{"five_hour":{"utilization":0.21,"resetsAt":2000003600},"seven_day":{"utilization":0.34,"resetsAt":2000043200}}},"debug":"discard-this-metadata"}' \
      '{"type":"result","result":"finished"}'
    ;;
esac
exit "${FAKE_EXIT:-0}"
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

    def run_lane(self, auth_file=True, status=0):
        script = '. "$1"; MODEL=claude-test; AGENT_TIMEOUT=30; agent_cli claude lane "do the brief"'
        env = {"PATH": "%s:%s:/usr/bin:/bin" % (self.root / "bin", Path(sys.executable).parent),
               "HOME": str(self.root), "FAKE_TREE": str(self.tree), "FAKE_EXIT": str(status),
               "LOOM_ROOT": str(ROOT), "LOOM_HOME": str(self.root), "SLOT": "claude"}
        if auth_file:
            env["LOOM_CLAUDE_AUTH_FILE"] = str(self.token)
        result = subprocess.run(["/bin/bash", "-c", script, "run", str(AGENT_CLI)],
                                cwd=self.tree, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, status, result.stderr)
        return result.stdout

    def test_a_pod_run_with_the_cluster_token_can_edit_the_tree(self):
        self.run_lane()
        self.assertTrue((self.tree / "edited.txt").exists())

    def test_a_pod_run_leaves_no_permission_refusal(self):
        self.assertNotIn("haven't granted", self.run_lane())

    def test_a_regular_run_feeds_short_and_weekly_windows_to_the_usage_board(self):
        (self.tree / "bus").mkdir()
        (self.tree / "bus" / "__init__.py").touch()
        stdout = self.run_lane(auth_file=False, status=7)
        self.assertIn("finished", stdout)
        now = time.time()
        rows = usage.collect(str(self.root), env={}, now=now, lane_names=["claude"], host="test-host")
        self.assertEqual([(row["window"], row["used_pct"]) for row in rows], [("5-hour", 21), ("weekly", 34)])
        self.assertEqual(rows[1]["resets_at"], "2033-05-18T15:33:20+00:00")
        rendered = board.render(board.build(rows, datetime.fromtimestamp(2000000000, timezone.utc)))
        self.assertIn("5-hour", rendered)
        self.assertIn("Weekly", rendered)
        self.assertIn("(in 1h 0m)", rendered)
        self.assertIn("(in 12h 0m)", rendered)
        meter = self.root / "state" / "usage" / "claude.meter.json"
        self.assertNotIn("discard-this-metadata", stdout + meter.read_text())
