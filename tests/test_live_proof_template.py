"""Adapter evidence for templated Live strings."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import live_proof  # noqa: E402

DEPLOYED = 1_800_000_000.0
HOUR = 3600.0
TEMPLATE = "bridge up: commit <sha>, previous <sha>"
FIXED_ONLY = "bridge up: commit abc123 but previous state unknown"
FULL_MATCH = "bridge up: commit abc123, previous def456"


def ticket(live: str) -> str:
    return "## Why\n\nWork.\n\n## Acceptance criteria\n\n- [ ] A test covers it.\n- [ ] %s\n\nSize: S\n" % live


def prove_with(answer: live_proof.Answer, live: str) -> live_proof.Outcome:
    return live_proof.prove(
        ticket(live), DEPLOYED,
        live_proof.Sources(source_log=lambda source, needle, since: answer),
        now=DEPLOYED + 30 * HOUR,
    )


class TemplatedAdapterPresence(unittest.TestCase):
    def test_fixed_piece_only_does_not_prove_presence(self):
        outcome = prove_with(live_proof.Answer([FIXED_ONLY], hours=24, count=1),
                             "Live: after the deploy, `log:worker` shows `%s`." % TEMPLATE)

        self.assertFalse(outcome.proven)

    def test_full_template_match_proves_presence(self):
        outcome = prove_with(live_proof.Answer([FULL_MATCH], hours=24, count=1),
                             "Live: after the deploy, `log:worker` shows `%s`." % TEMPLATE)

        self.assertTrue(outcome.proven, outcome.missing)

    def test_count_without_enough_sample_lines_is_unproven(self):
        outcome = prove_with(live_proof.Answer([], hours=24, count=5),
                             "Live: after the deploy, `log:worker` shows `%s`." % TEMPLATE)

        self.assertFalse(outcome.proven)
        self.assertIn("sample", outcome.missing[0][1].lower())

    def test_count_exceeding_sample_lines_is_unproven_even_with_a_match(self):
        outcome = prove_with(live_proof.Answer([FULL_MATCH], hours=24, count=5),
                             "Live: after the deploy, `log:worker` shows `%s`." % TEMPLATE)

        self.assertFalse(outcome.proven)
        self.assertIn("sample", outcome.missing[0][1].lower())


class TemplatedAdapterAbsence(unittest.TestCase):
    def test_fixed_piece_only_does_not_fail_absence(self):
        outcome = prove_with(live_proof.Answer([FIXED_ONLY], hours=30, count=1),
                             "Live: over 24 hours after the deploy, `log:worker` never shows `%s`."
                             % TEMPLATE)

        self.assertTrue(outcome.proven, outcome.missing)


class LiteralAdapterCount(unittest.TestCase):
    def test_non_templated_string_uses_adapter_count(self):
        outcome = prove_with(live_proof.Answer([], hours=24, count=7),
                             "Live: after the deploy, `log:worker` shows `bridge up: done`.")

        self.assertTrue(outcome.proven, outcome.missing)
        self.assertIn("named 7 times", next(iter(outcome.evidence.values())))


if __name__ == "__main__":
    unittest.main()
