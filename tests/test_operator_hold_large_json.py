"""A large generated JSON file GitHub sends no patch for is not a placement
change when its own content is no deploy manifest (bus/operator_hold.py)."""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import operator_hold  # noqa: E402

REPO = "example/repo"
PR = {"number": 7, "base": {"ref": "main", "sha": "b" * 40}, "head": {"sha": "a" * 40}}


class Reply:
    def __init__(self, data):
        self.data = data


class ContentsGitHub:
    """Answers contents reads by path and ref; records them."""

    def __init__(self, files):
        self.files = files
        self.reads = []

    def rest(self, method, path, body=None, ok=()):
        self.reads.append(path)
        name = path.split("/contents/")[1].split("?")[0]
        text = self.files.get(name)
        if text is None:
            return Reply(None)
        return Reply({"content": base64.b64encode(text.encode()).decode()})


class LargeJsonTest(unittest.TestCase):
    def test_a_removed_json_that_is_no_manifest_is_not_a_placement_file(self):
        gh = ContentsGitHub({"out/graph.json": '{"nodes": [], "links": []}'})
        entries = [{"filename": "out/graph.json", "status": "removed", "patch": None}]
        moved = operator_hold.placement_files(gh, REPO, PR, entries)
        self.assertEqual(moved, [])
        # A removed file is read at the base commit.
        self.assertIn("ref=" + "b" * 40, gh.reads[0])


if __name__ == "__main__":
    unittest.main()
