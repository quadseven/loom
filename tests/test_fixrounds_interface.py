import unittest

from bus import fixrounds, tickets


class FixRoundCountTest(unittest.TestCase):
    def test_counts_only_trusted_fix_comments_with_head_markers(self):
        comments = [
            self.comment("trusted", tickets.FIX_MARKER + "\n<!-- loom:fix-head abc -->"),
            self.comment("trusted", tickets.FIX_MARKER),
            self.comment("other", tickets.FIX_MARKER + "\n<!-- loom:fix-head def -->"),
        ]
        self.assertEqual(fixrounds.count(comments, {"trusted"}), 1)

    def test_excludes_cancelled_round_indexes(self):
        comments = [
            self.comment("trusted", tickets.FIX_MARKER + "\n<!-- loom:fix-head abc -->"),
            self.comment("trusted", tickets.FIX_MARKER + "\n<!-- loom:fix-head def -->"),
        ]
        self.assertEqual(fixrounds.count(comments, {"trusted"}, {0}), 1)

    @staticmethod
    def comment(login, body):
        return {"user": {"login": login}, "body": body}


if __name__ == "__main__":
    unittest.main()
