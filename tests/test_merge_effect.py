"""Merged-diff judgement (bus/merge_effect.py, loom#1570)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import merge_effect  # noqa: E402


def f(name, patch="+x"):
    return {"filename": name, "patch": patch}


NEVER = lambda name: []  # noqa: E731


class UnbuiltReasonTest(unittest.TestCase):
    def test_docs_only(self):
        self.assertIn("documentation", merge_effect.unbuilt_reason([f("README.md")]))

    def test_docs_and_tests_only(self):
        why = merge_effect.unbuilt_reason([f("docs/a.md"), f("tests/test_a.py")])
        self.assertIn("tests", why)

    def test_uncalled_function(self):
        why = merge_effect.unbuilt_reason(
            [f("bus/a.py", "+def decide(x):\n+    return x")], NEVER)
        self.assertIn("decide", why)

    def test_caller_in_the_diff(self):
        patch = "+def decide(x):\n+    return x\n+def run():\n+    return decide(1)"
        self.assertEqual(merge_effect.unbuilt_reason([f("bus/a.py", patch)], NEVER), "")

    def test_caller_in_the_base(self):
        self.assertEqual(merge_effect.unbuilt_reason(
            [f("bus/a.py", "+def decide(x):\n+    return x")], lambda n: ["bus/b.py"]), "")

    def test_failed_search_reads_as_built(self):
        self.assertEqual(merge_effect.unbuilt_reason(
            [f("bus/a.py", "+def decide(x):\n+    return x")], lambda n: None), "")

    def test_removed_lines_read_as_built(self):
        self.assertEqual(merge_effect.unbuilt_reason(
            [f("bus/a.py", "-old\n+def decide(x):\n+    return x")], NEVER), "")

    def test_non_code_config_change_reads_as_built(self):
        self.assertEqual(merge_effect.unbuilt_reason([f("ci.yml")], NEVER), "")


if __name__ == "__main__":
    unittest.main()
