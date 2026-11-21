"""An escaped run-log tail on a station line does not hide its outcome."""
from __future__ import annotations

import unittest

from bus import watch

OUTCOME = ("station: reported example-owner/repo-a#12 failed: [work] validation "
           "failed: the check exited 1 at 3e8c214fcd36 (base check exited 0); "
           "the edits are on branch main; no pull request opened")
TAIL = ("\\n[2026-11-18T10:11:18Z] example.repo-a-12-r6 agent gh calls: 0"
        "\\n```json\\n{\"ok\": false}")


class EscapedTailTest(unittest.TestCase):
    def test_designed_outcome_with_a_tail_is_not_kept_but_errors_are(self):
        self.assertFalse(watch.keep(OUTCOME))
        self.assertFalse(watch.keep(OUTCOME + TAIL))
        self.assertFalse(watch.keep("[2026-11-18T10:16:58Z] " + OUTCOME + TAIL))
        self.assertTrue(watch.keep("Traceback (most recent call last):" + TAIL))


if __name__ == "__main__":
    unittest.main()
