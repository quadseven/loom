"""The capability probes the lane check sandbox relies on."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USERS = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py")
               if "sandbox_caps" in p.read_text() and p.stem != "test_sandbox_caps")


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

    def test_the_probes_are_booleans(self):
        from tests import sandbox_caps
        for flag in (sandbox_caps.NESTED_SANDBOX, sandbox_caps.PROCESS_TABLE, sandbox_caps.LOCALHOST):
            self.assertIsInstance(flag, bool)


if __name__ == "__main__":
    unittest.main()
