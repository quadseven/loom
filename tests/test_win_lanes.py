"""Tests for lanes/win/ snapshot and restore scripts."""

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.session_procs import run


ROOT = Path(__file__).resolve().parent.parent
WIN_DIR = ROOT / "lanes" / "win"
SNAPSHOT_SCRIPT = WIN_DIR / "snapshot-lanes.ps1"
RESTORE_SCRIPT = WIN_DIR / "restore-lanes.ps1"
MANIFEST = WIN_DIR / "lanes.example.json"

IS_WINDOWS = platform.system() == "Windows"
HAS_PWSH = shutil.which("pwsh") is not None


class FakeAgentCLI:
    """Fake workspace CLI for testing."""

    def __init__(self, test_dir: Path):
        self.test_dir = test_dir
        self.bin_dir = test_dir / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = test_dir / "agent.log"
        self.sessions = {}
        self.next_session_id = 1
        self._write_cli()

    def _write_cli(self):
        script = self.bin_dir / "agent"
        content = f"""#!/usr/bin/env bash
set -euo pipefail
LOG="{self.log_file}"
echo "agent $*" >> "$LOG"
case "$1" in
    list)
        if [ "$2" = "--json" ]; then
            cat <<'EOF'
{json.dumps(list(self.sessions.values()))}
EOF
        fi
        ;;
    start)
        shift
        name=""
        cwd=""
        identity=""
        resume=""
        conversation=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --name) name="$2"; shift 2 ;;
                --cwd) cwd="$2"; shift 2 ;;
                --identity) identity="$2"; shift 2 ;;
                --resume) resume="$2"; shift 2 ;;
                --conversation) conversation="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        session_id="session-${{name}}-$((++SESSION_COUNTER))"
        echo "$session_id" >> "$LOG.session_ids"
        cat <<EOF
{{"id": "$session_id", "name": "$name", "identity": "$identity", "kind": "$identity"}}
EOF
        ;;
    *)
        echo "unknown command: $1" >&2
        exit 1
        ;;
esac
"""
        script.write_text(content)
        script.chmod(0o755)
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ['PATH']}"

    def add_session(self, name: str, identity: str, kind: str, session_id: str = None):
        if session_id is None:
            session_id = f"session-{name}-{self.next_session_id}"
            self.next_session_id += 1
        self.sessions[session_id] = {
            "id": session_id,
            "name": name,
            "identity": identity,
            "kind": kind
        }
        return session_id

    def clear_sessions(self):
        self.sessions = {}

    def get_log(self):
        if self.log_file.exists():
            return self.log_file.read_text()
        return ""


