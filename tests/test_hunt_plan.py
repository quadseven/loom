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
        self.assertTrue(any("non-empty bullets" in p for p in brain.plan_problems(thin)))
        self.assertTrue(brain.plan_problems(BRIEF % "XL"))
        self.assertEqual(brain.plan_problems(BRIEF % "Medium"), [])


if __name__ == "__main__":
    unittest.main()
