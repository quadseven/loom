"""bus/criteria_gate.py: a criterion naming a live observation is live.

The fixtures are the three acceptance criteria a verify lane passed on source
reading alone: each names something only a running system can show (a line in
a named log, a value that rises day over day, rows that move off a number),
and none says "Live:" or names a test, so `kind` read all three as code.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import criteria_gate  # noqa: E402

LIVE_OBSERVATIONS = (
    "Each gui" "ld's corps line names a cook, and the cook's skill rises in `character_skills` day over day.",
    "A cooking cast is preceded by a campfire cast in the bridge log, and the cast succeeds (no `SPELL_FAILED_REQUIRES_SPELL_FOCUS`).",
    "raidsupply's food lines move off 0 once the cook reaches 250.",
)


class LiveWords(unittest.TestCase):
    def test_each_criterion_naming_a_live_observation_is_live(self):
        for text in LIVE_OBSERVATIONS:
            with self.subTest(text=text):
                self.assertEqual(criteria_gate.kind(text), criteria_gate.LIVE)
