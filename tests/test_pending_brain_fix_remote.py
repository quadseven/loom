import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

from bus import brain, tickets  # noqa: E402

REPO = "example-owner/example-repo"
AUTHORS = ["alice-example"]


class TestPendingBrainFixRemote(unittest.TestCase):
    def test_remote_claim_clears_brain_fix(self):
        comments = [
            {"user": {"login": "alice-example"},
             "created_at": "2026-11-03T02:53:00Z",
             "body": "<!-- loom:fix v1 -->\n<!-- loom:brain-fix v1 --> criteria not ticked"},
            {"user": {"login": "alice-example"},
             "created_at": "2026-11-03T04:01:00Z",
             "body": "<!-- loom:remote-claim v1 muse 2531 1000.000 -->"},
        ]
        self.assertFalse(brain._pending_brain_fix(comments, AUTHORS))

    def test_brain_fix_naming_merged_pr_does_not_block_other_pr(self):
        comments = [
            {"user": {"login": "alice-example"},
             "created_at": "2026-11-03T02:53:00Z",
             "body": ("<!-- loom:fix v1 -->\n"
                      "Pull request: %s#10, branch loom/10-x\n"
                      "<!-- loom:brain-fix v1 --> criteria not ticked" % REPO)},
        ]
        def state_of(repo, number):
            if number == 10:
                return ("closed", True)
            return ("open", False)
        self.assertFalse(brain._pending_brain_fix(
            comments, AUTHORS, merging=(REPO, 20), state_of=state_of))

    def test_brain_fix_naming_merged_pr_still_blocks_when_no_state_lookup(self):
        comments = [
            {"user": {"login": "alice-example"},
             "created_at": "2026-11-03T02:53:00Z",
             "body": ("<!-- loom:fix v1 -->\n"
                      "Pull request: %s#10, branch loom/10-x\n"
                      "<!-- loom:brain-fix v1 --> criteria not ticked" % REPO)},
        ]
        # no merging/state => keeps today's behaviour (blocks)
        self.assertTrue(brain._pending_brain_fix(comments, AUTHORS))

    def test_brain_fix_naming_pr_being_merged_still_blocks(self):
        comments = [
            {"user": {"login": "alice-example"},
             "created_at": "2026-11-03T02:53:00Z",
             "body": ("<!-- loom:fix v1 -->\n"
                      "Pull request: %s#20, branch loom/20-x\n"
                      "<!-- loom:brain-fix v1 --> criteria not ticked" % REPO)},
        ]
        def state_of(repo, number):
            return ("open", False)
        self.assertTrue(brain._pending_brain_fix(
            comments, AUTHORS, merging=(REPO, 20), state_of=state_of))


if __name__ == "__main__":
    unittest.main()
