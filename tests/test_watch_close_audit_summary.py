from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import watch  # noqa: E402


class CloseAuditSummaryTest(unittest.TestCase):
    def test_summary_with_no_refusals_is_not_kept(self):
        line = ("station: close audit: 6 closes read, 0 refused, 34 waiting; "
                "at most 80 reads a pass")
        self.assertFalse(watch.keep(line))

    def test_summary_with_refusals_is_not_kept(self):
        line = ("station: close audit: 6 closes read, 2 refused, 34 waiting; "
                "at most 80 reads a pass")
        self.assertFalse(watch.keep(line))


if __name__ == "__main__":
    unittest.main()
