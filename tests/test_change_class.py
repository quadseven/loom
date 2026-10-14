"""Change classes (bus/change_class.py) and the Intent section."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import change_class  # noqa: E402


class ChangeClassTest(unittest.TestCase):
    def test_docs_only(self):
        files = ["README.md", "docs/guide.md", "LICENSE", "notes.txt"]
        self.assertEqual(change_class.classify(files, []), "docs")

    def test_tests_only(self):
        files = ["tests/test_a.py", "src/x_test.go"]
        self.assertEqual(change_class.classify(files, []), "tests")

    def test_mixed_logic(self):
        files = ["README.md", "tests/test_a.py", "src/a.py"]
        self.assertEqual(change_class.classify(files, []), "logic")

    def test_one_sensitive_path_wins_over_everything(self):
        patterns = ["bus/brain.py", ".github/"]
        files = ["README.md", "tests/test_a.py", "src/a.py",
                 ".github/workflows/ci.yml"]
        self.assertEqual(change_class.classify(files, patterns), "sensitive")
        self.assertEqual(change_class.classify(["bus/brain.py"], patterns),
                         "sensitive")

    def test_globs_read_with_comments_ignored(self):
        text = "# a comment\n\nbus/brain.py\n  # indented comment\n.github/\n"
        self.assertEqual(change_class.parse_patterns(text),
                         ["bus/brain.py", ".github/"])

    def test_intent_section_rendered_at_top_of_body(self):
        counts = {"sensitive": 0, "logic": 1, "tests": 1, "docs": 0}
        intent = change_class.render_intent(
            "logic", ["Fix the thing"], counts)
        body = change_class.with_intent("## Why\nx\n", intent)
        self.assertTrue(body.startswith("## Intent\n"))
        self.assertIn("Class: logic", body)
        self.assertIn("- Fix the thing", body)
        self.assertIn("Files:", body)
        self.assertIn("## Why\nx", body)

    def test_behavior_lines_fall_back_to_what(self):
        self.assertEqual(change_class.behavior_lines("", "Make it work.\n\nAgain."),
                         ["Make it work.", "Again."])
        self.assertEqual(change_class.behavior_lines("Did one.\nDid two.", "Other"),
                         ["Did one.", "Did two."])


if __name__ == "__main__":
    unittest.main()
