"""Unit tests for bus/metrics.py daily outcome measurements."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus.metrics import Totals, escalation_number, quality_number


class MetricsTest(unittest.TestCase):
    def test_escalation_number_names_ticket_and_model(self):
        failures = {
            "example-org/repo#42": {"claude-sonnet-5-5": 2},
        }
        num = escalation_number(failures)
        self.assertFalse(num.passed)
        line = num.line()
        self.assertIn("example-org/repo#42", line)
        self.assertIn("claude-sonnet-5-5", line)

    def test_escalation_number_passes_when_no_repeat_failures(self):
        failures = {
            "example-org/repo#42": {"claude-sonnet-5-5": 1},
            "example-org/repo#43": {"gpt-4": 1},
        }
        num = escalation_number(failures, left_out=1)
        self.assertTrue(num.passed)
        self.assertEqual(
            num.value,
            "0 tickets failed twice on one model doing the work; 1 failures left out as limit or transient",
        )

    def test_escalation_number_caps_past_five(self):
        failures = {
            f"example-org/repo#{i}": {"model-a": 2}
            for i in range(1, 8)
        }
        num = escalation_number(failures)
        self.assertFalse(num.passed)
        line = num.line()
        self.assertIn("and 2 more", line)
        for i in range(1, 6):
            self.assertIn(f"example-org/repo#{i}", line)
        self.assertNotIn("example-org/repo#6", line)
        self.assertNotIn("example-org/repo#7", line)

    def test_quality_number_names_fix_pr_without_test(self):
        total = Totals(fix_no_test=["example-org/repo#523"])
        num = quality_number(total)
        self.assertFalse(num.passed)
        line = num.line()
        self.assertIn("example-org/repo#523", line)
        self.assertIn("1 fix PRs without tests", line)

    def test_quality_number_cap_prints_and_n_more_past_five(self):
        prs = [f"example-org/repo#{i}" for i in range(1, 7)]
        total = Totals(fix_no_test=prs)
        num = quality_number(total)
        self.assertFalse(num.passed)
        line = num.line()
        self.assertIn("and 1 more", line)
        for i in range(1, 6):
            self.assertIn(f"example-org/repo#{i}", line)
        self.assertNotIn("example-org/repo#6", line)

    def test_quality_number_passes_when_all_zero(self):
        total = Totals()
        num = quality_number(total)
        self.assertTrue(num.passed)
        self.assertEqual(
            num.value,
            "0 unfixed regressions >2h; 0 unreviewed merges; 0 fix PRs without tests",
        )

    def test_quality_number_names_unchecked_and_regressions(self):
        total = Totals(
            regressions=["example-org/repo#10"],
            unchecked=["example-org/repo#20"],
        )
        num = quality_number(total)
        self.assertFalse(num.passed)
        line = num.line()
        self.assertIn("example-org/repo#10", line)
        self.assertIn("example-org/repo#20", line)

    def test_quality_number_supports_integer_counts_without_refs(self):
        total = Totals(regressions=1, unchecked=0, fix_no_test=0)
        num = quality_number(total)
        self.assertFalse(num.passed)
        self.assertEqual(
            num.value,
            "1 unfixed regressions >2h; 0 unreviewed merges; 0 fix PRs without tests",
        )


if __name__ == "__main__":
    unittest.main()
