"""The modules the lane runner starts with `python3 -m` once or more a pass
import only what their commands read (#3662). Each check runs in a fresh
interpreter, since this suite has already imported everything."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# module -> bus modules it must not load at import
KEPT_OUT = {
    "bus.operator_hold": ("bus.brief_check", "bus.criteria_gate", "bus.tickets"),
    "bus.pr_repair": ("bus.tickets", "bus.criteria_gate"),
    "bus.evidence": ("bus.log_capture",),
    "bus.risk_line": ("bus.tickets", "bus.criteria_gate", "bus.brief_check"),
}


def loaded(module: str) -> set:
    code = ("import sys, %s; print('\\n'.join(m for m in sys.modules if m.startswith('bus.')))"
            % module)
    out = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, check=True,
                         capture_output=True, text=True, timeout=60).stdout
    return set(out.split())


class ImportGraphTest(unittest.TestCase):
    def test_runner_modules_skip_the_close_gate(self):
        for module, absent in KEPT_OUT.items():
            with self.subTest(module=module):
                found = loaded(module)
                self.assertIn(module, found)
                self.assertEqual([m for m in absent if m in found], [])


if __name__ == "__main__":
    unittest.main()
