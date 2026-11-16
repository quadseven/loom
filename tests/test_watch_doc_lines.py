"""Tests for the doc line filtering in the log watcher.

This module tests that the log watcher's `keep` function correctly filters out
lines that are exact copies from documentation files. Previously, any line from
docs/ that contained the word "error" was treated as an error line.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import watch


class DocLineFilterTest(unittest.TestCase):
    """Test that doc lines are not treated as errors."""

    def test_doc_line_is_filtered_out(self):
        """A line that is exactly a line from a docs file should be filtered out.

        The fix reads all lines from docs files and checks if the current line
        matches any of them exactly.
        """
        # Create a temporary checkout with a docs file
        tmp = tempfile.TemporaryDirectory()
        try:
            docs_dir = os.path.join(tmp.name, "state", "test", "docs")
            os.makedirs(docs_dir)

            docs_file = os.path.join(docs_dir, "MUSE.md")
            with open(docs_file, "w", encoding="utf-8") as f:
                f.write("membership or permission error requires an operator to add Muse's existing\n")

            # Create a watcher to load the doc lines
            log_path = os.path.join(tmp.name, "logs", "test.log")
            state_path = os.path.join(tmp.name, "state", "test", "watch.json")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            os.makedirs(os.path.dirname(state_path), exist_ok=True)

            watcher = watch.Watcher(None, "owner/repo", [("test", log_path)], state_path, 0, lambda _: None)

            # Normalize the line the way the watcher does it
            stripped = watch._LEAD_RE.sub("", "membership or permission error requires an operator to add Muse's existing")
            stripped = stripped.rstrip("\r\n")

            # This should now return False
            self.assertFalse(watch.keep(stripped), f"Line should be filtered out: {stripped!r}")

        finally:
            tmp.cleanup()

    def test_actual_error_line_is_kept(self):
        """A real error line that is not a doc line should still be kept.

        This ensures that the fix does not filter out legitimate error lines.
        """
        # These lines should still be treated as errors even though they contain the word "error"
        lines_to_keep = [
            "fatal: membership or permission error from the API",
            "error: connection refused",
            "ValueError: bad",
            "HTTP 403 from the API",
        ]

        for line in lines_to_keep:
            self.assertTrue(watch.keep(line), f"Line should be kept: {line!r}")

    def test_doc_line_with_shared_words_is_kept(self):
        """A doc line that only shares words with another line should be kept.

        The fix only filters out lines that are EXACT matches to doc lines, not
        lines that only share words.
        """
        # Create a doc line that shares words with a potential error
        doc_line = "this has error in it but is from a doc"
        error_line = "error: something went wrong"

        self.assertTrue(watch.keep(doc_line), f"Doc line with shared words should be kept: {doc_line!r}")
        self.assertTrue(watch.keep(error_line), f"Error line should be kept: {error_line!r}")


class WatcherWithDocsTest(unittest.TestCase):
    """Test the full watcher behavior with doc lines."""

    def setUp(self):
        """Set up a temporary environment for testing."""
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.log = os.path.join(self.dir, "logs", "brain.log")
        os.makedirs(os.path.dirname(self.log))
        self.state = os.path.join(self.dir, "state", "station", "watch.json")
        self.gh = watch.Watcher(None, "owner/repo", [("brain", self.log)], self.state, 0, lambda _: None)
        # Simulate reading the docs file once
        docs_dir = os.path.join(self.dir, "docs")
        os.makedirs(docs_dir)

    def tearDown(self):
        """Clean up temporary files."""
        self.tmp.cleanup()

    def test_watcher_ignores_doc_lines(self):
        """The watcher should not file issues for doc lines."""
        docs_file = os.path.join(self.dir, "docs", "MUSE.md")
        with open(docs_file, "w", encoding="utf-8") as f:
            f.write("fails, record the exact command and complete error. A private-channel")

        # Add the doc line to the log
        with open(self.log, "a", encoding="utf-8") as f:
            f.write("fails, record the exact command and complete error. A private-channel\n")

        # Run the watcher
        self.gh.run()

        # No issue should be filed
        self.assertEqual(self.gh.issues, {})


if __name__ == "__main__":
    unittest.main()
