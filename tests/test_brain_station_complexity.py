"""Complexity caps for the two deferred PR 301 high-complexity findings.

The review bot caps a function at cyclomatic complexity 15. The deferred
review findings tracked by this ticket flagged high complexity in
`bus/brain.py` and `bus/station.py`; this pins the two refactored functions
under the cap so the warnings stay resolved.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAP = 15
TARGETS = (
    ("bus/brain.py", "_ready_sensitive"),
    ("bus/station.py", "Station._deliver_unlinked"),
)


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


def qualified_names(path: str):
    """Yield (qualified name, node) for every function in the file."""
    tree = ast.parse((ROOT / path).read_text(), filename=path)
    found = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(("%s%s" % (prefix, child.name), child))
                visit(child, "%s%s." % (prefix, child.name))
            elif isinstance(child, ast.ClassDef):
                visit(child, "%s%s." % (prefix, child.name))
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


class BrainStationComplexityTest(unittest.TestCase):
    def test_deferred_findings_are_under_the_cap(self):
        over = ["%s:%d %s (%d)" % (path, node.lineno, name, complexity(node))
                for path, want in TARGETS
                for name, node in qualified_names(path)
                if name == want and complexity(node) > CAP]
        self.assertEqual(over, [])

    def test_both_targets_still_exist(self):
        names = {path: {name for name, _ in qualified_names(path)}
                 for path, _ in TARGETS}
        for path, want in TARGETS:
            self.assertIn(want, names[path], "%s no longer defines %s" % (path, want))


if __name__ == "__main__":
    unittest.main()
