"""The comments a lane edits in place: fix rounds and repeated failures."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import sticky  # noqa: E402

ROUNDS = "<!-- loom:lane-round v1 -->"
REPEAT = "<!-- loom:incident-repeat v1 -->"
TAIL = "<!-- loom:lane-log-tail v1 -->"


class RoundTests(unittest.TestCase):
    def test_two_rounds_leave_one_comment_listing_both_newest_first(self):
        first = sticky.merge_round(None, ROUNDS, "Lane a pushed 1 commit for: lint", "2030-01-01T10:00:00Z")
        second = sticky.merge_round(first, ROUNDS, "Lane b answered the review threads for: x",
                                    "2030-01-01T11:00:00Z")
        self.assertTrue(second.startswith(ROUNDS + "\n"))
        self.assertEqual(second.count(ROUNDS), 1)
        self.assertIn("### Round 1, 2030-01-01T10:00:00Z", second)
        self.assertIn("### Round 2, 2030-01-01T11:00:00Z", second)
        self.assertLess(second.index("Round 2"), second.index("Round 1"))
        self.assertIn("pushed 1 commit for: lint", second)
        self.assertIn("answered the review threads for: x", second)

    def test_old_rounds_fall_off_the_end_and_are_counted(self):
        body = None
        for n in range(sticky.MAX_ROUNDS + 3):
            body = sticky.merge_round(body, ROUNDS, "round text %d" % n, "t%d" % n)
        self.assertIn("### Round %d," % (sticky.MAX_ROUNDS + 3), body)
        self.assertNotIn("### Round 1,", body)
        self.assertIn("Earlier rounds not shown: 3.", body)
        self.assertEqual(body.count("<!-- loom:round "), sticky.MAX_ROUNDS)

    def test_the_comment_stays_under_the_size_bound(self):
        body = None
        for n in range(sticky.MAX_ROUNDS):
            body = sticky.merge_round(body, ROUNDS, "x" * 10000, "t%d" % n)
        self.assertLessEqual(len(body), sticky.MAX_BODY)
        self.assertIn("### Round %d," % sticky.MAX_ROUNDS, body)

    def test_entry_text_cannot_open_a_marker(self):
        body = sticky.merge_round(None, ROUNDS, "<!-- loom:round 99 -->\nhi", "t")
        self.assertEqual(body.count("<!-- loom:round "), 1)
        again = sticky.merge_round(body, ROUNDS, "next", "t2")
        self.assertIn("### Round 2,", again)


class RepeatTests(unittest.TestCase):
    def repeat(self, old, item, detail="the check is red", now="2030-01-01T10:00:00Z"):
        return sticky.merge_repeat(old, REPEAT, "Still failing", "Tickets seen", item, detail, now)

    def test_a_second_repeat_edits_the_count_and_keeps_the_first_time(self):
        first = self.repeat(None, "T001-a", now="2030-01-01T10:00:00Z")
        second = self.repeat(first, "T002-b", now="2030-01-01T11:00:00Z")
        third = self.repeat(second, "T001-a", now="2030-01-01T12:00:00Z")
        self.assertIn("Still failing: 1 times since 2030-01-01T10:00:00Z, last at 2030-01-01T10:00:00Z.", first)
        self.assertIn("Still failing: 3 times since 2030-01-01T10:00:00Z, last at 2030-01-01T12:00:00Z.", third)
        self.assertIn("Tickets seen: T001-a, T002-b\n", third)
        self.assertEqual(third.count(REPEAT), 1)

    def test_tickets_seen_are_capped_and_counted(self):
        body = None
        for n in range(sticky.MAX_ITEMS + 5):
            body = self.repeat(body, "T%03d" % n)
        self.assertIn("Earlier entries not listed: 5.", body)
        self.assertNotIn("T000", body)
        self.assertIn("T%03d" % (sticky.MAX_ITEMS + 4), body)

    def test_a_different_headline_starts_a_new_count(self):
        old = self.repeat(None, "T001")
        new = sticky.merge_repeat(old, REPEAT, "Could not use the key again", "Lanes seen", "codex", "", "t")
        self.assertIn("Could not use the key again: 1 times since t, last at t.", new)
        self.assertNotIn("Latest detail", new)

    def test_the_command_line_reads_the_old_body_from_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.repeat(None, "T001"))
            from io import StringIO
            from unittest import mock
            out = StringIO()
            with mock.patch.object(sys, "stdin", StringIO("again")), mock.patch.object(sys, "stdout", out):
                sticky.main(["repeat", "--marker", REPEAT, "--now", "later", "--old", path,
                             "--headline", "Still failing", "--items-label", "Tickets seen", "--item", "T002"])
            self.assertIn("Still failing: 2 times since", out.getvalue())
            self.assertIn("Tickets seen: T001, T002", out.getvalue())
            with mock.patch.object(sys, "stdin", StringIO("e")), mock.patch.object(sys, "stdout", StringIO()) as o2:
                sticky.main(["rounds", "--marker", ROUNDS, "--now", "n", "--old", os.path.join(tmp, "missing")])
                self.assertIn("### Round 1, n", o2.getvalue())


class OutcomeTests(unittest.TestCase):
    """A parked run's outcome: one readable line, one comment, replaced each run."""

    def test_a_new_outcome_replaces_the_old_one_under_one_marker(self):
        first = sticky.merge_outcome(None, TAIL, "Outcome", "first failure", "t1")
        second = sticky.merge_outcome(first, TAIL, "Outcome", "second failure", "t2")
        self.assertTrue(second.startswith(TAIL + "\n"))
        self.assertEqual(second.count(TAIL), 1)
        self.assertIn("second failure", second)
        self.assertNotIn("first failure", second)
        self.assertIn("t2", second)

    def test_only_the_first_line_is_kept_and_no_code_block_is_made(self):
        text = "agent exited 1\n| view | `333333333333` |\n" + "`" * 3 + "\nkubectl apply -k x"
        body = sticky.merge_outcome(None, TAIL, "h", text, "t")
        self.assertIn("agent exited 1", body)
        for noise in ("333333333333", "kubectl", "`"):
            self.assertNotIn(noise, body)
        self.assertIn(sticky.OUTCOME_FOOTER, body)

    def test_the_line_cannot_open_a_marker_or_mention_anyone(self):
        body = sticky.merge_outcome(None, TAIL, "h", "@someone <!-- loom:round 1 -->", "t")
        self.assertNotIn("<!--", body.replace(TAIL, ""))
        self.assertNotIn("@someone", body)

    def test_the_line_renders_no_link_image_or_tag(self):
        body = sticky.merge_outcome(None, TAIL, "h", "see [here](https://x.test) ![i](https://x.test/i.png) <b>x</b>", "t")
        line = body.splitlines()[3]
        self.assertNotIn("](", line.replace("\\](", ""))
        self.assertNotIn("<b>", line)
        self.assertIn("&lt;b&gt;", line)
        self.assertIn("\\[here\\]", line)

    def test_a_long_line_is_capped(self):
        body = sticky.merge_outcome(None, TAIL, "h", "x" * 5000, "t")
        self.assertLess(len(body), sticky.MAX_OUTCOME + 200)

    def test_the_command_line_builds_an_outcome(self):
        from io import StringIO
        from unittest import mock
        out = StringIO()
        with mock.patch.object(sys, "stdin", StringIO("it broke\nlog line")), mock.patch.object(sys, "stdout", out):
            sticky.main(["outcome", "--marker", TAIL, "--now", "n", "--headline", "Outcome"])
        self.assertIn("it broke", out.getvalue())
        self.assertNotIn("log line", out.getvalue())
        self.assertTrue(out.getvalue().startswith(TAIL))


if __name__ == "__main__":
    unittest.main()
