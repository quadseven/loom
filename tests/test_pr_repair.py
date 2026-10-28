from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import hunt_plan, pr_repair, text  # noqa: E402


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

    def test_rendered_body_reads_a_summary_heading_when_what_is_absent(self):
        # Ticket briefs often write the change under "## Summary"; the body's
        # ## Summary came out empty for them (loom#1468).
        brief = BRIEF.replace("## What\nMake the thing work.",
                              "## Summary\nMake the thing work for every user.")
        body = pr_repair._render_generated(brief, "codex", "gpt-test", "T42")
        self.assertIn("## Why\nThe thing is broken for every user.", body)
        self.assertIn("## Summary\nMake the thing work for every user.", body)

    def test_rendered_body_uses_summary_when_why_is_empty_and_what_absent(self):
        brief = BRIEF.replace("## Why\nThe thing is broken for every user.", "## Why\n")
        brief = brief.replace("## What\nMake the thing work.",
                              "## Summary\nMake the thing work for every user.")
        body = pr_repair._render_generated(brief, "codex", "gpt-test", "T42")
        self.assertIn("## Why\nMake the thing work for every user.", body)
        self.assertIn("## Summary\nMake the thing work for every user.", body)
        self.assertEqual(hunt_plan.problems(body), [])

    def test_rendered_body_uses_the_title_when_why_and_summary_are_empty(self):
        brief = BRIEF.replace("## Why\nThe thing is broken for every user.", "## Why\n")
        brief = brief.replace("## What\nMake the thing work.", "## What\n")
        body = pr_repair._render_generated(brief, "codex", "gpt-test", "T42")
        why = body.split("## Why\n", 1)[1].split("\n\n## Summary", 1)[0]
        self.assertTrue(why.strip(), body)
        self.assertEqual(why, pr_repair._brief_title(brief))

    def test_criteria_under_an_acceptance_heading_render_as_the_runner_renders_them(self):
        # lanes/run_lane.sh::criteria_section reads the same headings: a
        # repair that read only "## Acceptance criteria" emptied the section
        # the runner had filled (2026-10-25).
        for heading in ("## Acceptance", "## Acceptance Criteria"):
            with self.subTest(heading=heading):
                brief = BRIEF.replace("## Acceptance criteria\n", heading + "\n")
                body = pr_repair._render_generated(brief, "codex", "gpt-test", "T42")
                self.assertEqual(body, RUNNER_BODY)

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

    OPEN_7 = staticmethod(lambda repo, number: (
        {"number": 7, "state": "open", "created_at": "2026-09-01T00:00:00Z"}
        if (repo, number) == ("example-owner/example-repo", 7) else {}))

    def bugs_summary(self, lines):
        summary = os.path.join(self._tmp.name, "summary.md")
        with open(summary, "w", encoding="utf-8") as fh:
            fh.write("Fix it\n\n## Bugs discovered\n" + lines + "\n")
        return summary

    def test_a_bug_citing_an_open_issue_reaches_the_body(self):
        # loom#1779: a bug line is kept only when its issue checks out.
        self.write_brief(BRIEF)
        result = pr_repair.reconcile(
            self.brief, "Fix the thing", RUNNER_BODY, "codex", "gpt-test", "T42",
            summary_path=self.bugs_summary("- The parser drops a tab, #7"),
            repo="example-owner/example-repo", lookup=self.OPEN_7)
        self.assertEqual(result["reason"], "")
        self.assertIn("## Operator steps before merge\nnone stated\n\n"
                      "## Bugs discovered\n- The parser drops a tab, #7\n\n---\n", result["body"])

    def test_a_bug_line_that_does_not_check_out_refuses_the_repair(self):
        self.write_brief(BRIEF)
        for line, why in (("- The cache never expires", "cites no issue"),
                          ("- Missing, #8", "does not exist")):
            result = pr_repair.reconcile(
                self.brief, "Fix the thing", RUNNER_BODY, "codex", "gpt-test", "T42",
                summary_path=self.bugs_summary(line),
                repo="example-owner/example-repo", lookup=self.OPEN_7)
            self.assertFalse(result["changed"], line)
            self.assertIn(why, result["reason"])
            self.assertIn("file the issue and cite it as #N, or drop the line", result["reason"])

    def test_a_bug_line_is_refused_without_a_repo_to_check_it_in(self):
        self.write_brief(BRIEF)
        result = pr_repair.reconcile(
            self.brief, "Fix the thing", RUNNER_BODY, "codex", "gpt-test", "T42",
            summary_path=self.bugs_summary("- The parser drops a tab, #7"), lookup=self.OPEN_7)
        self.assertFalse(result["changed"])
        self.assertIn("no repo", result["reason"])

    def test_a_verified_bugs_section_survives_a_round_without_one(self):
        body = RUNNER_BODY.replace("none stated\n\n---",
                                   "none stated\n\n## Bugs discovered\n- Tab bug, #7\n\n---")
        self.write_brief(BRIEF)
        result = pr_repair.reconcile(self.brief, "Fix the thing", body, "codex", "gpt-test", "T42",
                                     lookup=self.OPEN_7)
        self.assertEqual(result["reason"], "")
        self.assertFalse(result["changed"], result)

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
                     "/private/var/loom/worktrees/T42/.loom/summary.md\n")
        result = pr_repair.reconcile(self.brief, "Fix the thing", RUNNER_BODY,
                                     "codex", "gpt-test", "T42", summary_path=summary)
        self.assertEqual(result["reason"], "")
        self.assertTrue(result["changed"])
        self.assertIn("## Operator steps before merge\nnone\nfilePath\">\n<path>\n", result["body"])
        self.assertNotIn("/private/", result["body"])

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
        body = RUNNER_BODY.replace("---\n", "Reviewer note: see /var/run/notes\n\n---\n")
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


