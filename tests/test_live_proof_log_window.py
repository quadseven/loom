"""Absence proofs cannot exceed the timestamped log window actually read."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bus import live_proof

START = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
LIVE = "Live: over 2 hours after the deploy, `log:worker` never shows `worker failed`."
BODY = "## Acceptance criteria\n- [ ] " + LIVE + "\n"


def result(lines, hours=24):
    return live_proof.prove(
        BODY, START,
        live_proof.Sources(source_log=lambda *args: live_proof.Answer(lines, hours=hours, count=0)),
        now=START + 3 * HOUR,
    )


class ActualLogWindow(unittest.TestCase):
    def test_short_timestamped_read_cannot_use_claimed_long_window(self):
        outcome = result(["2030-01-01T01:00:00Z worker healthy",
                          "unstamped worker healthy",
                          "2030-01-01T01:30:00Z worker healthy"])
        self.assertFalse(outcome.proven)
        self.assertIn("0.5 hours", outcome.missing[0][1])
        self.assertIn("2.0 needed", outcome.missing[0][1])

    def test_single_timestamp_does_not_prove_a_window(self):
        outcome = result(["2030-01-01T01:00:00Z worker healthy"])
        self.assertFalse(outcome.proven)
        self.assertIn("0.0 hours", outcome.missing[0][1])

    def test_predeploy_and_future_lines_do_not_extend_covered_window(self):
        for lines in (
                ["2029-12-31T22:00:00Z worker healthy", "2030-01-01T00:30:00Z worker healthy"],
                ["2030-01-01T02:30:00Z worker healthy", "2030-01-01T05:00:00Z worker healthy"],
                ["2029-12-31T22:00:00Z worker healthy", "2029-12-31T23:00:00Z worker healthy"]):
            with self.subTest(lines=lines):
                self.assertFalse(result(lines).proven)

    def test_timezone_offsets_and_unsorted_lines_use_the_actual_span(self):
        outcome = result(["2030-01-01T02:30:00+01:00 worker healthy",
                          "2030-01-01T01:00:00Z worker healthy"])
        self.assertFalse(outcome.proven)
        self.assertIn("0.5 hours", outcome.missing[0][1])

    def test_epoch_zero_is_a_real_log_timestamp(self):
        step = live_proof.Step(live_proof.LOG, "", (), 2, source="worker", absent=("worker failed",))
        answer = live_proof.Answer(["1970-01-01T00:00:00Z worker healthy"], hours=24, count=0)
        ok, seen = live_proof.run(step, 0, live_proof.Sources(source_log=lambda *args: answer), now=3 * HOUR)
        self.assertFalse(ok)
        self.assertIn("0.0 hours", seen)

    def test_full_window_passes_but_smaller_claim_still_limits_coverage(self):
        lines = ["2030-01-01T00:30:00Z worker healthy", "2030-01-01T02:30:00Z worker healthy"]
        self.assertTrue(result(lines).proven)
        self.assertFalse(result(lines, hours=0.5).proven)


if __name__ == "__main__":
    unittest.main()
