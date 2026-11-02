"""The weekly spot mutation run and its survivor ratchet (bus/mutation_run.py)."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import mutation_run as mr  # noqa: E402

CODE = """def older(age):
    return age >= 18


def label():
    return "x"
"""
# Pins the boundary, never reads label().
TEST = """import unittest
import seam


class T(unittest.TestCase):
    def test_older(self):
        self.assertTrue(seam.older(18))
        self.assertFalse(seam.older(17))
"""
KILLED = ("boundary", "18", "19", CODE.replace("18", "19"), 2)
SURVIVES = ("return-value", 'return "x"', "return None", CODE.replace('return "x"', "return None"), 6)


def engine(*rows):
    """A stand-in with the pinned engine's interface."""
    def generate_mutants(source, *, file, target_lines):
        return [types.SimpleNamespace(file=file, line=line, operator=op, original=old, mutated=new,
                                      source=text)
                for op, old, new, text, line in rows if line in target_lines]
    return types.SimpleNamespace(generate_mutants=generate_mutants)


class Tree(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "seam.py").write_text(CODE)
        (self.root / "test_seam.py").write_text(TEST)
        self.seams = (("seam.py", "test_seam"),)

    def run_it(self, *rows, **caps):
        return mr.run(self.root, engine(*rows), self.seams, **caps)


class Run(Tree):
    def test_a_known_surviving_mutant_is_reported_and_a_killed_one_is_not(self):
        report = self.run_it(KILLED, SURVIVES)
        seam = report.seams["seam.py"]
        self.assertEqual((seam.planned, seam.run), (2, 2))
        self.assertEqual([s.source for s in seam.survivors], ['return "x"'])
        self.assertFalse(report.partial)

    def test_the_tree_is_left_as_it_was(self):
        self.run_it(KILLED, SURVIVES)
        self.assertEqual((self.root / "seam.py").read_text(), CODE)

    def test_a_mutant_that_hangs_counts_as_killed(self):
        hang = ("hang", "age", "hang", "import time\ntime.sleep(30)\n" + CODE, 2)
        report = self.run_it(hang, mutant_seconds=1)
        self.assertEqual(report.seams["seam.py"].survivors, [])
        self.assertEqual(report.seams["seam.py"].run, 1)

    def test_the_run_holds_no_token_of_the_caller(self):
        (self.root / "test_seam.py").write_text(
            "import os, unittest\nclass T(unittest.TestCase):\n"
            "    def test_env(self):\n"
            "        self.assertNotIn('GH_TOKEN', os.environ)\n"
            "        import seam\n        self.assertTrue(seam.older(18))\n")
        os.environ["GH_TOKEN"] = "not-a-real-value"
        self.addCleanup(os.environ.pop, "GH_TOKEN")
        report = self.run_it(KILLED)
        self.assertEqual(report.seams["seam.py"].survivors, [])
        self.assertEqual(report.seams["seam.py"].run, 1)

    def test_a_seam_whose_tests_fail_unmutated_stops_the_run(self):
        (self.root / "test_seam.py").write_text(TEST.replace("assertTrue", "assertFalse"))
        with self.assertRaisesRegex(ValueError, "fail before any mutation"):
            self.run_it(KILLED)

    def test_every_mutant_runs_so_one_edit_cannot_reshuffle_the_rest(self):
        rows = [("op", "a", "b", CODE, line) for line in range(1, 7)]
        report = self.run_it(*rows)
        self.assertEqual(report.seams["seam.py"].planned, 6)
        self.assertEqual(report.seams["seam.py"].run, 6)

    def test_a_seam_slower_than_a_mutants_bound_stops_the_run(self):
        (self.root / "test_seam.py").write_text("import time\ntime.sleep(5)\n" + TEST)
        with self.assertRaisesRegex(ValueError, "fail before any mutation"):
            self.run_it(KILLED, mutant_seconds=1)


class Caps(Tree):
    def test_the_mutant_cap_stops_the_run_and_reports_partial_never_a_pass(self):
        report = self.run_it(KILLED, SURVIVES, max_mutants=1)
        self.assertTrue(report.partial)
        self.assertEqual(report.seams["seam.py"].run, 1)
        code, said = mr.verdict(report, {"seams": {"seam.py": [], }})
        self.assertEqual(code, mr.PARTIAL)
        self.assertIn("partial", said)

    def test_a_seam_past_the_deadline_starts_no_tests_and_reads_partial(self):
        (self.root / "other.py").write_text(CODE)
        self.seams = (("seam.py", "test_seam"), ("other.py", "test_seam"))
        clock = iter([0.0] + [9999.0] * 20)
        report = self.run_it(KILLED, minutes=1, clock=lambda: next(clock))
        self.assertTrue(report.partial)
        self.assertEqual(report.seams["other.py"].run, 0)

    def test_the_time_cap_stops_the_run_and_reports_partial(self):
        clock = iter([0.0, 0.0, 9999.0, 9999.0, 9999.0, 9999.0, 9999.0])
        report = self.run_it(KILLED, SURVIVES, minutes=1, clock=lambda: next(clock))
        self.assertTrue(report.partial)
        self.assertLess(report.seams["seam.py"].run, 2)


