"""A Builds on declaration is read from a body with CRLF line endings.

Bodies edited in the GitHub web editor commonly carry CRLF. The
declaration line then ends in a carriage return before the newline,
and a pattern that allows only spaces and tabs before the end of the
line never matches it, so the body reads as having no declared stack.
"""

import unittest

from bus import brain


class BuildsOnLineEndings(unittest.TestCase):
    def number(self, body):
        match = brain.BUILDS_ON_RE.search(body)
        return match.group(1) if match else None

    def test_a_crlf_body_declares_its_stack(self):
        self.assertEqual(self.number("Intro\r\nBuilds on #5\r\nMore\r\n"), "5")

    def test_a_crlf_declaration_on_the_last_line_is_read(self):
        self.assertEqual(self.number("Intro\r\nBuilds on #12\r\n"), "12")
        self.assertEqual(self.number("Intro\r\nBuilds on #12\r"), "12")

    def test_trailing_blanks_before_a_crlf_are_allowed(self):
        self.assertEqual(self.number("Intro\r\nBuilds on #7 \t\r\nMore"), "7")

    def test_an_lf_body_still_declares_its_stack(self):
        self.assertEqual(self.number("Intro\nBuilds on #5\nMore\n"), "5")
        self.assertEqual(self.number("Builds on #5"), "5")

    def test_the_captured_number_carries_no_carriage_return(self):
        match = brain.BUILDS_ON_RE.search("Builds on #5\r\n")
        self.assertEqual(match.group(1), "5")
        self.assertEqual(int(match.group(1)), 5)

    def test_text_after_the_number_is_still_not_a_declaration(self):
        self.assertIsNone(self.number("Builds on #5 and more\r\n"))
        self.assertIsNone(self.number("Builds on #5\rmore\r\n"))
        self.assertIsNone(self.number("It builds on #5\r\n"))


if __name__ == "__main__":
    unittest.main()
