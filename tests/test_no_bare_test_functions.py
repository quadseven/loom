"""CI runs tests with plain unittest, which collects only TestCase methods.

A module-level `def test_...` in tests/ is never run there, so it passes
forever whatever it asserts. 18 such functions sat on main unnoticed (#2521).
"""
import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).parent


def bare_test_functions(directory):
    """Every top-level function in directory's Python files that unittest would skip."""
    found = []
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found.append(f"{path.relative_to(directory)}:{node.lineno} {node.name}")
    return found


class BareTestFunctionsTest(unittest.TestCase):
    def test_no_test_function_sits_outside_a_testcase(self):
        found = bare_test_functions(TESTS)
        self.assertEqual(found, [], "unittest never runs these; move each into a TestCase class:\n"
                         + "\n".join(found))
