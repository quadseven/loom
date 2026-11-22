"""A brief whose body already starts with the headers the tool writes files
them once, and refuses a leading header that names another value."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_bus_brain import BODY, EPIC, REPO, Fake, goal, reply  # noqa: E402


class BriefDuplicateHeadersTests(unittest.TestCase):
    def _filed(self, body):
        fake = Fake(reply(200, goal()),
                    reply(200, []),
                    reply(201, {"id": 202, "html_url": "https://example.com/t"}),
                    reply(201, {}))
        with tempfile.TemporaryDirectory() as home:
            brain.brief(GitHub("t", transport=fake), REPO, EPIC, "Ticket", body, "codex",
                        difficulty="medium", priority="P2", env={"LOOM_HOME": home})
        return next(c[2] for c in fake.calls
                    if c[0] == "POST" and c[1].endswith("/issues"))["body"]

    def test_a_body_that_starts_with_the_tool_headers_is_filed_with_one_block(self):
        filed = self._filed("Difficulty: medium\nPriority: P2\n\n" + BODY)
        self.assertEqual(filed.count("Difficulty:"), 1)
        self.assertEqual(filed.count("Priority:"), 1)
        self.assertTrue(filed.startswith("Difficulty: medium\nPriority: P2\n\n## Why"))

    def test_a_leading_header_naming_another_value_is_refused_with_both(self):
        with self.assertRaisesRegex(ValueError, "P1.*P2"):
            self._filed("Priority: P1\n\n" + BODY)

    def test_a_leading_line_the_tool_does_not_write_keeps_the_body(self):
        body = "Difficulty: medium\nDeliverable: a note\n\n" + BODY
        self.assertEqual(brain._without_own_headers(body, {"Difficulty": "medium"}), body)


if __name__ == "__main__":
    unittest.main()
