"""Test _with_header handles packed header lines correctly."""
import unittest
from bus.brain import _with_header


class WithHeaderPackedLineTest(unittest.TestCase):
    """_with_header should split packed headers before replacing one."""

    def test_packed_line_is_split_and_only_target_replaced(self):
        body = "Difficulty: medium Not before: 2026-12-01T00:00:00Z After: #1, #2 Priority: P3\n\n## Why\nx\n"
        result = _with_header(body, "Difficulty", "easy")
        # Each header should be on its own line
        self.assertIn("Difficulty: easy\n", result)
        self.assertIn("Not before: 2026-12-01T00:00:00Z\n", result)
        self.assertIn("After: #1, #2\n", result)
        self.assertIn("Priority: P3\n", result)
        # The order should be preserved
        lines = result.split("\n")
        header_lines = [l for l in lines if l and not l.startswith("#")]
        self.assertEqual(header_lines[0], "Difficulty: easy")
        self.assertEqual(header_lines[1], "Not before: 2026-12-01T00:00:00Z")
        self.assertEqual(header_lines[2], "After: #1, #2")
        self.assertEqual(header_lines[3], "Priority: P3")

    def test_packed_line_with_different_target_key(self):
        body = "Difficulty: medium Not before: 2026-12-01T00:00:00Z After: #1, #2 Priority: P3\n\n## Why\nx\n"
        result = _with_header(body, "Priority", "P0")
        self.assertIn("Difficulty: medium\n", result)
        self.assertIn("Not before: 2026-12-01T00:00:00Z\n", result)
        self.assertIn("After: #1, #2\n", result)
        self.assertIn("Priority: P0\n", result)
        lines = result.split("\n")
        header_lines = [l for l in lines if l and not l.startswith("#")]
        self.assertEqual(header_lines[0], "Difficulty: medium")
        self.assertEqual(header_lines[1], "Not before: 2026-12-01T00:00:00Z")
        self.assertEqual(header_lines[2], "After: #1, #2")
        self.assertEqual(header_lines[3], "Priority: P0")

    def test_packed_line_not_before_target(self):
        body = "Difficulty: medium Not before: 2026-12-01T00:00:00Z After: #1, #2 Priority: P3\n\n## Why\nx\n"
        result = _with_header(body, "Not before", "2026-11-01T00:00:00Z")
        self.assertIn("Difficulty: medium\n", result)
        self.assertIn("Not before: 2026-11-01T00:00:00Z\n", result)
        self.assertIn("After: #1, #2\n", result)
        self.assertIn("Priority: P3\n", result)

    def test_packed_line_after_target(self):
        body = "Difficulty: medium Not before: 2026-12-01T00:00:00Z After: #1, #2 Priority: P3\n\n## Why\nx\n"
        result = _with_header(body, "After", "#5, #6")
        self.assertIn("Difficulty: medium\n", result)
        self.assertIn("Not before: 2026-12-01T00:00:00Z\n", result)
        self.assertIn("After: #5, #6\n", result)
        self.assertIn("Priority: P3\n", result)


if __name__ == "__main__":
    unittest.main()