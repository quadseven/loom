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
# page's Upstream fake. test_run_lane is left out: its run takes about three
# minutes, and its servers already sit behind the probe.
SERVES = re.compile(r'HTTPServer\(\("127\.0\.0\.1"|Upstream\(\)')
SERVERS = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py")
                 if SERVES.search(p.read_text()) and p.stem != "test_run_lane")
# Loaded first by the child Python, it refuses every connect the way the lane
# check sandbox does, so the LOCALHOST probe reads False there too.
DENY_CONNECT = """import socket
def _deny(self, *args):
    raise PermissionError(1, "Operation not permitted")
socket.socket.connect = _deny
socket.socket.connect_ex = lambda self, *args: 1
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
        with tempfile.TemporaryDirectory() as shim:
            Path(shim, "sitecustomize.py").write_text(DENY_CONNECT)
            env = dict(os.environ, PYTHONPATH=shim)
            for name in SERVERS:
                with self.subTest(module=name):
                    result = subprocess.run([sys.executable, "-B", "-m", "unittest", f"tests.{name}"],
                                            cwd=ROOT, env=env, capture_output=True, text=True,
                                            timeout=300)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_the_probes_are_booleans(self):
        from tests import sandbox_caps
        for flag in (sandbox_caps.NESTED_SANDBOX, sandbox_caps.PROCESS_TABLE, sandbox_caps.LOCALHOST):
            self.assertIsInstance(flag, bool)


if __name__ == "__main__":
    unittest.main()
