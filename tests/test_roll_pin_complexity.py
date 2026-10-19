"""Every function in the roll pin module and its fake stays readable.

The review bot caps a function at cyclomatic complexity 15. `_update` in
`bus/roll_pin.py` and `FakeGitHub.rest` in `tests/test_roll_pin.py` landed
above it (#531); this keeps both files under the cap.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = ("bus/roll_pin.py", "tests/test_roll_pin.py")
CAP = 15


def complexity(function: ast.AST) -> int:
    """McCabe complexity: one, plus one per branch, loop, handler,
    comprehension clause and extra boolean operand."""
    count = 1
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor,
                             ast.While, ast.ExceptHandler, ast.match_case)):
            count += 1
        elif isinstance(node, ast.comprehension):
            count += 1 + len(node.ifs)
        elif isinstance(node, ast.BoolOp):
            count += len(node.values) - 1
    return count


def functions(path: str):
    tree = ast.parse((ROOT / path).read_text(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


class RollPinComplexityTest(unittest.TestCase):
    def test_counter_counts_branches_loops_and_boolean_operands(self):
        source = ("def f(a, b):\n"
                  "    if a and b:\n"
                  "        return [x for x in a if x]\n"
                  "    for item in b:\n"
                  "        pass\n")
        self.assertEqual(complexity(ast.parse(source).body[0]), 6)

    def test_every_function_is_under_the_cap(self):
        over = ["%s:%d %s (%d)" % (path, node.lineno, node.name, complexity(node))
                for path in FILES for node in functions(path)
                if complexity(node) > CAP]
        self.assertEqual(over, [])


if __name__ == "__main__":
    unittest.main()
