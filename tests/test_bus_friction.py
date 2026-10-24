"""Journal friction lines and their fingerprints (bus/friction.py)."""
import unittest

from bus.friction import fingerprint, friction_lines, jaccard, words


class FrictionHeadingsTest(unittest.TestCase):
    def test_each_heading_the_journals_use_is_read(self):
        body = "\n".join([
            "- Friction: one",
            "- What slowed the factory: two",
            "What slowed the factory down this tick: three",
            "- Slowed the factory: four",
            "- Factory note: five",
            "Factory notes (what slowed the factory this tick): six",
            "- Factory note for the next brain: seven",
            "- Factory friction: eight",
            "- Factory observation: nine",
            "- Improving Loom itself: ten",
            "- Factory: eleven",
            "**Blockers:** twelve",
            "What slowed the factory down (for the next loom#30 brief, not briefed: later): thirteen",
            "Factory note (per docs/MUSE.md, Improving Loom itself): fourteen",
        ])
        self.assertEqual(friction_lines(body), [
            "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
            "ten", "eleven", "twelve", "thirteen", "fourteen"])

    def test_status_lines_are_not_friction(self):
        body = "\n".join([
            "- Factory health: 11 tickets marked repeatedly stuck.",
            "- Moved: merged 1",
            "- Asks: none",
            "- Reports: next due 10:00Z.",
            "- Shipped: example-owner/sample#3 at 10:05Z",
        ])
        self.assertEqual(friction_lines(body), [])

    def test_none_and_nothing_new_are_skipped(self):
        body = "\n".join([
            "- Friction: none",
            "- What slowed the factory: nothing new observed this tick.",
            "- Factory note: None.",
            "- Improving Loom itself: none of the lanes ran the verify step",
        ])
        self.assertEqual(friction_lines(body), ["none of the lanes ran the verify step"])

    def test_a_heading_line_takes_the_list_under_it(self):
        body = "\n".join([
            "What slowed the factory down (for the next loom#30 brief):",
            "- A leak-scan hit cannot be fixed by a lane.",
            "- The ladder re-ran a ticket whose work was on main.",
            "",
            "- Reports: none owed.",
        ])
        self.assertEqual(friction_lines(body), [
            "A leak-scan hit cannot be fixed by a lane.",
            "The ladder re-ran a ticket whose work was on main."])

    def test_a_bulleted_heading_takes_only_its_nested_items(self):
        body = "\n".join([
            "- Factory notes this tick:",
            "  1. Status goes stale between two brains.",
            "  2. Resume is the slowest step.",
            "- Reports: none owed.",
        ])
        self.assertEqual(friction_lines(body), [
            "Status goes stale between two brains.", "Resume is the slowest step."])

    def test_hard_wrapped_lines_are_joined(self):
        body = "\n".join([
            "Factory notes: 38 lane PRs are open across the three repos and none is",
            "mergeable for the second tick; the common cause is unresolved",
            "review threads.",
            "Cap+8 live-regression rule in effect.",
        ])
        self.assertEqual(friction_lines(body), [
            "38 lane PRs are open across the three repos and none is mergeable for the "
            "second tick; the common cause is unresolved review threads."])


class FingerprintTest(unittest.TestCase):
    def test_short_words_case_and_order_do_not_change_the_key(self):
        one = fingerprint("Resume is the slowest step of the tick.")
        two = fingerprint("the TICK: slowest step, resume")
        self.assertEqual(one, two)
        self.assertEqual(one[0], frozenset({"resume", "slowest", "step", "tick"}))
        self.assertRegex(one[1], r"^[0-9a-f]{40}$")

    def test_different_words_are_a_different_key(self):
        self.assertNotEqual(fingerprint("resume is slow")[1], fingerprint("usage is unreadable")[1])

    def test_jaccard_is_shared_over_all_words(self):
        self.assertEqual(jaccard(words("resume takes seven minutes"),
                                 words("resume takes eight minutes")), 0.6)
        self.assertEqual(jaccard([], []), 0.0)


if __name__ == "__main__":
    unittest.main()
