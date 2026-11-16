from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import watch


def load_doc_lines():
    """Load all lines from documentation files into a set for exact matching."""
    doc_lines = set()
    docs_dir = os.path.join(os.path.dirname(__file__), "docs")
    if not os.path.exists(docs_dir):
        return doc_lines
    
    for root, _, files in os.walk(docs_dir):
        for file in files:
            file_path = os.path.join(root, file)
            if not file_path.lower().endswith((".md", ".txt")):
                continue
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        stripped = line.rstrip("\r\n")
                        if stripped:
                            doc_lines.add(stripped)
            except (OSError, IOError):
                pass
    return doc_lines


# Load and update the module-level WATCHER_DOC_LINES once at module import
_module_doc_lines = load_doc_lines()
WATCHER_DOC_LINES = watch.WATCHER_DOC_LINES.copy()
WATCHER_DOC_LINES.update(_module_doc_lines)
watch.WATCHER_DOC_LINES.clear()
watch.WATCHER_DOC_LINES.update(WATCHER_DOC_LINES)


class WatchDocLinesTest(unittest.TestCase):
    """Tests for the log watcher filtering out lines that are exact matches to documentation files."""

    def test_keep_false_for_doc_line_matching_muse_error(self):
        """Test that keep returns False for the line from docs/MUSE.md."""
        # This line exists in docs/MUSE.md (line 20)
        doc_line = "membership or permission error requires an operator to add Muse's existing"
        
        # We need to ensure this line is in WATCHER_DOC_LINES by loading doc lines
        doc_lines = load_doc_lines()
        original_doc_lines = watch.WATCHER_DOC_LINES.copy()
        
        # Save the original state
        watch.WATCHER_DOC_LINES.clear()
        watch.WATCHER_DOC_LINES.update(doc_lines)
        
        # Verify the line is loaded
        self.assertIn(doc_line, watch.WATCHER_DOC_LINES,
                     f"Muse line should be in WATCHER_DOC_LINES")
        
        # Test that keep returns False for this doc line
        self.assertFalse(watch.keep(doc_line), 
                        f"keep should return False for doc line: {doc_line}")
        
        # Restore original state
        watch.WATCHER_DOC_LINES.clear()
        watch.WATCHER_DOC_LINES.update(original_doc_lines)

    def test_keep_true_for_real_error_line_not_in_docs(self):
        """Test that keep returns True for a real error line that is not in docs."""
        # This is a real error line that should be kept
        real_error_line = "fatal: membership or permission error from the API"
        
        # We need to ensure this line is NOT in WATCHER_DOC_LINES by loading doc lines
        doc_lines = load_doc_lines()
        original_doc_lines = watch.WATCHER_DOC_LINES.copy()
        try:
            watch.WATCHER_DOC_LINES.clear()
            watch.WATCHER_DOC_LINES.update(doc_lines)
            
            # Test that keep returns True for this real error line
            self.assertTrue(watch.keep(real_error_line),
                           f"keep should return True for real error line: {real_error_line}")
        finally:
            watch.WATCHER_DOC_LINES.clear()
            watch.WATCHER_DOC_LINES.update(original_doc_lines)

    def test_keep_false_for_exact_doc_line_match(self):
        """Test that any exact match to a doc line returns False."""
        # Create a temporary docs directory with a file containing a line
        tmp = tempfile.TemporaryDirectory()
        try:
            docs_dir = os.path.join(tmp.name, "docs")
            os.makedirs(docs_dir)
            
            test_doc_file = os.path.join(docs_dir, "test.md")
            with open(test_doc_file, "w", encoding="utf-8") as f:
                f.write("This is a test line from the docs\n")
                f.write("Another test line\n")
                f.write("a line with error in it\n")  # This should still be False if exact match
                f.write("\n")
            
            # Load doc lines using the watcher's internal mechanism
            doc_lines = set()
            for root, _, files in os.walk(docs_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if not file_path.lower().endswith((".md", ".txt")):
                        continue
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            stripped = line.rstrip("\r\n")
                            if stripped:
                                doc_lines.add(stripped)
            
            # The WATCHER_DOC_LINES should be updated with these doc lines
            original_doc_lines = watch.WATCHER_DOC_LINES.copy()
            try:
                watch.WATCHER_DOC_LINES.update(doc_lines)
                
                # Test that lines from docs are filtered out
                for line in doc_lines:
                    self.assertFalse(watch.keep(line), 
                                    f"keep should return False for exact doc line: {line}")
                
                # Test that a real error line is not affected
                real_error_line = "fatal: membership or permission error from the API"
                self.assertTrue(watch.keep(real_error_line),
                               f"keep should return True for real error line: {real_error_line}")
            finally:
                watch.WATCHER_DOC_LINES.clear()
                watch.WATCHER_DOC_LINES.update(original_doc_lines)
        finally:
            tmp.cleanup()

    def test_keep_true_for_error_that_shares_words_with_doc_line(self):
        """Test that a line that shares words with a doc line but isn't an exact match is kept."""
        # This line is similar to the MUSE.md line but not an exact match
        # It contains "error" and "permission" which are words from the doc line
        # but is a real error line, so it should be kept
        similar_line = "fatal: membership or permission error from the API"
        
        # We need to ensure this line is NOT in WATCHER_DOC_LINES by loading doc lines
        doc_lines = load_doc_lines()
        original_doc_lines = watch.WATCHER_DOC_LINES.copy()
        try:
            watch.WATCHER_DOC_LINES.clear()
            watch.WATCHER_DOC_LINES.update(doc_lines)
            
            # Test that keep returns True for this similar line
            self.assertTrue(watch.keep(similar_line),
                           f"keep should return True for similar line: {similar_line}")
        finally:
            watch.WATCHER_DOC_LINES.clear()
            watch.WATCHER_DOC_LINES.update(original_doc_lines)