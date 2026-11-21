"""A header block packed onto one line reads as one header per key."""
import unittest

from bus.tickets import parse


class PackedBriefHeaders(unittest.TestCase):
    def test_packed_line_reads_as_three_headers(self):
        headers, rest = parse("Difficulty: medium Priority: P2 Model: gpt-6-luna\n\n## Why\nx")
        self.assertEqual(headers["Difficulty"], "medium")
        self.assertEqual(headers["Priority"], "P2")
        self.assertEqual(headers["Model"], "gpt-6-luna")
        self.assertTrue(rest.startswith("## Why"))

    def test_packed_line_then_a_header_line(self):
        headers, _ = parse("Difficulty: easy Priority: P1\nNot before: 2026-01-02T03:04:05Z\n\nbody")
        self.assertEqual(headers["Difficulty"], "easy")
        self.assertEqual(headers["Priority"], "P1")
        self.assertEqual(headers["Not before"], "2026-01-02T03:04:05Z")

    def test_a_bad_value_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, "Difficulty: must be one of easy, medium, hard"):
            parse("Difficulty: hardest\n\nbody")
        with self.assertRaisesRegex(ValueError, "Difficulty: must be one of easy, medium, hard"):
            parse("Difficulty: hardest Priority: P2\n\nbody")

    def test_a_repeated_packed_header_is_one_header(self):
        self.assertEqual(parse("Difficulty: hard Difficulty: hard\n\nbody")[0], {"Difficulty": "hard"})


if __name__ == "__main__":
    unittest.main()
