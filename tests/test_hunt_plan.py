"""The Hunt Plan checks Loom runs on every brief and generated PR body."""
from __future__ import annotations

import unittest

from bus import brain, hunt_plan, pr_repair

GOOD = """## Why
The page query is slow for every visitor.

## Summary
Cache the query.

## Acceptance criteria
- [ ] The page loads in under a second.
- [ ] A test fails without the cache.
- [ ] The query runs once a minute.

Size: M

## Out of scope
- Other pages.
"""

BRIEF = """## Why
The page query is slow for every visitor.

## What
Cache the query.

## Acceptance criteria
- [ ] The page loads in under a second.
- [ ] A test fails without the cache.
- [ ] The query runs once a minute.

## Size

%s

## Out of scope
- Other pages.
"""


class SizeTest(unittest.TestCase):
    def test_words_and_letters_become_the_gate_letter(self):
        cases = {"Medium": "M", "m": "M", "M": "M", "Small": "S", "S: one function.": "S",
                 "Size: Large": "L", "Medium (two files)": "M", "extra small": "XS",
                 "XS": "XS", "**L**": "L", "large.": "L"}
        for text, letter in cases.items():
            with self.subTest(text=text):
                self.assertEqual(hunt_plan.size_letter(text), letter)

    def test_xl_and_unknown_sizes_are_refused(self):
        for text in ("XL", "Extra large", "huge", "big", "", "5 points"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                hunt_plan.size_letter(text)


class ProblemsTest(unittest.TestCase):
    def test_a_good_body_passes(self):
        self.assertEqual(hunt_plan.problems(GOOD), [])

    def test_each_blocking_check_is_named(self):
        cases = {
            "## Why has 2 words": GOOD.replace("The page query is slow for every visitor.", "Too slow."),
            "non-empty bullets": GOOD.replace("- [ ] A test fails without the cache.\n", "- [ ]\n"),
            "no Size": GOOD.replace("Size: M", "Size: Medium"),
            "Size XL": GOOD.replace("Size: M", "Size: XL"),
            "Out of scope is empty": GOOD.replace("- Other pages.\n", ""),
            "missing ## Why": GOOD.replace("## Why", "## Reason").replace("## Summary", "## What"),
        }
        for expected, body in cases.items():
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in p for p in hunt_plan.problems(body)),
                                hunt_plan.problems(body))


class PadCriteriaTest(unittest.TestCase):
    """The gate counts three non-empty bullets under Acceptance criteria. A
    ticket may state fewer: the factory files a follow-up ticket with the one
    criterion a lane left not built, and a body the gate refuses holds the
    pull request until the plan-check invariant fires."""

    def test_a_section_with_the_bullets_the_gate_wants_is_left_alone(self):
        section = "- [ ] One\n- [ ] Two\n- [ ] Three"
        self.assertEqual(hunt_plan.pad_criteria(section), section)

    def test_a_thin_section_gets_the_standard_bullets(self):
        padded = hunt_plan.pad_criteria("- [ ] One")
        self.assertEqual(len(hunt_plan.bullets(padded)), hunt_plan.MIN_BULLETS)
        self.assertTrue(padded.startswith("- [ ] One"))
        for filler in hunt_plan.FILLER_BULLETS:
            self.assertIn(filler, padded)

    def test_a_section_with_no_criterion_keeps_the_gate_s_refusal(self):
        # A body with nothing to build is not a body to merge: the gate
        # refuses it, and so does the check that mirrors the gate.
        padded = hunt_plan.pad_criteria("")
        self.assertEqual(len(hunt_plan.bullets(padded)), len(hunt_plan.FILLER_BULLETS))
        body = GOOD.replace("- [ ] The page loads in under a second.\n"
                            "- [ ] A test fails without the cache.\n"
                            "- [ ] The query runs once a minute.", padded)
        self.assertTrue(any("non-empty bullets" in p for p in hunt_plan.problems(body)))

    def test_a_padded_body_passes_the_gate(self):
        thin = GOOD.replace("- [ ] A test fails without the cache.\n", "")
        self.assertTrue(any("non-empty bullets" in p for p in hunt_plan.problems(thin)))
        padded = GOOD.replace("- [ ] The page loads in under a second.\n"
                              "- [ ] A test fails without the cache.\n"
                              "- [ ] The query runs once a minute.",
                              hunt_plan.pad_criteria(
                                  "- [ ] The page loads in under a second."))
        self.assertEqual(hunt_plan.problems(padded), [])