TRUSTED = {"login": "alice-example"}
STRANGER = {"login": "mallory-example"}
SWEEP_TICKET = {"number": 42, "title": "Fix the thing", "state": "open", "user": TRUSTED,
                "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}], "body": (
    "## Why\nThe thing is broken for every user.\n\n## What\nMake the thing work.\n\n"
    "## Acceptance\n- [ ] The thing works\n- [ ] A test covers it\n- [ ] Nothing else changes\n\n"
    "## Size\n**Size:** S\n\n## Out of scope\n- Other things\n")}

# A lane body from before the criteria and size fixes (2026-10-25).
SWEEP_REF = "%s#%d" % ("example-owner/example-repo", 42)
SWEEP_BODY = ("## Why\nThe thing is broken for every user.\n\n## Summary\nMake the thing work.\n\n"
              "## Acceptance criteria\n\n\nSize: **Size:**\n\n## Out of scope\n- Other things\n\n"
              "## Operator steps before merge\nnone stated\n\n---\n"
              "Loom ticket example-owner.example-repo-42, lane codex, model gpt-6-luna.\n"
              "Refs " + SWEEP_REF)


def _run(name, conclusion):
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "started_at": "2026-10-25T00:00:00Z", "id": hash(name) % 1000}


class PlanSweepTest(unittest.TestCase):
    """loom#1837: open lane pull requests that fail only the plan check get
    their bodies rebuilt from their tickets, once, by a one-off sweep."""

    def setUp(self):
        self.edits = []
        self.said = []
        def pull(number, **extra):
            pr = {"number": number, "title": "Fix the thing", "body": SWEEP_BODY,
                  "draft": False, "user": TRUSTED,
                  "head": {"sha": "a%d" % number, "ref": "loom/42-fix-the-thing",
                           "repo": {"full_name": "example-owner/example-repo"}}}
            pr.update(extra)
            return pr
        self.pull = pull
        self.pulls = [
            pull(7),
            # Another check fails too: a lane round is right, not a sweep.
            pull(8),
            # Not a lane pull request: no runner footer.
            pull(9, body="## Why\nx"),
        ]
        self.runs = {"a7": [_run(pr_repair.PLAN_CHECK, "failure"), _run("unittest", "success")],
                     "a8": [_run(pr_repair.PLAN_CHECK, "failure"), _run("unittest", "failure")],
                     "a9": [_run(pr_repair.PLAN_CHECK, "failure")]}
        self.ticket = dict(SWEEP_TICKET)

    def api(self, path):
        if path.startswith("repos/example-owner/example-repo/pulls?"):
            return self.pulls
        if "/commits/" in path:
            return {"check_runs": self.runs.get(path.split("/commits/")[1].split("/")[0],
                                                self.runs["a7"])}
        if path == "repos/example-owner/example-repo/issues/42":
            return self.ticket
        raise AssertionError(path)

    def sweep(self, apply):
        return pr_repair.sweep(["example-owner/example-repo"], apply, api=self.api,
                               edit=lambda *a: self.edits.append(a), say=self.said.append,
                               authors=["Alice-Example"])

    def test_a_dry_run_lists_what_it_would_repair_and_writes_nothing(self):
        rows = self.sweep(apply=False)
        self.assertEqual([(r["number"], r["changed"]) for r in rows], [(7, True)])
        self.assertEqual(self.edits, [])
        self.assertIn("would repair", self.said[0])

    def test_apply_edits_the_body_so_it_passes(self):
        self.sweep(apply=True)
        self.assertEqual(len(self.edits), 1)
        repo, number, title, body = self.edits[0]
        self.assertEqual((repo, number, title), ("example-owner/example-repo", 7, "Fix the thing"))
        self.assertEqual(pr_repair.hunt_plan.problems(body), [])
        self.assertIn("- [ ] A test covers it", body)
        self.assertIn("\nSize: S\n", body)
        self.assertIn("Refs " + SWEEP_REF, body)

    def test_only_a_pull_request_the_factory_would_judge_is_repaired(self):
        # A forged footer and Refs line in a stranger's pull request, a
        # draft, a branch outside loom/ or in a fork, or a ticket that is not
        # a trusted loom:ticket at loom:pr is never edited (security review).
        cases = {
            "stranger": dict(user=STRANGER),
            "draft": dict(draft=True),
            "branch": dict(head={"sha": "a7", "ref": "feature/x",
                                 "repo": {"full_name": "example-owner/example-repo"}}),
            "fork": dict(head={"sha": "a7", "ref": "loom/42-fix-the-thing",
                               "repo": {"full_name": "mallory-example/example-repo"}}),
        }
        for name, extra in cases.items():
            with self.subTest(name=name):
                self.edits, self.said = [], []
                self.pulls = [self.pull(7, **extra)]
                self.assertEqual(self.sweep(apply=True), [])
                self.assertEqual(self.edits, [])
                self.assertIn("skipped", self.said[0])
        for name, ticket in {"stranger ticket": dict(user=STRANGER),
                             "not at pr": dict(labels=[{"name": "loom:ticket"},
                                                       {"name": "loom:merged"}]),
                             "not a ticket": dict(labels=[{"name": "loom:pr"}])}.items():
            with self.subTest(name=name):
                self.edits, self.said = [], []
                self.pulls = [self.pull(7)]
                self.ticket = dict(SWEEP_TICKET, **ticket)
                self.assertEqual(self.sweep(apply=True), [])
                self.assertEqual(self.edits, [])

    def test_a_ticket_that_lacks_the_parts_is_reported_not_edited(self):
        self.ticket["body"] = self.ticket["body"].replace("## Acceptance\n", "## Notes\n")
        rows = self.sweep(apply=True)
        self.assertEqual(self.edits, [])
        self.assertFalse(rows[0]["changed"])
        self.assertIn("## Acceptance criteria", self.said[0])


