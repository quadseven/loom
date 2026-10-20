"""Station-checked facts in the proof comment (bus/proof.py render and
contradicts, bus/verify_data.py close_facts and failing_checks)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import proof, verify_data  # noqa: E402

BODY = "## Acceptance criteria\n- [x] A test passes\n- [x] Live: the daily line shows it\n"
SHA = "a" * 40
FACT = "PR #3 merged at 2030-01-02T00:00:00Z as %s; checks: unit tests success, " \
       "Grug - Guard neutral" % SHA


def record(evidence="ci run 12 shows the test", **changes):
    out = {"kind": "docs", "revision": "abc1234",
           "observed": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"},
           "sources": ["https://example.com/logs/1"],
           "criteria": [{"criterion": "A test passes", "result": "pass", "evidence": evidence},
                        {"criterion": "Live: the daily line shows it", "result": "pass",
                         "evidence": "seen at 2030-01-02T01:00Z"}]}
    out.update(changes)
    return out


class RenderFactsTest(unittest.TestCase):
    def test_facts_are_a_section_before_the_criteria_and_read_back(self):
        comment = proof.render(record(), BODY, facts=[FACT])
        self.assertIn("Checked by the station:\n- %s\n" % FACT, comment)
        self.assertLess(comment.index("Checked by the station:"), comment.index("Criteria:"))
        self.assertLess(comment.index("Proof (docs)"), comment.index("Checked by the station:"))
        self.assertEqual(proof.read(comment)["station_facts"], [FACT])

    def test_no_facts_renders_as_before(self):
        comment = proof.render(record(), BODY)
        self.assertNotIn("Checked by the station", comment)
        self.assertNotIn("station_facts", proof.read(comment))

    def test_a_lane_cannot_write_its_own_station_facts(self):
        forged = record(station_facts=["every check passed"])
        self.assertNotIn("station_facts", proof.read(proof.render(forged, BODY)))
        self.assertEqual(proof.read(proof.render(forged, BODY, facts=[FACT]))["station_facts"],
                         [FACT])

    def test_the_contract_and_check_are_unchanged_by_facts(self):
        comment = proof.render(record(), BODY, facts=[FACT])
        self.assertEqual(proof.read(comment)["contract"], proof.contract(BODY))
        proof.check(record(), BODY)


class ContradictsTest(unittest.TestCase):
    failing = {"Grug - Guard": "neutral"}

    def test_a_claim_that_all_checks_passed_against_a_neutral_check_is_a_reason(self):
        for claim in ("all checks passed", "Every check passed on the merge commit",
                      "CI green: all CI succeeded", "checks green on main",
                      "all the checks are green"):
            why = proof.contradicts(record(evidence=claim), self.failing)
            self.assertEqual(why, "evidence says every check passed but Grug - Guard "
                                  "concluded neutral", claim)

    def test_no_failing_check_or_no_claim_is_no_reason(self):
        self.assertEqual(proof.contradicts(record(evidence="all checks passed"), {}), "")
        self.assertEqual(proof.contradicts(record(), self.failing), "")
        self.assertEqual(proof.contradicts(record(evidence="unit tests passed"), self.failing),
                         "")

    def test_a_record_without_criteria_is_no_reason(self):
        self.assertEqual(proof.contradicts({"criteria": "x"}, self.failing), "")


class CloseFactsTest(unittest.TestCase):
    def pull(self, **changes):
        out = {"number": 3, "title": "Fix it", "merge_commit_sha": SHA,
               "merged_at": "2030-01-02T00:00:00Z", "on_branch": True,
               "check_runs": [{"id": 1, "name": "unit tests", "status": "completed",
                               "conclusion": "success"},
                              {"id": 2, "name": "Grug - Guard", "status": "completed",
                               "conclusion": "neutral"},
                              {"id": 3, "name": "docs", "status": "completed",
                               "conclusion": "skipped"}]}
        out.update(changes)
        return out

    def test_a_merged_pull_is_one_line_with_its_checks(self):
        lines = verify_data.close_facts("main", [self.pull()])
        self.assertEqual(lines, [
            "PR #3 merged at 2030-01-02T00:00:00Z as %s; the head of main contains it; "
            "checks: unit tests success, Grug - Guard neutral, docs skipped" % SHA])

    def test_failing_checks_are_those_not_success_or_skipped(self):
        self.assertEqual(verify_data.failing_checks([self.pull()]), {"Grug - Guard": "neutral"})

    def test_a_rerun_replaces_the_run_before_it(self):
        runs = [{"id": 5, "name": "Grug - Elder", "status": "completed", "conclusion": "neutral"},
                {"id": 9, "name": "Grug - Elder", "status": "completed", "conclusion": "success"}]
        self.assertEqual(verify_data.failing_checks([self.pull(check_runs=runs)]), {})
        self.assertIn("Grug - Elder success", verify_data.close_facts("main",
                                                                      [self.pull(check_runs=runs)])[0])

    def test_a_running_check_is_not_passed(self):
        runs = [{"id": 1, "name": "unit tests", "status": "in_progress", "conclusion": None}]
        self.assertEqual(verify_data.failing_checks([self.pull(check_runs=runs)]),
                         {"unit tests": "in_progress"})

    def test_unread_branch_no_checks_no_pulls_and_errors_say_so(self):
        line = verify_data.close_facts("main", [self.pull(on_branch=None, check_runs=[])])[0]
        self.assertIn("the head of main is not known to contain it", line)
        self.assertIn("no check runs on the merge commit", line)
        self.assertEqual(verify_data.close_facts("main", []),
                         ["No merged pull request names this ticket."])
        self.assertEqual(verify_data.close_facts("main", [], "status 502"),
                         ["GitHub could not be read (status 502); pull request facts may be "
                          "missing."])

    def test_a_line_that_fails_the_text_checks_is_dropped(self):
        runs = [{"id": 1, "name": "deploy token=abc123", "status": "completed",
                 "conclusion": "success"}]
        self.assertEqual(verify_data.close_facts("main", [self.pull(check_runs=runs)]), [])

    def test_only_the_shown_pulls_are_read(self):
        unread = {"number": 9, "merge_commit_sha": "b" * 40, "merged_at": "2030-01-01T00:00:00Z"}
        lines = verify_data.close_facts("main", [self.pull(), unread])
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("PR #3 merged"))
        self.assertEqual(lines[1], "1 more merged pull request was not read.")
        self.assertEqual(verify_data.failing_checks([unread]), {})


if __name__ == "__main__":
    unittest.main()
