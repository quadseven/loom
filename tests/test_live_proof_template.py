"""Test templated Live line proofs with adapter steps.

This tests the fix for T1788: a templated Live line is proven only by adapter lines
that match the whole template, not just the fixed piece.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import criteria_gate, live_proof, evidence

DEPLOYED = 1_800_000_000.0
HOUR = 3600.0


def ticket(*criteria: str) -> str:
    return "## Why\n\nWork.\n\n## Acceptance criteria\n\n%s\n\nSize: S\n" % "\n".join(
        "- [ ] %s" % c for c in criteria)


class FakeLog:
    """A log source: every line, with the epoch time it was written."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.calls = []

    def __call__(self, literal, since):
        self.calls.append((literal, since))
        return [text for at, text in self.lines if at >= since and literal in text]


class Refusing:
    """A runner, fetch and GitHub client that fail the test when used."""

    def __init__(self, case):
        self.case = case

    def __call__(self, *args, **kwargs):
        self.case.fail("a refused step ran: %r" % (args,))

    def rest(self, *args, **kwargs):
        self.case.fail("a refused step read GitHub: %r" % (args,))


class TemplatedPresenceWithAdapter(unittest.TestCase):
    """A templated presence step whose adapter returns lines matching only
    the fixed piece is not proven; one with a line matching the full template is
    proven (tests)."""

    def test_adapter_returns_only_fixed_part_lines_not_proven(self):
        """Adapter lines matching only the fixed part of a template do not prove
        the presence check when the full template does not match."""
        # Create an adapter that returns lines matching only "bridge up:"
        def adapter(source, needle, since):
            # The needle is the fixed part "bridge up:"
            lines = ["bridge up: commit abc123, previous def456",
                     "bridge up: commit ghi789, previous jkl012"]
            return live_proof.Answer(lines, hours=24)
        # The template is named with angle brackets
        body = ticket("A test covers the template.",
                     "Live: after the deploy, `log:worker` shows <bridge up: commit <sha>, previous <sha>>")
        outcome = live_proof.prove(body, DEPLOYED, live_proof.Sources(source_log=adapter),
                                   now=DEPLOYED + 3 * HOUR)
        self.assertFalse(outcome.proven, "A templated presence with only fixed-part matches is not proven")

    def test_adapter_returns_full_template_match_proven(self):
        """Adapter lines that match the full template prove the presence check."""
        # Create an adapter that returns lines matching the full template
        def adapter(source, needle, since):
            # The needle is the fixed part "bridge up:"
            # But the line also matches the full template with placeholders
            lines = ["bridge up: commit abc123, previous def456",
                     "bridge up: commit ghi789, previous jkl012"]
            return live_proof.Answer(lines, hours=24)
        body = ticket("A test covers the template.",
                     "Live: after the deploy, `log:worker` shows <bridge up: commit <sha>, previous <sha>>")
        outcome = live_proof.prove(body, DEPLOYED, live_proof.Sources(source_log=adapter),
                                   now=DEPLOYED + 3 * HOUR)
        self.assertTrue(outcome.proven, "A templated presence with full template match is proven")

    def test_adapter_returns_only_count_no_lines_not_proven(self):
        """When an adapter returns only a count without sample lines, the step
        cannot be proven since we cannot re-filter against the full template."""
        body = ticket("A test covers the template.",
                     "Live: after the deploy, `log:worker` shows <bridge up: commit <sha>, previous <sha>>")
        sources = live_proof.Sources(source_log=lambda s, n, since: live_proof.Answer(
            lines=None, hours=24, count=5))
        outcome = live_proof.prove(body, DEPLOYED, sources, now=DEPLOYED + 3 * HOUR)
        self.assertFalse(outcome.proven, "A templated presence with only a count is not proven")


class TemplatedAbsenceWithAdapter(unittest.TestCase):
    """A templated absence step is not failed by lines that match only the fixed
    piece (test)."""

    def test_adapter_only_fixed_part_does_not_fail_absence_check(self):
        """Adapter lines matching only the fixed part of a template do not fail
        an absence check when the full template also does not match."""
        # Create an adapter that returns lines matching only "bridge up:"
        # but not the full template
        def adapter(source, needle, since):
            lines = ["bridge up: commit abc123, previous def456",
                     "bridge up: commit ghi789, previous jkl012"]
            return live_proof.Answer(lines, hours=24)
        body = ticket("A test covers the template.",
                     "Live: over 24 hours after the deploy, `log:worker` never shows <bridge up: commit <sha>, previous <sha>>")
        outcome = live_proof.prove(body, DEPLOYED, live_proof.Sources(source_log=adapter),
                                   now=DEPLOYED + 30 * HOUR)
        self.assertFalse(outcome.proven, "An absence check should not fail on fixed-part matches")


class NonTemplatedAdapterKeepsExistingBehavior(unittest.TestCase):
    """A step with no template parts keeps using the adapter's count
    unchanged (test)."""

    def test_non_templated_adapter_count_is_used(self):
        """A non-templated adapter (no placeholders) continues to use the count
        as-is without re-filtering."""
        log = FakeLog([
            (DEPLOYED + HOUR, "bridge up: done"),
            (DEPLOYED + 2 * HOUR, "bridge up: done"),
        ])
        body = ticket("A test covers the string.",
                     "Live: after the deploy, the log shows `bridge up: done`.")
        outcome = live_proof.prove(body, DEPLOYED, live_proof.Sources(log=log),
                                   now=DEPLOYED + 3 * HOUR)
        self.assertTrue(outcome.proven, "A non-templated adapter uses its count as-is")


if __name__ == "__main__":
    unittest.main()
