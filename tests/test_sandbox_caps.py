"""The capability probes the lane check sandbox relies on."""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USERS = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py")
               if "sandbox_caps" in p.read_text() and p.stem != "test_sandbox_caps")
# Modules that start a localhost HTTP server, directly or through the tiers
# page's Upstream fake, or build the dashboard's own server. test_run_lane is
# left out: its run takes about three minutes, and its servers already sit
# behind the probe.
SERVES = re.compile(r'HTTPServer\(\("127\.0\.0\.1"|Upstream\(\)|server_from_env\(|make_server\(')
SERVERS = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py")
                 if SERVES.search(p.read_text()) and p.stem != "test_run_lane")
# Loaded first by the child Python, it refuses every connect and every bind
# the way the lane check sandbox does, so the LOCALHOST probe reads False there
# too. A sandbox may deny bind and allow connect, or the reverse.
DENY_CONNECT = """import socket
def _deny(self, *args):
    raise PermissionError(1, "Operation not permitted")
socket.socket.connect = _deny
socket.socket.bind = _deny
socket.socket.connect_ex = lambda self, *args: 1
"""

# Modules that bind an AF_UNIX socket. test_run_lane is included: a static
# source check is cheap where a child run of it is not.
UNIX_BINDERS = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py")
                      if "AF_UNIX" in p.read_text() and p.stem != "test_sandbox_caps")
# A sandbox that allows TCP binds and denies only AF_UNIX binds.
DENY_UNIX_BIND = """import socket
_bind = socket.socket.bind
def _guard(self, address):
    if self.family == socket.AF_UNIX:
        raise PermissionError(1, "Operation not permitted")
    return _bind(self, address)
socket.socket.bind = _guard
"""


class SandboxCapsTest(unittest.TestCase):
    def test_every_module_using_the_probes_also_loads_by_module_name(self):
        # `python -m unittest tests.test_run_lane`, the way a lane reruns one
        # failing module, imports from the repo root, not from tests/.
        self.assertTrue(USERS)
        for name in USERS:
            with self.subTest(module=name):
                result = subprocess.run([sys.executable, "-B", "-c", f"import tests.{name}"],
                                        cwd=ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_every_localhost_server_module_passes_when_connects_are_denied(self):
        # Two tiers page modules connected to their own fake upstream without
        # the probe. Inside the check sandbox seven of their tests failed, so
        # every loom base check exited 1 and 14 tickets sat held behind a
        # factory incident while CI stayed green (2026-10-20).
        self.assertIn("test_dashboard_tiers_sources", SERVERS)
        self.assertIn("test_dashboard_main", SERVERS)
        self.assertIn("test_no_reverse_lookup", SERVERS)
        with tempfile.TemporaryDirectory() as shim:
            Path(shim, "sitecustomize.py").write_text(DENY_CONNECT)
            env = dict(os.environ, PYTHONPATH=shim)
            for name in SERVERS:
                with self.subTest(module=name):
                    result = subprocess.run([sys.executable, "-B", "-m", "unittest", f"tests.{name}"],
                                            cwd=ROOT, env=env, capture_output=True, text=True,
                                            timeout=300)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_every_module_binding_a_unix_socket_consults_the_unix_probe(self):
        # api_lane() in test_run_lane guarded on LOCALHOST alone, so a sandbox
        # that allowed TCP binds and denied AF_UNIX binds failed 23 tests.
        self.assertIn("test_run_lane", UNIX_BINDERS)
        for name in UNIX_BINDERS:
            with self.subTest(module=name):
                source = (ROOT / "tests" / f"{name}.py").read_text()
                self.assertIn("sandbox_caps.UNIX_SOCKET", source)
                self.assertIn("sandbox_caps.NO_UNIX_SOCKET", source)

    def test_the_unix_probe_reads_false_when_only_unix_binds_are_denied(self):
        code = "from tests import sandbox_caps as c; print(c.LOCALHOST, c.UNIX_SOCKET)"
        with tempfile.TemporaryDirectory() as shim:
            Path(shim, "sitecustomize.py").write_text(DENY_UNIX_BIND)
            env = dict(os.environ, PYTHONPATH=shim)
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        # The shim leaves TCP alone, so the child's LOCALHOST is this process's:
        # True wherever TCP is allowed, and this test still passes where it is not.
        from tests import sandbox_caps
        self.assertEqual(result.stdout.split(), [str(sandbox_caps.LOCALHOST), "False"])

    def test_the_probes_are_booleans(self):
        from tests import sandbox_caps
        for flag in (sandbox_caps.NESTED_SANDBOX, sandbox_caps.PROCESS_TABLE, sandbox_caps.LOCALHOST,
                     sandbox_caps.UNIX_SOCKET):
            self.assertIsInstance(flag, bool)


if __name__ == "__main__":
    unittest.main()
