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


def run(code: str) -> str:
    """Run `code` in a fresh interpreter; the child's stderr is the failure."""
    out = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError("child exited %d: %s" % (out.returncode, out.stderr.strip()))
    return out.stdout


def loaded(module: str) -> set:
    return set(run("import sys, %s; print('\\n'.join(m for m in sys.modules "
                   "if m.startswith('bus.')))" % module).split())


# Each call reaches one lazy import from a fresh interpreter, so a lazy import
# that no longer resolves fails here and not first in a lane pass.
CALLS = (
    "from bus import operator_hold; print(operator_hold.host_only('## Acceptance criteria\\n'"
    "'- [ ] `pytest` passes'))",
    "from bus import operator_hold; print(repr(operator_hold.measurement_reason("
    "None, 'o/r', {}, 'A plain ticket.', [])))",
    "from bus import pr_repair; print(pr_repair.build_checklist('A plain ticket.', '- [ ] x'))",
    "import pathlib; from bus import evidence; print(evidence.lines_since("
    "pathlib.Path('/nonexistent/capture'), 'ready', 0.0, 1.0, 1.0))",
)


class ImportGraphTest(unittest.TestCase):
    def test_runner_modules_skip_the_close_gate(self):
        for module, absent in KEPT_OUT.items():
            with self.subTest(module=module):
                found = loaded(module)
                self.assertIn(module, found)
                self.assertEqual([m for m in absent if m in found], [])

    def test_lazy_imports_resolve_when_called(self):
        for code in CALLS:
            with self.subTest(code=code):
                run(code)


if __name__ == "__main__":
    unittest.main()
