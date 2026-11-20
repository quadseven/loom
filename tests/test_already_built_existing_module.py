"""bus/brief_check.py: a criterion that adds a test to an existing module is
not a request to add a file, so it never reads as already built."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import brief_check  # noqa: E402

REPO = "example-owner/example-app"


class AnyFileExists:
    """A GitHub stand-in on which every contents read finds a file."""

    def rest(self, method, path, *args, **kwargs):
        return {"type": "file"}


def body(criterion):
    return "## Acceptance criteria\n\n- [ ] %s\n\n## Size\n\nXS\n" % criterion


class AlreadyBuiltExistingModule(unittest.TestCase):
    def built(self, criterion):
        return brief_check.already_built(AnyFileExists(), REPO, body(criterion), "main")

    def test_a_new_test_in_an_existing_module_is_not_stale(self):
        self.assertIsNone(self.built(
            "A new test in `tests/test_example.py` shows the change."))

    def test_a_test_added_to_an_existing_module_is_not_stale(self):
        self.assertIsNone(self.built(
            "Adds a test to `tests/test_example.py` for the change."))

    def test_a_new_test_module_that_exists_is_stale(self):
        got = self.built("A new test module `tests/test_x.py` shows the change.")
        self.assertEqual(got.kind, brief_check.STALE)
        self.assertIn("`tests/test_x.py`", got.reason)

    def test_adds_a_file_that_exists_is_stale(self):
        got = self.built("Adds `docs/X.md` with the rules.")
        self.assertEqual(got.kind, brief_check.STALE)


if __name__ == "__main__":
    unittest.main()
