from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import watch  # noqa: E402


REF = "owner/repo" + "#" + "3181"


class LeftRunningBackstopTest(unittest.TestCase):
    def test_the_never_left_running_backstop_is_not_an_error(self):
        line = ("station: moved %s to loom:failed: " % REF +
                "its reported run left it running")
        self.assertFalse(watch.keep(line))

    def test_a_reported_work_failure_is_still_kept(self):
        line = ("station: reported %s failed: [work] validation failed: " % REF +
                "the check exited 1 at 3a747815f1f3 (base check exited 1); "
                "the edits are on branch loom/3181-x; no pull request opened")
        self.assertTrue(watch.keep(line))

    def test_another_move_to_failed_is_still_kept(self):
        line = "station: moved %s to loom:failed: its lane vanished" % REF
        self.assertTrue(watch.keep(line))


if __name__ == "__main__":
    unittest.main()
