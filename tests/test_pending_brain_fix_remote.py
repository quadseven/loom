"""Regressions for remote claims and completed brain-fix pull requests."""
import unittest

from bus import brain


AUTHOR = "codex-example"
LANE = "lane-example"


def fix_comment(number=41, created="2030-01-01T00:00:00Z"):
    return {
        "user": {"login": AUTHOR},
        "created_at": created,
        "body": ("<!-- loom:fix v1 -->\nPull request: %s#%d, branch fix\n"
                 "<!-- loom:brain-fix v1 --> update the requested change") % (
                     "example/repo", number),
    }


class PendingBrainFixRemoteTests(unittest.TestCase):
    def test_newer_trusted_remote_claim_clears_pending_fix(self):
        comments = [fix_comment(), {
            "user": {"login": LANE},
            "created_at": "2030-01-01T00:01:00Z",
            "body": "<!-- loom:remote-claim v1 muse 9 1893456060.000 -->",
        }]

        self.assertFalse(brain._pending_brain_fix(comments, [AUTHOR, LANE]))

    def test_merged_fix_pr_does_not_block_other_pr(self):
        comments = [fix_comment(41)]

        pending = brain._pending_brain_fix(
            comments, [AUTHOR], current_pr=("example/repo", 42),
            pr_state=lambda repo, number: {"state": "closed", "merged_at": "2030-01-02"},
        )

        self.assertFalse(pending)

    def test_fix_pr_being_merged_keeps_blocking(self):
        comments = [fix_comment(42)]

        pending = brain._pending_brain_fix(
            comments, [AUTHOR], current_pr=("example/repo", 42),
            pr_state=lambda repo, number: self.fail("same pull request needs no lookup"),
        )

        self.assertTrue(pending)


if __name__ == "__main__":
    unittest.main()
