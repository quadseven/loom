"""Legacy demotions: entries written before demotion kinds are re-scored."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import scorecard  # noqa: E402

AUDIT_AT = scorecard._ts(scorecard.AUDIT_AT)
NOW = AUDIT_AT + 86400


def quiet(fn, *args, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


def runs(lane, total, bad, at, model="gpt-6-luna"):
    return {
        "%s-%d" % (lane, n): {
            "lane": lane,
            "model": model,
            "at": at,
            "merged": True,
            "review": scorecard.BAD_WORK if n < bad else scorecard.CLEAN,
        }
        for n in range(total)
    }


def card(now=NOW, env=None, state=None):
    return scorecard.Scorecard(state or tempfile.mkdtemp(), now, env=env or {})


class LegacyDemotion(unittest.TestCase):
    def test_kindless_under_threshold_is_lifted(self):
        got = card()
        got.data["records"] = runs("claude", 90, 1, NOW - 60)
        got.data["demoted"]["claude"] = {
            "at": NOW - 3600,
            "why": "3 bad outcomes in 7 days (3 bad-work findings), at or past LOOM_LANE_DEMOTE_AT=3",
        }
        quiet(got.demote)
        self.assertNotIn("claude", got.demoted())

    def test_kindless_over_threshold_gains_bad_outcomes_kind(self):
        got = card()
        got.data["records"] = runs("claude", 5, 3, NOW - 60)
        got.data["demoted"]["claude"] = {
            "at": NOW - 3600,
            "why": "3 bad outcomes in 7 days (3 bad-work findings), at or past LOOM_LANE_DEMOTE_AT=3",
        }
        quiet(got.demote)
        self.assertIn("claude", got.demoted())
        self.assertEqual(got.data["demoted"]["claude"]["kind"], "bad-outcomes")

    def test_kindless_lift_prints_same_line_as_bad_outcomes(self):
        got = card()
        got.data["records"] = runs("claude", 90, 1, NOW - 60)
        got.data["demoted"]["claude"] = {
            "at": NOW - 3600,
            "why": "old",
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got.demote()
        self.assertIn("lifted demotion of lane claude:", out.getvalue())
        self.assertIn("below LOOM_LANE_DEMOTE_AT", out.getvalue())


if __name__ == "__main__":
    unittest.main()