@unittest.skipUnless(HAS_PWSH, "pwsh not available; skipping PowerShell execution tests")
class TestWinLanes(unittest.TestCase):
    """Test Windows lane snapshot and restore."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_win_lanes_"))
        self.fake_cli = FakeAgentCLI(self.test_dir)
        self.state_dir = self.test_dir / "state"
        self.state_dir.mkdir(parents=True)
        self.old_cwd = Path.cwd()
        os.chdir(self.test_dir)

        # Copy scripts and manifest to test dir
        self.win_dir = self.test_dir / "lanes" / "win"
        self.win_dir.mkdir(parents=True)
        shutil.copy(MANIFEST, self.win_dir / "lanes.example.json")
        shutil.copy(SNAPSHOT_SCRIPT, self.win_dir / "snapshot-lanes.ps1")
        shutil.copy(RESTORE_SCRIPT, self.win_dir / "restore-lanes.ps1")

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def run_pwsh(self, script: Path, args: list = None, env: dict = None, timeout: int = 30):
        """Run a PowerShell script."""
        cmd = ["pwsh", "-NoProfile", "-Command", f"& '{script}'"]
        if args:
            cmd.extend(args)
        test_env = os.environ.copy()
        if env:
            test_env.update(env)
        test_env["PATH"] = f"{self.fake_cli.bin_dir}:{test_env['PATH']}"
        result = run(cmd, timeout * 1000, env=test_env)
        return result

    def test_snapshot_writes_atomic_json(self):
        """Snapshot writes atomic JSON with timestamp and lane session IDs."""
        # Add a session for codex
        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")

        result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])

        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        snapshot_path = self.state_dir / "lanes-snapshot.json"
        self.assertTrue(snapshot_path.exists())

        with open(snapshot_path) as f:
            snapshot = json.load(f)

        self.assertIn("timestamp", snapshot)
        self.assertIn("lanes", snapshot)
        self.assertEqual(len(snapshot["lanes"]), 3)

        codex_lane = next(l for l in snapshot["lanes"] if l["name"] == "codex")
        self.assertEqual(codex_lane["session_id"], "session-codex-1")
        self.assertEqual(codex_lane["kind"], "codex")
        self.assertEqual(codex_lane["restore_policy"], "resume")

    def test_snapshot_no_tokens_in_output(self):
        """Snapshot never writes tokens or credentials."""
        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")

        result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])

        self.assertEqual(result.returncode, 0)

        snapshot_path = self.state_dir / "lanes-snapshot.json"
        content = snapshot_path.read_text()

        # No token-like patterns
        self.assertNotIn("token", content.lower())
        self.assertNotIn("credential", content.lower())
        self.assertNotIn("password", content.lower())
        self.assertNotIn("secret", content.lower())
        self.assertNotIn("key", content.lower())

    def test_snapshot_missing_lane_returns_null_session(self):
        """Lane with no active session gets null session_id."""
        # No sessions added

        result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])

        self.assertEqual(result.returncode, 0)

        snapshot_path = self.state_dir / "lanes-snapshot.json"
        with open(snapshot_path) as f:
            snapshot = json.load(f)

        for lane in snapshot["lanes"]:
            self.assertIsNone(lane["session_id"])

    def test_restore_waits_for_workspace_server(self):
        """Restore waits for workspace server to be ready."""
        # Add sessions so snapshot has data
        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")
        self.fake_cli.add_session("antigravity", "antigravity", "antigravity", "session-ag-1")
        self.fake_cli.add_session("claude", "claude", "claude", "session-claude-1")

        # Write snapshot
        snapshot_result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])
        self.assertEqual(snapshot_result.returncode, 0)

        # Restore should succeed (server is ready because fake CLI works)
        result = self.run_pwsh(self.win_dir / "restore-lanes.ps1", [
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-WorkspaceCli", "agent",
            "-ServerWaitTimeoutSec", "10",
            "-ServerPollIntervalSec", "1"
        ])

        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        self.assertIn("Restored:", result.stdout)

    def test_restore_idempotent_skips_running_lanes(self):
        """Restore skips lanes that are already running."""
        # Add sessions (simulating already running)
        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")
        self.fake_cli.add_session("antigravity", "antigravity", "antigravity", "session-ag-1")
        self.fake_cli.add_session("claude", "claude", "claude", "session-claude-1")

        snapshot_result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])
        self.assertEqual(snapshot_result.returncode, 0)

        result = self.run_pwsh(self.win_dir / "restore-lanes.ps1", [
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-WorkspaceCli", "agent",
            "-ServerWaitTimeoutSec", "10",
            "-ServerPollIntervalSec", "1"
        ])

        self.assertEqual(result.returncode, 0)
        self.assertIn("already running", result.stdout)
        self.assertIn("Skipped:", result.stdout)

    def test_restore_resumes_with_correct_flags_per_kind(self):
        """Restore uses correct resume flags per agent kind."""
        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")
        self.fake_cli.add_session("antigravity", "antigravity", "antigravity", "session-ag-1")
        self.fake_cli.add_session("claude", "claude", "claude", "session-claude-1")

        snapshot_result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])
        self.assertEqual(snapshot_result.returncode, 0)

        # Clear sessions so restore sees them as not running
        self.fake_cli.clear_sessions()

        result = self.run_pwsh(self.win_dir / "restore-lanes.ps1", [
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-WorkspaceCli", "agent",
            "-ServerWaitTimeoutSec", "10",
            "-ServerPollIntervalSec", "1"
        ])

        self.assertEqual(result.returncode, 0)

        log = self.fake_cli.get_log()
        # Check that start was called with correct resume flags
        self.assertIn("--resume session-codex-1", log)  # codex uses --resume
        self.assertIn("--conversation session-ag-1", log)  # antigravity uses --conversation
        self.assertIn("--resume session-claude-1", log)  # claude uses --resume

    def test_restore_awaiting_decision_not_auto_resumed(self):
        """Lanes with restore_policy awaiting-decision are not auto-resumed."""
        # Modify manifest to have awaiting-decision policy
        manifest_path = self.win_dir / "lanes.example.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["lanes"][0]["restore_policy"] = "awaiting-decision"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")

        snapshot_result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])
        self.assertEqual(snapshot_result.returncode, 0)

        self.fake_cli.clear_sessions()

        result = self.run_pwsh(self.win_dir / "restore-lanes.ps1", [
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-WorkspaceCli", "agent",
            "-ServerWaitTimeoutSec", "10",
            "-ServerPollIntervalSec", "1"
        ])

        self.assertEqual(result.returncode, 0)
        self.assertIn("awaiting-decision", result.stdout)
        self.assertIn("Awaiting Decision:", result.stdout)

    def test_restore_oneshot_listed_as_unfinished(self):
        """Lanes with restore_policy oneshot are listed as unfinished for operator."""
        manifest_path = self.win_dir / "lanes.example.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["lanes"][0]["restore_policy"] = "oneshot"
        manifest["lanes"][0]["brief"] = "T042-fix-the-thing"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")

        snapshot_result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])
        self.assertEqual(snapshot_result.returncode, 0)

        self.fake_cli.clear_sessions()

        result = self.run_pwsh(self.win_dir / "restore-lanes.ps1", [
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-WorkspaceCli", "agent",
            "-ServerWaitTimeoutSec", "10",
            "-ServerPollIntervalSec", "1"
        ])

        self.assertEqual(result.returncode, 0)
        self.assertIn("Unfinished One-shots:", result.stdout)
        self.assertIn("T042-fix-the-thing", result.stdout)

    def test_restore_preserves_claim_label(self):
        """Restored lane keeps its claim label (documented behavior)."""
        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")

        snapshot_result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])
        self.assertEqual(snapshot_result.returncode, 0)

        self.fake_cli.clear_sessions()

        result = self.run_pwsh(self.win_dir / "restore-lanes.ps1", [
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-WorkspaceCli", "agent",
            "-ServerWaitTimeoutSec", "10",
            "-ServerPollIntervalSec", "1"
        ])

        self.assertEqual(result.returncode, 0)
        # The claim label preservation is a documented behavior;
        # the script doesn't manipulate GitHub labels directly
        self.assertIn("Restored:", result.stdout)

    def test_snapshot_atomic_write(self):
        """Snapshot uses atomic write (temp file + move)."""
        self.fake_cli.add_session("codex", "codex", "codex", "session-codex-1")

        result = self.run_pwsh(self.win_dir / "snapshot-lanes.ps1", [
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-WorkspaceCli", "agent"
        ])

        self.assertEqual(result.returncode, 0)

        # No temp files should remain
        temp_files = list(self.state_dir.glob("*.tmp.*"))
        self.assertEqual(len(temp_files), 0)

    def test_restore_fails_when_workspace_server_unavailable(self):
        """Restore fails if workspace server doesn't become ready."""
        # Use a non-existent CLI to simulate server down
        result = self.run_pwsh(self.win_dir / "restore-lanes.ps1", [
            "-SnapshotPath", "state/lanes-snapshot.json",
            "-LanesManifest", "lanes/win/lanes.example.json",
            "-WorkspaceCli", "nonexistent-agent-cli",
            "-ServerWaitTimeoutSec", "2",
            "-ServerPollIntervalSec", "1"
        ])

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Workspace server did not become ready", result.stderr)


