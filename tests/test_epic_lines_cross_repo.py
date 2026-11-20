from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import brain  # noqa: E402

HOME = "owner/home"
OTHER = "owner/other"


def epic(number, repo):
    return {"number": number, "title": "Epic: x", "state": "open",
            "repository_url": "https://api.github.com/repos/" + repo}


class EpicLinesCrossRepoTest(unittest.TestCase):
    def test_an_epic_of_another_repository_prints_with_its_repository(self):
        lines = brain.epic_lines([(epic(11, HOME), []), (epic(12, OTHER), [])], HOME)
        self.assertIn("  - epic #11 Epic: x - tickets 0/%d" % brain.EPIC_TICKET_CAP, lines)
        self.assertIn("  - epic %s#12 Epic: x - tickets 0/%d"
                      % (OTHER, brain.EPIC_TICKET_CAP), lines)

    def test_an_epic_with_no_repository_read_stays_unqualified(self):
        lines = brain.epic_lines([({"number": 5, "title": "Epic: y"}, [])], HOME)
        self.assertTrue(any(line.startswith("  - epic #5 ") for line in lines))


if __name__ == "__main__":
    unittest.main()