class Ratchet(Tree):
    def baseline(self, *keys):
        return {"seams": {"seam.py": list(keys)}}

    def test_a_count_above_baseline_exits_non_zero(self):
        code, said = mr.verdict(self.run_it(KILLED, SURVIVES), self.baseline())
        self.assertEqual(code, mr.ABOVE)
        self.assertIn("seam.py", said)

    def test_a_count_at_baseline_exits_zero(self):
        report = self.run_it(KILLED, SURVIVES)
        keys = [s.key for s in report.seams["seam.py"].survivors]
        self.assertEqual(mr.verdict(report, self.baseline(*keys))[0], mr.PASSED)

    def test_a_seam_with_no_baseline_fails(self):
        self.assertEqual(mr.verdict(self.run_it(KILLED), {"seams": {}})[0], mr.ABOVE)

    def test_a_key_outlives_a_line_move(self):
        report = self.run_it(SURVIVES)
        self.assertNotIn("6", report.seams["seam.py"].survivors[0].key)
        self.assertIn('return "x"', report.seams["seam.py"].survivors[0].key)

    def test_write_baseline_records_the_survivors_and_the_minutes(self):
        report = self.run_it(KILLED, SURVIVES)
        out = mr.baseline_of(report, "a" * 40)
        self.assertEqual(out["engine"], "a" * 40)
        self.assertEqual(len(out["seams"]["seam.py"]), 1)
        self.assertIn("minutes", out)
        json.dumps(out)

    def test_a_partial_run_cannot_write_a_baseline(self):
        with self.assertRaises(ValueError):
            mr.baseline_of(self.run_it(KILLED, SURVIVES, max_mutants=1), "a" * 40)


class Report(Tree):
    def test_a_report_survives_its_file_for_the_step_that_holds_the_token(self):
        report = self.run_it(KILLED, SURVIVES)
        path = self.root / "report.json"
        mr.write_report(report, path)
        back = mr.read_report(path)
        self.assertEqual(back, report)

    def test_a_baseline_for_another_engine_refuses_the_run(self):
        with self.assertRaisesRegex(ValueError, "engine"):
            mr.check_engine({"engine": "a" * 40}, "b" * 40)
        mr.check_engine({"engine": "a" * 40}, "a" * 40)


class Workflow(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent
    # In docs/ until the operator moves it; an agent App cannot write workflows.
    LIVE = ROOT / ".github" / "workflows" / "mutation.yml"
    TEXT = (LIVE if LIVE.exists() else ROOT / "docs" / "mutation-workflow.yml").read_text()

    def test_it_runs_weekly_on_main_only_on_a_hosted_runner(self):
        self.assertIn("cron:", self.TEXT)
        self.assertIn("if: github.ref == 'refs/heads/main'", self.TEXT)
        self.assertIn("runs-on: ubuntu-latest", self.TEXT)
        self.assertNotIn("pull_request", self.TEXT)

    def test_its_time_bound_covers_the_runs_own_cap(self):
        minutes = int(re.search(r"timeout-minutes: (\d+)", self.TEXT).group(1))
        self.assertGreater(minutes, mr.MINUTES)

    def test_it_holds_no_paid_key_and_pins_the_engine_of_the_baseline(self):
        self.assertNotIn("secrets.", self.TEXT)
        pin = re.search(r"ref: ([0-9a-f]{40})\n", self.TEXT).group(1)
        self.assertEqual(pin, json.loads(mr.BASELINE.read_text())["engine"])
        self.assertIn("--engine-sha %s" % pin, self.TEXT)

    def test_the_step_that_runs_mutants_holds_no_token(self):
        run_step = self.TEXT.split("- name: Spot mutation run", 1)[1].split("- name:", 1)[0]
        self.assertNotIn("GH_TOKEN", run_step)

    def test_the_baseline_names_every_seam(self):
        baseline = json.loads(mr.BASELINE.read_text())
        self.assertEqual(sorted(baseline["seams"]), sorted(path for path, _ in mr.SEAMS))
        self.assertGreater(baseline["minutes"], 0)


class Reply:
    def __init__(self, data):
        self.data = data


class FakeGitHub:
    def __init__(self, issues=()):
        self.issues = list(issues)
        self.writes = []

    def paginate(self, path):
        return list(self.issues)

    def rest(self, method, path, body=None):
        self.writes.append((method, path, body))
        return Reply({"number": 9})


class Rolling(Tree):
    REPO = "example-owner/example-repo"

    def test_a_first_run_files_one_issue_and_a_later_run_edits_it_with_no_comment(self):
        report = self.run_it(KILLED, SURVIVES)
        gh = FakeGitHub()
        mr.rolling_issue(gh, self.REPO, report, {"seams": {}})
        self.assertEqual([w[0] for w in gh.writes], ["POST"])
        body = gh.writes[0][2]["body"]
        self.assertIn('`seam.py:6`', body)
        self.assertIn('`return "x"`', body)
        gh = FakeGitHub([{"number": 9, "body": "old"}])
        mr.rolling_issue(gh, self.REPO, report, {"seams": {}})
        self.assertEqual([(w[0], w[1].rsplit("/", 1)[1]) for w in gh.writes], [("PATCH", "9")])
        self.assertFalse([w for w in gh.writes if w[1].endswith("/comments")])

    def test_an_unchanged_body_writes_nothing(self):
        report = self.run_it(KILLED)
        body = mr.issue_body(report, {"seams": {"seam.py": []}})
        gh = FakeGitHub([{"number": 9, "body": body}])
        mr.rolling_issue(gh, self.REPO, report, {"seams": {"seam.py": []}})
        self.assertEqual(gh.writes, [])

    def test_only_new_survivors_are_listed_and_partial_is_said(self):
        report = self.run_it(KILLED, SURVIVES, max_mutants=1)
        self.assertIn("Partial", mr.issue_body(report, {"seams": {}}))
        report = self.run_it(SURVIVES)
        key = report.seams["seam.py"].survivors[0].key
        self.assertNotIn('`return "x"`', mr.issue_body(report, {"seams": {"seam.py": [key]}}))
