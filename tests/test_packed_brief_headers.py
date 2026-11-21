"""Test that the brief header parser reads several headers packed on one line."""
import unittest

from bus.tickets import parse


class PackedBriefHeadersTest(unittest.TestCase):
    """Headers packed on one line should be split and parsed individually."""

    def test_three_headers_on_one_line_are_parsed(self):
        body = "Difficulty: medium Priority: P2 Model: gpt-6-luna\n\n## Why\nx\n"
        headers, rest = parse(body)
        self.assertEqual(headers["Difficulty"], "medium")
        self.assertEqual(headers["Priority"], "P2")
        self.assertEqual(headers["Model"], "gpt-6-luna")
        self.assertIn("## Why", rest)

    def test_packed_headers_with_invalid_difficulty_still_raises(self):
        body = "Difficulty: hardest Priority: P2 Model: gpt-6-luna\n\n## Why\nx\n"
        with self.assertRaisesRegex(ValueError, "Difficulty: must be one of"):
            parse(body)

    def test_packed_headers_with_invalid_priority_still_raises(self):
        body = "Difficulty: medium Priority: P9 Model: gpt-6-luna\n\n## Why\nx\n"
        with self.assertRaisesRegex(ValueError, "Priority: must be one of"):
            parse(body)

    def test_packed_headers_with_invalid_model_still_raises(self):
        # MODEL_RE allows letters, digits, . _ : / @ - but not spaces
        body = "Difficulty: medium Priority: P2 Model: not a model\n\n## Why\nx\n"
        with self.assertRaisesRegex(ValueError, "Model:"):
            parse(body)

    def test_packed_headers_followed_by_normal_headers(self):
        body = ("Difficulty: medium Priority: P2 Model: gpt-6-luna\n"
                "Foundational: yes\n"
                "Not before: 2026-11-21T12:00:00Z\n\n## Why\nx\n")
        headers, rest = parse(body)
        self.assertEqual(headers["Difficulty"], "medium")
        self.assertEqual(headers["Priority"], "P2")
        self.assertEqual(headers["Model"], "gpt-6-luna")
        self.assertEqual(headers["Foundational"], "yes")
        self.assertEqual(headers["Not before"], "2026-11-21T12:00:00Z")

    def test_packed_line_with_unknown_key_is_not_split(self):
        # A line where not every part starts with a known key should not be split
        # and should fail validation on the first header's value
        body = "Difficulty: medium Unknown: value Priority: P2\n\n## Why\nx\n"
        with self.assertRaisesRegex(ValueError, "Difficulty: must be one of"):
            parse(body)

    def test_packed_headers_with_extra_whitespace(self):
        body = "Difficulty: medium  Priority: P2  Model: gpt-6-luna\n\n## Why\nx\n"
        headers, rest = parse(body)
        self.assertEqual(headers["Difficulty"], "medium")
        self.assertEqual(headers["Priority"], "P2")
        self.assertEqual(headers["Model"], "gpt-6-luna")

    def test_packed_after_header_works(self):
        body = "Difficulty: easy Priority: P1 After: #5, #7\n\n## Why\nx\n"
        headers, rest = parse(body)
        self.assertEqual(headers["Difficulty"], "easy")
        self.assertEqual(headers["Priority"], "P1")
        self.assertEqual(headers["After"], "#5, #7")

    def test_packed_deliverable_header_works(self):
        body = "Difficulty: easy Deliverable: answer\n\n## Why\nx\n"
        headers, rest = parse(body)
        self.assertEqual(headers["Difficulty"], "easy")
        self.assertEqual(headers["Deliverable"], "answer")

    def test_packed_repairs_incident_header_works(self):
        body = "Difficulty: easy Repairs incident: #42\n\n## Why\nx\n"
        headers, rest = parse(body)
        self.assertEqual(headers["Difficulty"], "easy")
        self.assertEqual(headers["Repairs incident"], "#42")


if __name__ == "__main__":
    unittest.main()