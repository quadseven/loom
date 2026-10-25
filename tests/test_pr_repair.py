from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import pr_repair, text  # noqa: E402


BRIEF = """# Brief: example-owner.example-repo-42

Repo: example-owner/example-repo
Branch: loom/42-fix-the-thing
Lane: codex
Issue: example-owner/example-repo#42

# T42: Fix the thing

## Why
The thing is broken for every user.

## What
Make the thing work.

## Acceptance criteria
- [ ] The thing works
- [ ] A test covers the thing
- [ ] Nothing else changes

## Size
S: one function.

## Out of scope
- Other things
"""

# The body run_lane.sh::pr_body writes for BRIEF. Kept literal so a test that
# starts from it catches drift in _render_generated, rather than building the
# 'current' body with the same function that builds the desired one.
RUNNER_BODY = (
    "## Why\nThe thing is broken for every user.\n\n"
    "## Summary\nMake the thing work.\n\n"
    "## Acceptance criteria\n- [ ] The thing works\n"
    "- [ ] A test covers the thing\n"
    "- [ ] Nothing else changes\n\n"
    "Size: S\n\n"
    "## Out of scope\n- Other things\n\n"
    "## Operator steps before merge\nnone stated\n\n"
    "---\n"
    "Loom ticket T42, lane codex, model gpt-test.\n"
    "Refs example-owner/example-repo#42")