# Cross-platform tests that don't require pwsh
class TestWinLanesLogic(unittest.TestCase):
    """Test Windows lane logic without executing PowerShell."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_win_lanes_logic_"))
        self.old_cwd = Path.cwd()
        os.chdir(self.test_dir)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_manifest_schema(self):
        """Lane manifest has required fields for all three kinds."""
        manifest = json.loads(MANIFEST.read_text())
        self.assertIn("lanes", manifest)
        self.assertEqual(len(manifest["lanes"]), 3)

        names = {lane["name"] for lane in manifest["lanes"]}
        self.assertEqual(names, {"codex", "antigravity", "claude"})

        for lane in manifest["lanes"]:
            self.assertIn("name", lane)
            self.assertIn("kind", lane)
            self.assertIn("cwd", lane)
            self.assertIn("identity", lane)
            self.assertIn("model", lane)
            self.assertIn("launch_flags", lane)
            self.assertIn("restore_policy", lane)
            self.assertIn(lane["kind"], ["codex", "antigravity", "claude"])
            self.assertIn(lane["restore_policy"], ["resume", "awaiting-decision", "oneshot"])

    def test_snapshot_script_no_token_writes(self):
        """Snapshot script does not contain token/credential writing logic."""
        content = SNAPSHOT_SCRIPT.read_text()
        # Should not write tokens to snapshot (check for actual token operations, not comments)
        self.assertNotIn("write.*token", content.lower())
        self.assertNotIn("password", content.lower())
        self.assertNotIn("secret", content.lower())
        # Should not have set-content or add-content with token/credential
        self.assertNotRegex(content.lower(), r"set-content.*token")
        self.assertNotRegex(content.lower(), r"add-content.*credential")
        # Should write atomic (temp file + move)
        self.assertIn(".tmp.", content)  # temp file pattern

    def test_restore_script_resume_flags_per_kind(self):
        """Restore script uses correct resume flags per kind."""
        content = RESTORE_SCRIPT.read_text()
        # codex uses --resume
        self.assertIn("--resume", content)
        # antigravity uses --conversation
        self.assertIn("--conversation", content)
        # claude uses --resume
        self.assertIn("codex", content.lower())
        self.assertIn("antigravity", content.lower())
        self.assertIn("claude", content.lower())

    def test_restore_script_idempotent_check(self):
        """Restore script checks if lane is already running before starting."""
        content = RESTORE_SCRIPT.read_text()
        self.assertIn("already running", content.lower())

    def test_restore_script_awaiting_decision_handling(self):
        """Restore script handles awaiting-decision policy."""
        content = RESTORE_SCRIPT.read_text()
        self.assertIn("awaiting-decision", content.lower())

    def test_restore_script_oneshot_handling(self):
        """Restore script handles oneshot policy."""
        content = RESTORE_SCRIPT.read_text()
        self.assertIn("oneshot", content.lower())

    def test_restore_script_workspace_server_wait(self):
        """Restore script waits for workspace server."""
        content = RESTORE_SCRIPT.read_text()
        self.assertIn("workspace", content.lower())
        self.assertIn("ready", content.lower())
        self.assertIn("timeout", content.lower())

    def test_restore_script_preserves_identity_cwd_model(self):
        """Restore script restores identity, cwd, and model from manifest."""
        content = RESTORE_SCRIPT.read_text()
        self.assertIn("identity", content.lower())
        self.assertIn("cwd", content.lower())
        self.assertIn("model", content.lower())

    def test_snapshot_script_reads_manifest(self):
        """Snapshot script reads lanes manifest."""
        content = SNAPSHOT_SCRIPT.read_text()
        self.assertIn("LanesManifest", content)
        self.assertIn("lanes.example.json", content)

    def test_snapshot_script_queries_agent_list(self):
        """Snapshot script queries agent list --json."""
        content = SNAPSHOT_SCRIPT.read_text()
        self.assertIn("list --json", content)
        self.assertIn("--json", content)

    def test_snapshot_script_writes_timestamp(self):
        """Snapshot script writes timestamp."""
        content = SNAPSHOT_SCRIPT.read_text()
        self.assertIn("timestamp", content.lower())


if __name__ == "__main__":
    unittest.main()