"""A checked live box whose output is not in the run log is unticked."""
import unittest

from bus import live_proof

BODY = """## Summary

Change.

## Acceptance criteria

- [x] A unit test covers the parser.
- [x] Live: the next status read prints `proof due: 3`.
- [ ] Another box.
"""


class UntickLiveBoxTest(unittest.TestCase):
    def test_a_box_missing_its_output_is_unticked_with_a_note(self):
        got = live_proof.untick_unproven_live(BODY, ["nothing relevant\n"])
        self.assertIn("- [ ] Live: the next status read", got)
        self.assertIn("- [x] A unit test covers the parser.", got)
        self.assertIn("criterion 2 unticked", got)

    def test_a_box_with_its_output_in_the_log_stays_checked(self):
        got = live_proof.untick_unproven_live(BODY, ["proof due: 3\n"])
        self.assertEqual(got, BODY)

    def test_a_body_with_no_live_box_is_unchanged(self):
        body = BODY.replace("- [x] Live: the next status read prints `proof due: 3`.\n", "")
        self.assertEqual(live_proof.untick_unproven_live(body, []), body)


if __name__ == "__main__":
    unittest.main()
