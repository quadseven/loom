"""This repo is public: dispatch code must not publish to it or pin a machine.

#13: the dispatcher used to push state to a branch of this repo every cycle
and load a credential helper from another machine's home directory.
"""

import re
import unittest
from pathlib import Path

DISPATCH = Path(__file__).resolve().parent.parent / "dispatch"

# Writing a ref or a file through the GitHub API, or pushing with git.
WRITES_TO_GITHUB = re.compile(r"git/refs|git/commits|/contents/|git\s+push")
HOME_PATH = re.compile(r"/(?:home|Users)/[a-z][\w.-]*/")


class DispatchHygiene(unittest.TestCase):
    def sources(self):
        files = sorted(DISPATCH.glob("*.py"))
        self.assertTrue(files)
        return {f.name: f.read_text() for f in files}

    def test_no_dispatch_code_writes_to_github(self):
        for name, text in self.sources().items():
            self.assertIsNone(WRITES_TO_GITHUB.search(text), name)

    def test_no_dispatch_code_names_a_home_directory(self):
        for name, text in self.sources().items():
            self.assertIsNone(HOME_PATH.search(text), name)


if __name__ == "__main__":
    unittest.main()