class PlanRepairTest(unittest.TestCase):
    def test_the_plan_command_writes_a_passing_body_or_names_the_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = os.path.join(tmp, "brief.md")
            body = os.path.join(tmp, "body.md")
            out = os.path.join(tmp, "out.md")
            with open(brief, "w") as fh:
                fh.write("Issue: " + SWEEP_REF + "\n\n# T42: Fix the thing\n\n"
                         + SWEEP_TICKET["body"])
            with open(body, "w") as fh:
                fh.write(SWEEP_BODY + "\n\n" + pr_repair.PLAN_HOLD_MARKER)
            argv = ["plan", brief, body, "codex", "gpt-6-luna",
                    "example-owner.example-repo-42", "--out", out]
            self.assertEqual(pr_repair.main(argv), 0)
            with open(out) as fh:
                fixed = fh.read()
            self.assertEqual(pr_repair.hunt_plan.problems(fixed), [])
            self.assertNotIn(pr_repair.PLAN_HOLD_MARKER, fixed)
            with open(brief, "w") as fh:
                fh.write(SWEEP_TICKET["body"].replace("**Size:** S", "big"))
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                self.assertEqual(pr_repair.main(argv), 1)
            self.assertEqual(printed.getvalue(), "## Size (XS, S, M or L)\n")

    def test_gaps_name_the_ticket_sections(self):
        self.assertEqual(pr_repair.ticket_gaps([
            "## Acceptance criteria has 0 non-empty bullets; need >=3",
            "no Size: XS/S/M/L/XL in body", "## Why has 2 words; need >=5"]),
            ["## Acceptance criteria (three or more \"- [ ]\" lines)",
             "## Size (XS, S, M or L)", "## Why (five or more words)"])
