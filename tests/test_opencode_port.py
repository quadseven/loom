"""The loopback port an opencode 2.x run's server gets (lanes/agent_sandbox.sh),
with the shared sandbox file sourced into bash as the lanes source it.

The picker hands out a port nothing holds, a picker that fails or prints
something else says why before the run parks, and the server is never
started on a port another process already listens on (Refs #219)."""

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import sandbox_caps

LANES = Path(__file__).resolve().parent.parent / "lanes"
SANDBOX = LANES / "agent_sandbox.sh"


class OpencodePortTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.marker = self.tmp / "started"

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)
        return path

    def bash(self, script, *args, fake_path=False, **env):
        full = dict(os.environ, NET_TIMEOUT="5", AGENT_TIMEOUT="5", LANE="opencode-go",
                    MODEL="opencode-go/deepseek-v4.1-flash", AGENT_HOME=str(self.tmp / "home"),
                    MARKER=str(self.marker), **env)
        if fake_path:
            full["PATH"] = "%s:%s" % (self.bin, os.environ["PATH"])
        return subprocess.run(["bash", "-c", '. "$1"; ' + script, "bash", str(SANDBOX), *args],
                              capture_output=True, text=True, env=full, timeout=60)

    def test_the_picker_prints_a_free_port_in_the_dynamic_range(self):
        result = self.bash("free_loopback_port")
        self.assertEqual(result.returncode, 0, result.stderr)
        port = int(result.stdout)
        self.assertGreaterEqual(port, 49152)
        self.assertLessEqual(port, 65535)
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))

    def test_a_failed_picker_names_the_fault(self):
        self.tool("python3", "echo 'boom: no socket module' >&2; exit 1\n")
        result = self.bash("free_loopback_port", fake_path=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("no free loopback port for the opencode server", result.stderr)
        self.assertIn("boom: no socket module", result.stderr)

    def test_a_picker_that_prints_no_port_is_refused_by_name(self):
        for printed in ("", "garbage", "80", "70000", "50000 50001"):
            with self.subTest(printed=printed):
                self.tool("python3", "printf '%%s\\n' '%s'\n" % printed)
                result = self.bash("free_loopback_port", fake_path=True)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn("not a port in 49152-65535", result.stderr)

    def test_opencode_run_parks_with_the_reason_and_starts_nothing(self):
        self.tool("python3", "exit 1\n")
        opencode = self.tool("opencode", 'echo "opencode v2.0.28"\n')
        result = self.bash('api_agent() { touch "$MARKER"; }; opencode_run - "$2" brief',
                           str(opencode), fake_path=True)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("no free loopback port for the opencode server", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_the_sandbox_names_a_port_it_refuses(self):
        result = self.bash('sandbox_profile "$PWD" bash "" garbage')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("refusing sandbox: loopback port garbage is not in 49152-65535", result.stderr)

    @unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
    def test_the_server_never_starts_on_a_port_another_process_holds(self):
        cli = self.tool("fake-cli", 'touch "$MARKER"\n')
        with socket.socket() as held:
            for port in range(65535, 49151, -1):
                try:
                    held.bind(("127.0.0.1", port))
                    break
                except OSError:
                    continue
            held.listen(1)
            result = self.bash('api_agent - "$CLI" run', fake_path=True, CLI=str(cli),
                               OPENCODE_SERVER_PORT=str(port), API_KEY="")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("loopback port %d is already in use" % port, result.stdout + result.stderr)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