BRIEF_CORRECTED = BRIEF.replace(
    "- [ ] A test covers the thing",
    "- [ ] A test covers the thing\n- [ ] The edge case is handled")


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.brief = os.path.join(self._tmp.name, "brief.md")

    def tearDown(self):
        self._tmp.cleanup()

    def write_brief(self, text):
        with open(self.brief, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_rendered_body_matches_runner_format(self):
        body = pr_repair._render_generated(pr_repair._normalise(BRIEF), "codex", "gpt-test", "T42")
        self.assertEqual(body, RUNNER_BODY)
        self.assertIn("## Why\nThe thing is broken for every user.", body)
        self.assertIn("## Summary\nMake the thing work.", body)
        self.assertIn("## Acceptance criteria\n- [ ] The thing works", body)
        self.assertIn("\nSize: S\n", body)
        self.assertIn("## Out of scope\n- Other things", body)
        self.assertIn("---\nLoom ticket T42, lane codex, model gpt-test.", body)
        self.assertIn("\nRefs example-owner/example-repo#42", body)

    TICKED_BODY = RUNNER_BODY.replace("- [ ] The thing works", "- [x] The thing works").replace(
        "- [ ] Nothing else changes", "- [ ] Nothing else changes; not built: out of reach")

    def test_a_fix_rounds_checklist_ticks_the_body(self):
        # A fix round's ticks never reached its pull request: the repair
        # rendered the ticket's unticked criteria every round (2026-10-20).
        self.write_brief(BRIEF)
        summary = os.path.join(self._tmp.name, "summary.md")
        with open(summary, "w", encoding="utf-8") as fh:
            fh.write("Fix it\n\n## Acceptance criteria\n- [x] The thing works\n"
                     "- [x] A test covers the thing\n- [x] Nothing else changes\n")
        result = pr_repair.reconcile(self.brief, "Fix the thing", RUNNER_BODY,
                                     "codex", "gpt-test", "T42", summary_path=summary)
        self.assertTrue(result["changed"])
        self.assertIn("## Acceptance criteria\n- [x] The thing works\n- [x] A test covers the thing\n"
                      "- [x] Nothing else changes\n", result["body"])

    def test_a_lane_can_tick_but_never_rewrite_a_criterion_or_add_unsafe_text(self):
        self.write_brief(BRIEF)
        summary = os.path.join(self._tmp.name, "summary.md")
        with open(summary, "w", encoding="utf-8") as fh:
            fh.write("## Acceptance criteria\n- [x] The thing works\n"
                     "- [x] A test covers everything, see 192.0.2.3\n"
                     "- [ ] Nothing else changes; not built: see 192.0.2.5\n"
                     "- [x] An extra criterion\n")
        result = pr_repair.reconcile(self.brief, "Fix the thing", RUNNER_BODY,
                                     "codex", "gpt-test", "T42", summary_path=summary)
        self.assertIn("## Acceptance criteria\n- [x] The thing works\n- [ ] A test covers the thing\n"
                      "- [ ] Nothing else changes\n\n", result["body"])
        for unsafe in ("192.0.2.3", "192.0.2.5", "extra"):
            self.assertNotIn(unsafe, result["body"])

    def test_a_plain_not_built_reason_is_kept(self):
        self.write_brief(BRIEF)
        summary = os.path.join(self._tmp.name, "summary.md")
        with open(summary, "w", encoding="utf-8") as fh:
            fh.write("## Acceptance criteria\n- [ ] Nothing else changes; not built: needs the next deploy\n")
        result = pr_repair.reconcile(self.brief, "Fix the thing", RUNNER_BODY,
                                     "codex", "gpt-test", "T42", summary_path=summary)
        self.assertIn("- [ ] Nothing else changes; not built: needs the next deploy\n", result["body"])

    def test_ticks_already_in_the_body_survive_a_round_with_no_checklist(self):
        self.write_brief(BRIEF)
        result = pr_repair.reconcile(self.brief, "Fix the thing", self.TICKED_BODY,
                                     "codex", "gpt-test", "T42")
        self.assertFalse(result["changed"])
        self.assertEqual(result["body"], self.TICKED_BODY)

    def test_a_corrected_ticket_still_replaces_criteria_whose_text_changed(self):
        self.write_brief(BRIEF_CORRECTED)
        result = pr_repair.reconcile(self.brief, "Fix the thing", self.TICKED_BODY,
                                     "codex", "gpt-test", "T42")
        self.assertIn("- [ ] The edge case is handled", result["body"])

    def test_no_change_when_body_and_title_match(self):
        self.write_brief(BRIEF)
        result = pr_repair.reconcile(
            self.brief, "Fix the thing", RUNNER_BODY, "codex", "gpt-test", "T42")
        self.assertFalse(result["changed"])
        self.assertEqual(result["body"], RUNNER_BODY)
        self.assertEqual(result["old_hash"], result["new_hash"])
        self.assertEqual(result["reason"], "")

    def test_updates_generated_sections_and_keeps_reviewer_prefix(self):
        old_body = RUNNER_BODY
        current = "A reviewer note at the top.\n\n" + old_body
        self.write_brief(BRIEF_CORRECTED)
        result = pr_repair.reconcile(self.brief, "Fix the thing", current, "codex", "gpt-test", "T42")
        self.assertTrue(result["changed"])
        self.assertIn("A reviewer note at the top.", result["body"])
        self.assertIn("- [ ] The edge case is handled", result["body"])
        self.assertIn("Loom ticket T42, lane codex, model gpt-test.", result["body"])
        self.assertNotEqual(result["old_hash"], result["new_hash"])
        self.assertEqual(result["reason"], "")

    def test_updates_generated_sections_and_keeps_reviewer_suffix(self):
        old_body = RUNNER_BODY
        current = old_body + "\n\nA reviewer note at the bottom."
        self.write_brief(BRIEF_CORRECTED)
        result = pr_repair.reconcile(self.brief, "Fix the thing", current, "codex", "gpt-test", "T42")
        self.assertTrue(result["changed"])
        self.assertIn("A reviewer note at the bottom.", result["body"])
        self.assertIn("- [ ] The edge case is handled", result["body"])
        # Old generated sections are replaced.
        self.assertNotIn("- [ ] A test covers the thing\n- [ ] Nothing else changes", result["body"])

    def test_updates_title_when_ticket_title_changed(self):
        body = RUNNER_BODY
        corrected = BRIEF.replace("Fix the thing", "Repair the thing")
        self.write_brief(corrected)
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42")
        self.assertTrue(result["changed"])
        self.assertEqual(result["title"], "Repair the thing")

    def test_refuses_body_with_missing_generated_headings(self):
        self.write_brief(BRIEF)
        result = pr_repair.reconcile(self.brief, "Fix the thing", "no headings here", "codex", "gpt-test", "T42")
        self.assertFalse(result["changed"])
        self.assertTrue(result["reason"].startswith("cannot reconcile"))

    def test_a_declared_operator_step_survives_a_repair_round(self):
        # The factory holds a body whose section lists a step
        # (bus/operator_hold.py): a fix round must not reset it to none.
        body = RUNNER_BODY.replace("none stated", "- Grow the VM disk first")
        self.write_brief(BRIEF_CORRECTED)
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42")
        self.assertTrue(result["changed"], result)
        self.assertIn("## Operator steps before merge\n- Grow the VM disk first\n\n---",
                      result["body"])

    def test_a_body_from_before_the_operator_section_is_repaired_to_carry_it(self):
        old = RUNNER_BODY.replace("## Operator steps before merge\nnone stated\n\n", "")
        self.write_brief(BRIEF)
        result = pr_repair.reconcile(self.brief, "Fix the thing", old, "codex", "gpt-test", "T42")
        self.assertEqual(result["reason"], "")
        self.assertEqual(result["body"], RUNNER_BODY)

    def test_refuses_body_with_footer_rule_before_generated_sections(self):
        # A leading rule line is not a footer for the generated sections.
        body = "---\n## Why\nBroken.\n\n## Summary\nFix it.\n\n## Acceptance criteria\n- [ ] Works\n\nSize: S\n\n## Out of scope\n- Nothing"
        self.write_brief(BRIEF)
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42")
        self.assertFalse(result["changed"])
        self.assertIn("footer rule missing", result["reason"])

    def test_refuses_proposed_text_that_mentions_bot(self):
        body = RUNNER_BODY
        bad = BRIEF.replace("every user", "every user @" + text.REVIEW_BOT_SHORT_NAME)
        self.write_brief(bad)
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42")
        self.assertFalse(result["changed"])
        self.assertIn("review bot", result["reason"])

    def test_refuses_proposed_title_that_mentions_bot(self):
        body = RUNNER_BODY
        bad = BRIEF.replace("# T42: Fix the thing", "# T42: ask @" + text.REVIEW_BOT_SHORT_NAME)
        self.write_brief(bad)
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42")
        self.assertFalse(result["changed"])
        self.assertIn("review bot", result["reason"])

    def test_a_local_path_in_the_lanes_summary_reads_path(self):
        # A model wrote its write tool's file path into the summary's
        # operator steps, and every repair of the pull request was refused
        # (infra-5956-r3, 2026-10-25).
        self.write_brief(BRIEF_CORRECTED)
        summary = os.path.join(self._tmp.name, "summary.md")
        with open(summary, "w", encoding="utf-8") as fh:
            fh.write("## Operator steps before merge\nnone\nfilePath\">\n"
                     "/Users/someone/loom/worktrees/T42/.loom/summary.md\n")
        result = pr_repair.reconcile(self.brief, "Fix the thing", RUNNER_BODY,
                                     "codex", "gpt-test", "T42", summary_path=summary)
        self.assertEqual(result["reason"], "")
        self.assertTrue(result["changed"])
        self.assertIn("## Operator steps before merge\nnone\nfilePath\">\n<path>\n", result["body"])
        self.assertNotIn("/Users/", result["body"])

    def test_a_path_the_ticket_names_is_kept(self):
        # The ticket named a server path, the first pull request body carried
        # it, and the repair refused that same text (infra-5819-r3).
        named = BRIEF.replace("Make the thing work.", "Keep the data under `/opt/apps/*`.")
        self.write_brief(named.replace(
            "- [ ] A test covers the thing",
            "- [ ] A test covers the thing\n- [ ] The edge case is handled"))
        body = RUNNER_BODY.replace("Make the thing work.", "Keep the data under `/opt/apps/*`.")
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42")
        self.assertEqual(result["reason"], "")
        self.assertTrue(result["changed"])
        self.assertIn("Keep the data under `/opt/apps/*`.", result["body"])

    def test_refuses_a_path_neither_the_ticket_nor_the_lane_wrote(self):
        self.write_brief(BRIEF_CORRECTED)
        body = RUNNER_BODY.replace("---\n", "Reviewer note: see /home/someone/notes\n\n---\n")
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42")
        self.assertFalse(result["changed"])
        self.assertIn("absolute path", result["reason"])

    def test_refuses_proposed_text_with_closing_keyword(self):
        body = RUNNER_BODY
        bad = BRIEF.replace("Make the thing work.", "Fixes #42.")
        self.write_brief(bad)
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42")
        self.assertFalse(result["changed"])
        self.assertIn("closing keyword", result["reason"])


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.brief = os.path.join(self._tmp.name, "brief.md")
        self.pr_json = os.path.join(self._tmp.name, "pr.json")
        with open(self.brief, "w", encoding="utf-8") as fh:
            fh.write(BRIEF)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cli_outputs_json_result(self):
        body = RUNNER_BODY
        with open(self.pr_json, "w", encoding="utf-8") as fh:
            json.dump({"title": "Fix the thing", "body": body}, fh)
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = pr_repair.main([self.brief, "--pr-json", self.pr_json, "codex", "gpt-test", "T42"])
        self.assertEqual(rc, 0)
        result = json.loads(out.getvalue())
        self.assertFalse(result["changed"])

    def test_cli_reports_missing_pr_json(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = pr_repair.main([self.brief, "--pr-json", self.pr_json, "codex", "gpt-test", "T42"])
        self.assertEqual(rc, 3)
        self.assertIn("cannot read pr-json:", out.getvalue())

    def test_cli_reports_invalid_pr_json(self):
        with open(self.pr_json, "w", encoding="utf-8") as fh:
            fh.write("not JSON")
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = pr_repair.main([self.brief, "--pr-json", self.pr_json, "codex", "gpt-test", "T42"])
        self.assertEqual(rc, 3)
        self.assertIn("cannot read pr-json:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
