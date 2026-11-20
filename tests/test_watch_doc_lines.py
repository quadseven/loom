"""The log watcher does not file a whole line of a tracked doc as an error."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import watch  # noqa: E402

DOC_LINE = "membership or permission error requires an operator to add Muse's existing"
REAL_ERROR = "fatal: membership or permission error from the API"


class DocLineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The checkout (with docs/) and the state directory are separate
        # trees, as on the station.
        self.checkout = os.path.join(tmp.name, "checkout")
        self.state = os.path.join(tmp.name, "state", "watch.json")
        os.makedirs(os.path.join(self.checkout, "docs"))
        with open(os.path.join(self.checkout, "docs", "MUSE.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Muse\n\n- %s\n" % DOC_LINE)

    def watcher(self, root):
        return watch.Watcher(None, "example-owner/lanes", [], self.state, 0.0,
                             lambda _line: None, root=root)

    def test_a_doc_line_is_not_kept(self):
        hits = self.watcher(self.checkout)._hits({}, [DOC_LINE], "brain")
        self.assertEqual(hits, [])

    def test_a_stamped_doc_line_is_not_kept(self):
        line = "[2026-11-16T07:31:23Z] " + DOC_LINE
        self.assertEqual(self.watcher(self.checkout)._hits({}, [line], "brain"), [])

    def test_a_real_error_line_is_kept(self):
        hits = self.watcher(self.checkout)._hits({}, [REAL_ERROR], "brain")
        self.assertEqual(hits, [REAL_ERROR])

    def test_docs_are_read_from_the_checkout_not_the_state_directory(self):
        # Docs next to the state file are not the checkout's.
        wrong = os.path.join(os.path.dirname(self.state), "docs")
        os.makedirs(wrong)
        self.assertTrue(os.path.isdir(wrong))
        hits = self.watcher(os.path.dirname(self.state))._hits({}, [DOC_LINE], "brain")
        self.assertEqual(hits, [DOC_LINE])

    def test_default_root_is_the_running_checkout(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(watch.__file__)))
        self.assertEqual(self.watcher(None).root, root)

    def test_doc_lines_are_per_watcher(self):
        first = self.watcher(self.checkout)
        other = self.watcher(os.path.dirname(self.state))
        first._hits({}, [DOC_LINE], "brain")
        self.assertEqual(other._hits({}, [DOC_LINE], "brain"), [DOC_LINE])


if __name__ == "__main__":
    unittest.main()
