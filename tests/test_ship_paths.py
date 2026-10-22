from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import ship_paths  # noqa: E402


class PushPathsTest(unittest.TestCase):
    def test_block_list_under_push(self):
        text = "name: x\non:\n  push:\n    branches:\n      - main\n    paths:\n      - 'a/**'  # c\n      - b.txt\n  pull_request:\n    paths:\n      - other/**\njobs: {}\n"
        self.assertEqual(ship_paths.push_paths(text), ["a/**", "b.txt"])

    def test_inline_list_and_quoted_on(self):
        self.assertEqual(ship_paths.push_paths('"on":\n  push:\n    paths: [a/**, "b/*.yml"]\n'),
                         ["a/**", "b/*.yml"])

    def test_no_push_or_no_paths_is_not_scoped(self):
        self.assertEqual(ship_paths.push_paths("on:\n  pull_request:\n    paths: [a/**]\n"), [])
        self.assertEqual(ship_paths.push_paths("on:\n  push:\n    branches: [main]\n"), [])
        self.assertEqual(ship_paths.push_paths("on: push\n"), [])


class MatchesTest(unittest.TestCase):
    def test_globs(self):
        self.assertTrue(ship_paths.matches(["a/**"], "a/b/c.py"))
        self.assertTrue(ship_paths.matches(["a/*.py"], "a/c.py"))
        self.assertFalse(ship_paths.matches(["a/*.py"], "a/b/c.py"))
        self.assertTrue(ship_paths.matches(["**/x.yml"], "x.yml"))
        self.assertFalse(ship_paths.matches(["a/**"], "ab/c"))

    def test_a_later_negation_excludes(self):
        self.assertFalse(ship_paths.matches(["a/**", "!a/docs/**"], "a/docs/x.md"))
        self.assertTrue(ship_paths.matches(["a/**", "!a/docs/**"], "a/x.md"))


class PathsIgnoreTest(unittest.TestCase):
    def test_paths_ignore_counts_every_path_it_does_not_ignore(self):
        block = ship_paths.push_paths("on:\n  push:\n    paths-ignore:\n      - 'docs/**'\n")
        inline = ship_paths.push_paths("on:\n  push:\n    paths-ignore: ['docs/**']\n")
        for patterns in (block, inline):
            self.assertTrue(ship_paths.matches(patterns, "src/x.py"))
            self.assertFalse(ship_paths.matches(patterns, "docs/x.md"))


class BadPatternTest(unittest.TestCase):
    def test_a_pattern_that_will_not_compile_counts_the_workflow_as_needed(self):
        self.assertTrue(ship_paths.matches(["a/[z-a]"], "b/c.py"))


if __name__ == "__main__":
    unittest.main()
