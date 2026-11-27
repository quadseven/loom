"""Every lane signs what it writes: the signature line on each pull request
body and comment, and the four Loom- trailers on each commit."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import lane, pr_repair, signature  # noqa: E402

MODEL = "example-provider/example-model-9"
SCRIPT = (ROOT / "lanes" / "run_lane.sh").read_text(encoding="utf-8")
BRIEF = """# Brief: example-owner.example-repo-42

Repo: example-owner/example-repo
Lane: spark
Issue: example-owner/example-repo""" """#42

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


def shell(snippet: str, **env: str) -> str:
    """Run snippet in bash with run_lane.sh's signing helpers defined."""
    start = SCRIPT.index("\nsigned() {")
    end = SCRIPT.index("\n}\n", SCRIPT.index("\ncommit_msg() {")) + 3
    prelude = 'LOOM_ROOT="%s"\nSLOT="${SLOT:-spark}"\n%s\n' % (ROOT, SCRIPT[start:end])
    out = subprocess.run(["bash", "-c", prelude + snippet], capture_output=True, text=True,
                         env={**{k: v for k, v in os.environ.items() if k != "MODEL" and not k.endswith("_MODEL")},
                              **env}, check=True)
    return out.stdout


class SignatureLineTest(unittest.TestCase):
    def test_the_line_names_the_lane_and_the_configured_model(self):
        with mock.patch.dict(os.environ, {"LOOM_SPARK_MODEL": MODEL}):
            self.assertEqual(signature.line("spark"), "Signed: lane-spark, model " + MODEL)
            self.assertEqual(signature.line("lane-spark"), "Signed: lane-spark, model " + MODEL)

    def test_a_slot_reads_its_own_model_before_its_lane_family(self):
        env = {"LOOM_OPENCODE_GO_MODEL": "family-model", "LOOM_OPENCODE_GO_3_MODEL": "slot-model"}
        self.assertEqual(signature.model_for("opencode-go-3", env), "slot-model")
        self.assertEqual(signature.model_for("opencode-go-2", env), "family-model")
        self.assertEqual(signature.model_for("copilot", env), "")

    def test_a_lane_with_no_configured_model_says_unknown_never_a_literal_model(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(signature.line("spark"), "Signed: lane-spark, model unknown")

    def test_signing_twice_leaves_one_line_and_the_last_model_wins(self):
        once = signature.sign("Body text.", "spark", "model-a")
        twice = signature.sign(once, "spark", "model-b")
        self.assertEqual(twice, "Body text.\n\nSigned: lane-spark, model model-b")
        self.assertEqual(twice.count("Signed: "), 1)
        self.assertEqual(signature.sign(twice, "spark", "model-b"), twice)

    def test_the_line_is_ascii_with_no_dash_characters(self):
        text = signature.sign("Body.", "spark", "model\u2014x\u2013y\nz")
        self.assertTrue(text.isascii())
        self.assertNotIn("\u2014", text)
        self.assertEqual(text.splitlines()[-1], "Signed: lane-spark, model model-x-y z")

    def test_a_marker_after_the_signature_is_kept_and_the_line_is_not_doubled(self):
        body = "Text.\n\nSigned: lane-spark, model a\n\n<!-- loom:plan-hold v1 -->"
        self.assertEqual(signature.strip(body), "Text.\n\n<!-- loom:plan-hold v1 -->")
        again = signature.sign(body, "spark", "b")
        self.assertEqual(again.count("Signed: "), 1)
        self.assertEqual(signature.strip("Text.\n\n<!-- m -->"), "Text.\n\n<!-- m -->")

    def test_ordinary_text_comes_back_unchanged(self):
        for plain in ("Plain words with a | pipe and a b | c.", "Signed: not a signature",
                      "a <b> tag and 100% done", "line one\nline two"):
            self.assertEqual(signature.strip(plain), plain)
            self.assertEqual(signature._clean(plain.replace("\n", " ")),
                             plain.replace("\n", " "))
            self.assertEqual(signature.sign(plain, "spark", "m"),
                             plain + "\n\nSigned: lane-spark, model m")

    def test_a_signed_line_inside_the_text_is_kept(self):
        body = "Signed: lane-x, model y\nmore text"
        self.assertEqual(signature.strip(body), body)

    def test_commit_trailers_are_added_once(self):
        message = signature.commit_message("Fix the thing", "spark", MODEL)
        self.assertEqual(
            message,
            "Fix the thing\n\nLoom-Engine: unknown\nLoom-Model: %s\nLoom-Lane: lane-spark\n"
            "Loom-Run: unknown\n" % MODEL)
        self.assertEqual(signature.commit_message(message, "spark", MODEL), message)

    def test_commit_carries_the_four_trailers_with_engine_and_run(self):
        message = signature.commit_message("Fix", "spark", "m1", "eng", "run-7")
        self.assertEqual(message.splitlines()[-4:], [
            "Loom-Engine: eng", "Loom-Model: m1", "Loom-Lane: lane-spark", "Loom-Run: run-7"])
        again = signature.commit_message(message, "spark", "m1", "eng", "run-7")
        self.assertEqual(again, message)

    def test_engine_and_run_fall_back_to_the_environment(self):
        env = {"LOOM_SPARK_ENGINE": "eng-env", "LOOM_RUN_ID": "r9"}
        with mock.patch.dict(os.environ, env, clear=False):
            text = signature.trailers("spark", "m1")
        self.assertIn("Loom-Engine: eng-env", text)
        self.assertIn("Loom-Run: r9", text)


class PullRequestBodyTest(unittest.TestCase):
    def test_the_first_body_a_lane_opens_is_signed_with_the_configured_model(self):
        issue = {"number": 42, "title": "Fix the thing", "body": BRIEF, "labels": []}
        with mock.patch.dict(os.environ, {"LOOM_SPARK_MODEL": MODEL}), \
                mock.patch.object(lane.tickets, "render_brief", return_value=BRIEF):
            body = lane.pr_body("example-owner/example-repo", issue, "logic",
                                {"logic": 1, "tests": 1, "docs": 0, "sensitive": 0}, lane="spark")
        self.assertEqual(body.rstrip().splitlines()[-1], "Signed: lane-spark, model " + MODEL)
        self.assertEqual(body.count("Signed: "), 1)

    def test_a_repair_edit_signs_the_body_once_and_does_not_stack_on_a_second_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = os.path.join(tmp, "brief.md")
            Path(brief).write_text(BRIEF, encoding="utf-8")
            old = pr_repair._render_generated(BRIEF, "spark", MODEL, "T42").replace(
                "## Operator steps before merge\nnone stated\n\n", "")
            first = pr_repair.reconcile(brief, "Fix the thing", old, "spark", MODEL, "T42")
            self.assertEqual(first["reason"], "")
            self.assertTrue(first["changed"])
            self.assertEqual(first["body"].rstrip().splitlines()[-1],
                             "Signed: lane-spark, model " + MODEL)
            # The edited body, with a reviewer note after the footer, edited again.
            stale = first["body"].replace("The thing works", "The thing works!", 1)
            noted = stale.replace("\n\nSigned:", "\n\nReviewer note.\n\nSigned:")
            second = pr_repair.reconcile(brief, "Fix the thing", noted, "spark", MODEL, "T42")
            self.assertEqual(second["reason"], "")
            self.assertTrue(second["changed"])
            self.assertEqual(second["body"].count("Signed: "), 1)
            self.assertEqual(second["body"].rstrip().splitlines()[-1],
                             "Signed: lane-spark, model " + MODEL)
            self.assertIn("Reviewer note.", second["body"])

    def test_a_body_that_differs_only_by_its_signature_is_not_edited(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = os.path.join(tmp, "brief.md")
            Path(brief).write_text(BRIEF, encoding="utf-8")
            body = pr_repair._render_generated(BRIEF, "spark", MODEL, "T42")
            for current in (body, signature.sign(body, "spark", "another-model")):
                result = pr_repair.reconcile(brief, "Fix the thing", current, "spark", MODEL, "T42")
                self.assertFalse(result["changed"])


class LaneCommentTest(unittest.TestCase):
    def test_the_claim_and_heartbeat_comments_are_signed_and_stay_signed_once_edited(self):
        with mock.patch.dict(os.environ, {"LOOM_MUSE_MODEL": MODEL}):
            claim = lane._claim_body("muse", 4, 1000.0)
            again = lane._claim_body("muse", 4, 2000.0, 1000.0)
            self.assertEqual(claim.splitlines()[-1], "Signed: lane-muse, model " + MODEL)
            self.assertEqual(again.count("Signed: "), 1)
            self.assertEqual(lane.lane_markers.claim_match(claim).group(1), "muse")

    def test_a_hand_back_comment_is_signed(self):
        posted = []
        with mock.patch.dict(os.environ, {"LOOM_MUSE_MODEL": MODEL}), \
                mock.patch.object(lane.notify, "post",
                                  side_effect=lambda gh, repo, n, body, kind: posted.append(body)), \
                mock.patch.object(lane, "_names", return_value=[]):
            gh = mock.Mock()
            lane._hand_back(gh, "example-owner/example-repo", {"number": 4}, "muse", "ran out")
        self.assertEqual(posted, ["[transient] ran out\n\nSigned: lane-muse, model " + MODEL])


class RunnerShellTest(unittest.TestCase):
    def test_signed_adds_one_line_with_the_runs_model(self):
        out = shell('signed "A comment."')
        self.assertEqual(out, "A comment.\n\nSigned: lane-spark, model unknown")
        out = shell('MODEL=%s; signed "$(signed "A comment.")"' % MODEL)
        self.assertEqual(out, "A comment.\n\nSigned: lane-spark, model " + MODEL)

    def test_signed_reads_the_lanes_configured_model_when_the_run_has_none(self):
        out = shell('signed "A comment."', LOOM_SPARK_MODEL=MODEL)
        self.assertEqual(out, "A comment.\n\nSigned: lane-spark, model " + MODEL)

    def test_sign_tail_ends_a_message_that_keeps_its_literal_opening(self):
        out = shell('MODEL=%s; printf "%%s" "Cleared: done$(sign_tail)"' % MODEL)
        self.assertEqual(out, "Cleared: done\n\nSigned: lane-spark, model " + MODEL)

    def test_commit_msg_carries_both_trailers_once(self):
        out = shell('MODEL=%s; commit_msg "$(commit_msg "Fix the thing")"' % MODEL)
        self.assertEqual(out.strip(), "Fix the thing\n\nLoom-Engine: unknown\nLoom-Model: " + MODEL
                         + "\nLoom-Lane: lane-spark\nLoom-Run: unknown")

    def test_commit_msg_names_the_lanes_engine_and_the_briefs_run(self):
        out = shell('LANE=opencode-go; TICKET=example.repo-7-r2; MODEL=%s; commit_msg "Fix"' % MODEL,
                    SLOT="opencode-go-2")
        self.assertEqual(out.strip().splitlines()[-4:], [
            "Loom-Engine: opencode", "Loom-Model: " + MODEL, "Loom-Lane: lane-opencode-go-2",
            "Loom-Run: example.repo-7-r2"])

    def test_lane_engine_names_the_program_run_agent_starts_for_each_lane(self):
        cases = {"codex": "codex", "claude": "claude", "anthropic": "claude", "agy": "agy",
                 "copilot": "copilot", "openrouter": "opencode", "tinker": "opencode",
                 "opencode-go": "opencode", "zen": "opencode", "spark": "opencode",
                 "free": "opencode", "zai": "opencode", "workersai": "opencode", "nosuch": ""}
        for name, engine in cases.items():
            self.assertEqual(shell("LANE=%s; lane_engine" % name), engine, name)
        body = SCRIPT[SCRIPT.index("\nrun_agent() {"):]
        body = body[:body.index("\n}\n")]
        groups = re.findall(r"^    ([a-z|-]+)\)", body, re.M)
        lanes = {x for group in groups for x in group.split("|")}
        self.assertTrue(lanes)
        self.assertEqual(lanes - set(cases), set(), "lane_engine misses a run_agent lane")

    def test_a_real_commit_made_with_commit_msg_has_the_trailers(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(git + ["init", "-q"], check=True)
            Path(tmp, "f").write_text("x", encoding="utf-8")
            subprocess.run(git + ["add", "f"], check=True)
            message = shell('MODEL=%s; commit_msg "Add f"' % MODEL)
            subprocess.run(git + ["commit", "-qm", message], check=True)
            trailers = subprocess.run(
                git + ["log", "-1", "--format=%(trailers:key=Loom-Lane,valueonly)"
                       "%(trailers:key=Loom-Model,valueonly)"],
                capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(trailers, ["lane-spark", MODEL])

    def test_strip_removes_a_signature_so_a_sticky_merge_never_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.md")
            Path(path).write_text(signature.sign("<!-- m -->\nBody", "spark", MODEL), encoding="utf-8")
            subprocess.run([sys.executable, "-B", "-m", "bus.signature", "strip", path],
                           cwd=ROOT, check=True)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "<!-- m -->\nBody")

    def test_every_runner_write_to_github_goes_through_the_signing_helpers(self):
        # No lane comment, ticket, close message, reply or PR body and no commit
        # reaches GitHub unsigned: each write names `signed` or `commit_msg`.
        lines = SCRIPT.splitlines()
        write = re.compile(r"(gh issue (comment|create|close)|gh pr (create|close)).*|"
                           r"safe_git commit|merge --no-edit")
        for number, text in enumerate(lines, 1):
            if text.lstrip().startswith("#") or not write.search(text):
                continue
            window = " ".join(lines[max(0, number - 1):number + 2])
            if "gh issue create" in text:
                window = " ".join(lines[number - 1:number + 3])
            if "gh pr create" in text:
                window = " ".join(lines[number - 8:number])
            if re.search(r"gh (issue|pr) (edit|view|list)", text):
                continue
            self.assertRegex(window, r"signed |sign_tail|commit_msg ", "run_lane.sh:%d is unsigned" % number)

    def test_the_sticky_comment_and_review_reply_writers_sign(self):
        sticky = SCRIPT[SCRIPT.index("\nupsert_sticky_comment() {"):SCRIPT.index("\nROUND_MARKER=")]
        self.assertIn('signed "$(cat "$new_file")"', sticky)
        self.assertIn("bus.signature strip", sticky)
        replies = SCRIPT[SCRIPT.index("\npost_replies() {"):]
        self.assertIn('body="$(signed "$body")"', replies[:replies.index("\n}\n")])


if __name__ == "__main__":
    unittest.main()
