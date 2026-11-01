"""A ticket refused on one class again and again stops climbing the ladder."""
import unittest

from bus import refusals

LEAK = "- 2026-10-31T08:46:34Z Lane claude failed: [work] public repo leak scan failed; nothing pushed"
LEAK_CHECK = "- Check `leak-scan / Scan diff and PR/issue text for private identifiers` failed"
TRUSTED = {"station"}
TANGLED = ("<!-- loom:fix v1 -->\nFunction `run` too tangled -- cyclomatic 25 (cap 15)\n"
           "### Thread T1 (comment 9) at bus/station.py:%d\n")


def note(body):
    return {"body": body, "user": {"login": "Station"}}


class RepeatedTest(unittest.TestCase):
    def test_two_rounds_of_one_class_is_not_yet_a_repeat(self):
        self.assertIsNone(refusals.repeated([note(LEAK), note(LEAK)], TRUSTED))

    def test_the_third_round_of_a_class_is_a_repeat(self):
        self.assertEqual(refusals.repeated([note(LEAK), note(LEAK_CHECK), note(LEAK)], TRUSTED),
                         refusals.LEAK_SCAN)

    def test_complexity_counts_across_rounds_wherever_the_function_sits(self):
        self.assertEqual(refusals.repeated([note(TANGLED % n) for n in (1, 400, 900)], TRUSTED),
                         refusals.COMPLEXITY)

    def test_review_threads_at_one_file_repeat_across_lines_and_rules(self):
        comments = [note("<!-- loom:fix v1 -->\n### Thread T%d (comment %d) at bus/x.py:%d\n"
                         % (n, n, n * 500)) for n in range(1, 4)]
        self.assertEqual(refusals.repeated(comments, TRUSTED), "review findings at bus/x.py")

    def test_threads_at_different_files_do_not_repeat(self):
        comments = [note("<!-- loom:fix v1 -->\n### Thread T%d (comment %d) at bus/x%d.py:1\n"
                         % (n, n, n)) for n in range(1, 4)]
        self.assertIsNone(refusals.repeated(comments, TRUSTED))

    def test_comments_from_anyone_else_do_not_count(self):
        stranger = {"body": LEAK, "user": {"login": "someone"}}
        self.assertIsNone(refusals.repeated([stranger] * 5, TRUSTED))

    def test_a_ladder_restart_clears_the_count(self):
        comments = [note(LEAK), note(LEAK), note("<!-- loom:rung 0 -->\nthe brief stands"),
                    note(LEAK)]
        self.assertIsNone(refusals.repeated(comments, TRUSTED))


if __name__ == "__main__":
    unittest.main()
