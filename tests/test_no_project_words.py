"""Loom is a public, reusable factory: no file in it carries words from one
operator's own project (loom#674). Neutral examples (checkout, search,
billing) stand in for them in docs and fixtures."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Words that only make sense in one game project. bus/metrics.py keeps its
# own copy of some as the generic-code metric, so it is the one exception.
PROJECT_WORDS = re.compile(
    r"\b(worldserver|warcraft|playerbots?|raids?|dungeons?|loot|guilds?|realms?|"
    r"quests?|party-trip|durability|full bags)\b", re.I)
ALLOWED = {"bus/metrics.py", "tests/test_no_project_words.py"}


class NoProjectWords(unittest.TestCase):
    def test_no_tracked_file_carries_a_project_word(self):
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                                check=True).stdout.decode().split("\0")
        hits = []
        for rel in listed:
            if not rel or rel in ALLOWED:
                continue
            try:
                text = (ROOT / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                m = PROJECT_WORDS.search(line)
                if m:
                    hits.append("%s:%d: %s" % (rel, number, m.group(0)))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
