"""bus/text.py keeps one definition of each check (loom#503)."""
import ast
import collections
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import text  # noqa: E402


class TextModuleTest(unittest.TestCase):
    def test_each_module_level_name_is_bound_once(self):
        # A second binding silently replaces the first, so two copies of a
        # pattern can drift and only the later one is ever used.
        tree = ast.parse((ROOT / "bus" / "text.py").read_text())
        names = collections.Counter(
            target.id
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name))
        self.assertEqual(sorted(n for n, c in names.items() if c > 1), [])

    def test_private_hostname_is_refused(self):
        with self.assertRaisesRegex(ValueError, "private hostname"):
            text.check("the job on service.internal failed")
        text.check("see docs/ROADMAP.md for the plan")


if __name__ == "__main__":
    unittest.main()