class BriefToPullRequestTest(unittest.TestCase):
    def test_a_word_size_brief_yields_a_body_the_gate_accepts(self):
        # The runner's body (pr_repair renders it the same way) from a brief
        # whose Size is a word: the gate accepts only the letter.
        for word in ("Medium", "Small", "Large", "M"):
            with self.subTest(word=word):
                body = pr_repair._render_generated(BRIEF % word, "lane", "model", "T1")
                self.assertEqual(hunt_plan.problems(body), [])

    def test_the_filed_brief_carries_the_letter(self):
        self.assertIn("## Size\n\nM\n", hunt_plan.with_size_letter(BRIEF % "Medium"))

    def test_a_brief_whose_pull_request_would_fail_is_named(self):
        thin = (BRIEF % "S").replace("- [ ] A test fails without the cache.\n", "")
        # A brief with fewer criteria than the gate wants still yields a body
        # it accepts: the body carries the standard bullets.
        self.assertEqual(brain.plan_problems(thin), [])
        self.assertTrue(brain.plan_problems(BRIEF % "XL"))
        self.assertEqual(brain.plan_problems(BRIEF % "Medium"), [])


class OneSizeParserTest(unittest.TestCase):
    """#668: triage, the Hunt Plan gate and PR repair read the size through
    hunt_plan.stated_size, whether it is a ## Size section or a Size: line."""

    def test_heading_and_inline_forms_read_the_same_letter(self):
        heading = BRIEF % "Small"
        for inline in ("Size: S", "size: s", "**Size:** S", "**Size: S**"):
            with self.subTest(inline=inline):
                body = (BRIEF % "S").replace("## Size\n\nS\n", inline + "\n")
                self.assertEqual(hunt_plan.size_letter(hunt_plan.stated_size(heading)), "S")
                self.assertEqual(hunt_plan.size_letter(hunt_plan.stated_size(body)), "S")
                self.assertEqual(pr_repair._brief_size(body), pr_repair._brief_size(heading))
                self.assertEqual(hunt_plan.brief_size(body), "S")
                self.assertEqual(brain.plan_problems(body), [])
                self.assertEqual(brain.missing_sections(body), [])
                generated = pr_repair._render_generated(body, "lane", "model", "T1")
                self.assertIn("\nSize: S\n", generated)
                self.assertEqual(hunt_plan.problems(generated), [])

    def test_no_size_in_either_form_is_none(self):
        body = (BRIEF % "S").replace("## Size\n\nS\n", "")
        self.assertIsNone(hunt_plan.stated_size(body))
        self.assertEqual(hunt_plan.brief_size(body), "")
        # Prose that mentions a size is not a size line.
        self.assertIsNone(hunt_plan.stated_size(body + "\nThe Size: S part is small.\n"))
        self.assertIsNone(hunt_plan.stated_size(body + "\nSize: XL\n"))
        self.assertEqual(hunt_plan.stated_size(BRIEF % ""), "")

    def test_the_heading_wins_over_an_inline_line(self):
        self.assertEqual(hunt_plan.stated_size((BRIEF % "Large") + "\nSize: S\n"), "Large")

    def test_inline_word_size_is_read_as_the_gate_letter(self):
        # A brief that states its size as a word on an inline Size: line
        # (not a ## Size section) still yields a body the gate accepts.
        for word in ("Medium", "Small", "Large", "medium", "small"):
            with self.subTest(word=word):
                body = (BRIEF % "S").replace("## Size\n\nS\n", "Size: %s\n" % word)
                self.assertEqual(hunt_plan.stated_size(body), hunt_plan.size_letter(word))
                self.assertEqual(hunt_plan.brief_size(body), hunt_plan.size_letter(word))
                self.assertEqual(brain.plan_problems(body), [])
                generated = pr_repair._render_generated(body, "lane", "model", "T1")
                self.assertIn("\nSize: %s\n" % hunt_plan.size_letter(word), generated)
                self.assertEqual(hunt_plan.problems(generated), [])

    def test_inline_xl_size_is_refused(self):
        # XL on an inline Size: line is still too big: stated_size skips it.
        body = (BRIEF % "S").replace("## Size\n\nS\n", "Size: XL\n")
        self.assertIsNone(hunt_plan.stated_size(body))
        self.assertTrue(brain.plan_problems(body))


if __name__ == "__main__":
    unittest.main()
