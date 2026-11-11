"""Adapter coverage is independent of its matching output lines."""
from __future__ import annotations

import unittest
from bus import live_adapter, live_proof
from tests import test_live_adapter

START = 1893456000.0  # 2030-01-01T00:00:00Z
HOUR = 3600.0
LIVE = "Live: over 2 hours after the deploy, `log:worker` never shows `worker failed`."
BODY = "## Acceptance criteria\n- [ ] " + LIVE + "\n"


def result(answer):
    return live_proof.prove(BODY, START,
        live_proof.Sources(source_log=lambda *args: answer), now=START + 3 * HOUR)


def read_answer(start=None, end=None, hours=24, lines=()):
    output = ["loom-evidence-window-hours: %s" % hours]
    if start is not None:
        output.append("loom-evidence-observed-start: " + start)
    if end is not None:
        output.append("loom-evidence-observed-end: " + end)
    output.extend(lines)
    script = "#!/bin/sh\ncat <<'EOF'\n" + "\n".join(output) + "\nEOF\nexit 1\n"
    return test_live_adapter.RunnerTest().runner(script).log("worker", "worker failed", START)


class ActualLogWindow(unittest.TestCase):
    def test_empty_matches_short_actual_window_cannot_use_claimed_hours(self):
        outcome = result(read_answer("2030-01-01T01:00:00Z", "2030-01-01T01:30:00Z"))
        self.assertFalse(outcome.proven)
        self.assertIn("0.5 hours", outcome.missing[0][1])
        self.assertIn("2.0 needed", outcome.missing[0][1])

    def test_predeploy_matches_with_full_coverage_pass(self):
        answer = read_answer("2029-12-31T03:00:00Z", "2030-01-01T03:00:00Z",
                             lines=["2029-12-31T23:00:00Z worker failed"])
        self.assertTrue(result(answer).proven)

    def test_legacy_no_boundary_contract_is_unchanged(self):
        self.assertTrue(result(live_proof.Answer([], hours=24, count=0)).proven)
        self.assertTrue(result(live_proof.Answer(
            ["2029-12-31T23:00:00Z worker failed"], hours=24)).proven)

    def test_boundaries_are_metadata_not_match_lines(self):
        answer = read_answer("2030-01-01T00:30:00Z", "2030-01-01T02:30:00Z")
        self.assertEqual(answer.lines, [])
        self.assertEqual(answer.observed_start, START + HOUR / 2)
        self.assertEqual(answer.observed_end, START + 2.5 * HOUR)
        outcome = result(answer)
        self.assertTrue(outcome.proven)
        self.assertEqual(live_proof._covered_hours(answer, START, START + 3 * HOUR), 2.0)

    def test_smaller_claim_limits_actual_span(self):
        answer = read_answer("2030-01-01T00:00:00Z", "2030-01-01T03:00:00Z", hours=0.5)
        self.assertFalse(result(answer).proven)

    def test_predeploy_and_future_time_do_not_extend_coverage(self):
        for start, end in (("2029-12-31T22:00:00Z", "2030-01-01T00:30:00Z"),
                           ("2030-01-01T02:30:00Z", "2030-01-01T05:00:00Z")):
            with self.subTest(start=start):
                self.assertFalse(result(read_answer(start, end)).proven)

    def test_invalid_or_partial_bounds_fail_closed(self):
        for start, end in (("bad", "2030-01-01T03:00:00Z"),
                           ("2030-01-01T00:00:00Z", None),
                           (None, "2030-01-01T03:00:00Z"),
                           ("2030-01-01T03:00:00Z", "2030-01-01T00:00:00Z")):
            with self.subTest(start=start, end=end):
                self.assertIsNone(read_answer(start, end).lines)

    def test_duplicate_boundary_is_no_answer(self):
        answer = read_answer("2030-01-01T00:00:00Z", "2030-01-01T03:00:00Z",
                             lines=["loom-evidence-observed-end: 2030-01-01T03:00:00Z"])
        self.assertIsNone(answer.lines)

    def test_offset_boundaries_are_normalized(self):
        answer = read_answer("2030-01-01T01:30:00+01:00", "2030-01-01T03:30:00+01:00")
        self.assertTrue(result(answer).proven)
        self.assertEqual(answer.observed_start, START + HOUR / 2)
        self.assertEqual(answer.observed_end, START + 2.5 * HOUR)

    def test_single_timestamp_is_zero_coverage(self):
        outcome = result(read_answer("2030-01-01T01:00:00Z", "2030-01-01T01:00:00Z"))
        self.assertFalse(outcome.proven)
        self.assertIn("0.0 hours", outcome.missing[0][1])

    def test_zero_epoch_is_a_real_boundary(self):
        answer = read_answer("1970-01-01T00:00:00Z", "1970-01-01T00:30:00Z")
        outcome = live_proof.prove(BODY, 0, live_proof.Sources(source_log=lambda *args: answer),
                                   now=3 * HOUR)
        self.assertFalse(outcome.proven)
        self.assertIn("0.5 hours", outcome.missing[0][1])

    def test_smaller_passing_claim_limits_covered_hours(self):
        answer = read_answer("2029-12-31T00:00:00Z", "2030-01-01T02:30:00Z", hours=2)
        self.assertTrue(result(answer).proven)
        self.assertEqual(live_proof._covered_hours(answer, START, START + 3 * HOUR), 2.0)

    def test_each_criterion_uses_its_own_covered_window(self):
        body = (BODY + "- [ ] " + LIVE.replace("worker failed", "worker stopped") + "\n").replace("2 hours", "1 hour")
        answers = iter((read_answer("2030-01-01T00:00:00Z", "2030-01-01T01:00:00Z"),
                        read_answer("2030-01-01T02:00:00Z", "2030-01-01T03:00:00Z")))
        outcome = live_proof.prove(body, START,
            live_proof.Sources(source_log=lambda *args: next(answers)), now=START + 3 * HOUR)
        self.assertTrue(outcome.proven)


if __name__ == "__main__":
    unittest.main()
