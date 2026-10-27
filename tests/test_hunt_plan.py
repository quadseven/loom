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

    def test_why_opening_with_issue_reference_is_read_as_text(self):
        # A brief whose Why section opens with an issue reference like
        # "#214 (Routes 2-4) ..." should be read as body text, not as a heading.
        # The plan check must find the words in the Why section.
        brief = """## Why
#214 (Routes 2-4) The page query is slow for every visitor.

## What
Cache the query.

## Acceptance criteria
- [ ] The page loads in under a second.
- [ ] A test fails without the cache.
- [ ] The query runs once a minute.

## Size
Medium

## Out of scope
- Other pages.
"""
        body = pr_repair._render_generated(brief, "lane", "model", "T1")
        self.assertEqual(hunt_plan.problems(body), [])
        why = hunt_plan._section_text(body, "Why", "Summary")
        self.assertIsNotNone(why)
        self.assertGreaterEqual(len(why.split()), 5)


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

    def test_a_size_section_in_markdown_or_with_a_label_reads_as_the_letter(self):
        # A lane pull request in another repo (2026-10-25): its ticket's
        # ## Size section said "**Size:** S", brief_size kept its first
        # word, and the body read
        # "Size: **Size:**", which the plan check held. Every reader goes
        # through size_letter, so each form gives the letter everywhere.
        forms = {"**Size:** S": "S", "Size: S": "S", "**S**": "S", "S (small)": "S",
                 "Small": "S", "M - medium": "M", "**Size: M**": "M", "`L`": "L",
                 "- XS": "XS", "Size - M": "M", "__Size__: L": "L"}
        for line, letter in forms.items():
            with self.subTest(line=line):
                body = BRIEF % line
                self.assertEqual(hunt_plan.size_letter(line), letter)
                self.assertEqual(hunt_plan.size_letter(hunt_plan.stated_size(body)), letter)
                self.assertEqual(hunt_plan.brief_size(body), letter)
                self.assertEqual(pr_repair._brief_size(body), letter)
                self.assertIn("## Size\n\n%s\n" % letter, hunt_plan.with_size_letter(body))
                generated = pr_repair._render_generated(body, "lane", "model", "T1")
                self.assertIn("\nSize: %s\n" % letter, generated)
                self.assertEqual(hunt_plan.problems(generated), [])

    def test_an_xl_written_with_a_label_is_still_too_big(self):
        with self.assertRaises(ValueError):
            hunt_plan.size_letter("**Size:** XL")
        self.assertEqual(hunt_plan.brief_size(BRIEF % "**Size:** XL"), "XL")

    def test_inline_xl_size_is_refused(self):
        # XL on an inline Size: line is still too big: stated_size skips it.
        body = (BRIEF % "S").replace("## Size\n\nS\n", "Size: XL\n")
        self.assertIsNone(hunt_plan.stated_size(body))
        self.assertTrue(brain.plan_problems(body))


if __name__ == "__main__":
    unittest.main()


# The shape of a real triage refusal (loom#2031): an issue whose original
# text was kept under "## Further notes" with every heading demoted one
# level. Its Why is several sentences long, under "### Why".
DEMOTED = """## Status

Status: two criteria met, one needs the operator, one is blocked by it.

Part of #654

## Further notes

Original issue text, kept below unchanged in substance (headings demoted one level).

### Why

The parent issue is verified five of six on main. Its sixth criterion is a
single query against the live store, and it is the only thing keeping the
issue open. Every automated verify pass re-derives the same five met
criteria, writes another comment saying so, and leaves the issue open.

### What

Run the one query against the live store and record it.

### Acceptance criteria

> **Status:** AC1 and AC4 met.

- [x] The value of the setting is recorded in a comment, with the query and its output.
- [ ] If the value is false, the repo is enrolled or a reason is recorded.
- [ ] If the repo is enrolled, the first weekly run covering it is linked here.
- [x] The parent's criterion is ticked and the parent is closed.

Size: XS

### Out of scope

- Any change to the scanner or its corpus.
- Enrolling any other repo.

## Ask

The operator: enable the setting in the dashboard, yes or no.
"""


def _promoted(body):
    """DEMOTED with its section headings written at level two."""
    for name in ("Why", "What", "Acceptance criteria", "Out of scope"):
        body = body.replace("\n### %s\n" % name, "\n## %s\n" % name)
    return body


class DemotedHeadingTest(unittest.TestCase):
    def _refusal(self, body):
        with self.assertRaises(ValueError) as caught:
            brain._check_brief_args("A title", body, None, None, None)
        return str(caught.exception)

    def test_a_why_under_a_demoted_heading_is_named_not_counted_as_zero(self):
        message = self._refusal(DEMOTED)
        self.assertIn("no ## Why heading (found ### Why)", message)
        self.assertNotIn("0 words", message)
        self.assertIn("no ## Acceptance criteria heading (found ### Acceptance criteria)",
                      message)

    def test_a_missing_why_says_so_without_a_found_heading(self):
        message = self._refusal((BRIEF % "S").replace("## Why\n", "## Reason\n"))
        self.assertIn("no ## Why heading", message)
        self.assertNotIn("found", message)
        self.assertNotIn("0 words", message)

    def test_the_demoted_fixture_passes_once_its_headings_are_level_two(self):
        body = _promoted(DEMOTED)
        brain._check_brief_args("A title", body, None, None, None)
        self.assertEqual(brain.plan_problems(body), [])

    def test_why_words_include_text_under_lower_level_subheadings(self):
        brief = (BRIEF % "S").replace(
            "## Why\nThe page query is slow for every visitor.\n",
            "## Why\n### Background\nThe page query is slow for every visitor.\n"
            "#### Detail\nIt runs on every request.\n")
        body = pr_repair._render_generated(brief, "lane", "model", "T1")
        self.assertEqual(hunt_plan.problems(body), [])
        why = hunt_plan._section_text(body, "Why")
        self.assertIn("The page query is slow for every visitor.", why)
        self.assertIn("It runs on every request.", why)
        # The section still ends at the next heading of the same level.
        self.assertNotIn("Cache the query.", why)
