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

    def test_grug_issue_983_body_fixture_passes_triage(self):
        # A sanitized fixture based on the referenced issue body. Keep its
        # section shape and Why wording while generalizing cross-repo links.
        body = """## Why
A prior issue is verified 5-of-6 on the default branch and has been since
early September. Its sixth criterion is a single query against the live
store, and it is the only thing keeping the issue open:

> A recorded answer to whether this repository is enrolled in its own
> hygiene watch, with the query and its output. If it is not, either enroll
> it or record why not - do not leave it ambiguous.

The flag is a per-repository row read by the watch listing query; it is a
database fact, not a tree fact, so no checkout and no CI job can produce it.
The consequence is that every automated verify pass re-derives the same
five met criteria, writes another comment saying so, and leaves the issue
open. The issue has accumulated several such comments for one unchanged
blocker.

## What
Run the one query against the live store and record it. Then either enroll
the repository or write down why not.

## Acceptance criteria
- [ ] Record the flag value with the query and raw output.
- [ ] If disabled, enroll the repository or record why not.
- [ ] Link the first weekly watch run and its finding count.
- [ ] Close the earlier issue when its verification criterion is complete.

Size: XS

## Out of scope
- Changes to the scanner or its parity corpus.
- Enrolling any other repository.
"""
        self.assertEqual(hunt_plan.problems(body), [])

    def test_what_good_look_like_is_not_an_acceptance_heading(self):
        # "What good look like" is the label of the review bot's acceptance
        # check in its report, not a heading that check reads (see the
        # drift test below): a body whose criteria sit under it has no
        # criteria section the gate accepts.
        body = GOOD.replace("## Acceptance criteria", "## What good look like")
        self.assertTrue(any("missing ## Acceptance criteria" in p
                            for p in hunt_plan.problems(body)),
                        hunt_plan.problems(body))

    def test_accepted_acceptance_headings_equal_the_review_bots(self):
        # The drift catcher: Loom's plan check mirrors the review bot's
        # check_acceptance in services/_shared/personas/tpm/dor_checks.py
        # (the grug repo), which reads sections under these two headings
        # (a trailing qualifier on the heading is tolerated there, as
        # here, by the same heading match). Deriving
        # Loom's accepted set through problems(), over the bot's headings
        # plus the near-misses this check has actually produced (the bot's
        # report label for it; the brief-side "Acceptance" alias), and
        # comparing it with the bot's set restated here from that source,
        # fails loudly if an alias creeps back into the plan check.
        chief_headings = {"Acceptance criteria", "Test plan"}
        candidates = chief_headings | {"What good look like", "Acceptance"}
        accepted_headings = set()
        for heading in candidates:
            body = GOOD.replace("## Acceptance criteria", "## " + heading)
            if hunt_plan.problems(body) == []:
                accepted_headings.add(heading)
        self.assertEqual(accepted_headings, chief_headings)

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

    def test_demoted_why_heading_is_named_instead_of_counted_as_zero(self):
        body = """### Why
This is a substantial explanation for the requested change.

## Acceptance criteria
- [ ] First criterion.
- [ ] Second criterion.
- [ ] Third criterion.

Size: S

## Out of scope
- Other changes.
"""
        problems = hunt_plan.problems(body)
        self.assertTrue(any("no ## Why heading (found ### Why)" in problem
                            for problem in problems), problems)
        self.assertFalse(any("0 words" in problem for problem in problems), problems)

    def test_why_words_include_lower_level_subheadings(self):
        body = """## Why
Short intro.

### Further notes
These nested details explain the issue and make the section long enough.

## Summary
Cache the query.
"""
        why = hunt_plan._section_text(body, "Why")
        self.assertIsNotNone(why)
        self.assertGreaterEqual(len(why.split()), 5)
        self.assertNotIn("Cache the query.", why)

    def test_reported_body_shape_names_the_nested_why_heading(self):
        body = """## Further notes
### Why
The current behavior loses useful context across several related steps.
The explanation remains here as multiple paragraphs under the nested heading.

## What
Keep the context available to triage.
"""
        problems = hunt_plan.problems(body)
        self.assertTrue(any("no ## Why heading (found ### Why)" in problem
                            for problem in problems), problems)
        self.assertFalse(any("0 words" in problem for problem in problems), problems)


class BriefSectionTest(unittest.TestCase):
    def test_brief_section_keeps_nested_subheadings_until_next_level_two(self):
        brief = """## Why
Short intro.

### Further notes
These nested details belong to the Why section.

## What
Cache the query.
"""
        self.assertEqual(
            pr_repair._brief_section(brief, "Why"),
            "Short intro.\n### Further notes\nThese nested details belong to the Why section.")


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
        self.assertEqual(padded, "")
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
