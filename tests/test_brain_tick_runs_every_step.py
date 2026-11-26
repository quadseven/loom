"""A brain tick with time left runs every step (loom#3593): on 2026-11-25
every lane machine tick ended after merge in 0 to 2 minutes of its 12, so
failed, asks, brief, triage and sweep never ran and the cheap lanes sat idle.
The tick record names the steps not yet run, the journal step is refused
while any is left and the clock is open, and `step-pending` tells the
wrapper to hand the tick back to the agent. Under the Anthropic spend floor
a brief to a non-anthropic lane goes out and one to an anthropic lane is
refused."""
from __future__ import annotations

import io
import os
import sys
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import brain, spend  # noqa: E402
from test_bus_sweep import FakeGH, LOCK_REPO, START  # noqa: E402


def at(minutes: float):
    return START + timedelta(minutes=minutes)


def tick_through_merge(tick_minutes: int = 12) -> FakeGH:
    """The 2026-11-25 tick: the wrapper records lock, the agent status and
    merge, both in the first two minutes."""
    gh = FakeGH({}, tick={"started_at": None, "step": None, "step_index": None, "notes": []})
    brain.tick_step(gh, LOCK_REPO, "lock", holder="muse", now=at(0), tick_minutes=tick_minutes)
    brain.tick_step(gh, LOCK_REPO, "status", holder="muse", now=at(1))
    brain.tick_step(gh, LOCK_REPO, "merge", "merged 0", holder="muse", now=at(2))
    return gh


class PendingStepsTest(unittest.TestCase):
    def test_a_tick_after_merge_has_every_later_step_pending(self):
        record = brain.tick_step(tick_through_merge(), LOCK_REPO)
        self.assertEqual(brain.pending_steps(record),
                         ["verify", "failed", "asks", "brief", "triage", "sweep"])

    def test_a_tick_with_time_left_does_not_end_after_merge(self):
        gh = tick_through_merge()
        with self.assertRaisesRegex(ValueError, "verify, failed, asks, brief, triage, sweep"):
            brain.tick_step(gh, LOCK_REPO, "journal", "done", holder="muse", now=at(3))
        # Refused, not recorded: the tick is still at merge.
        self.assertEqual(brain.tick_step(gh, LOCK_REPO)["step"], "merge")
        verdict, go_on = brain.step_pending(brain.tick_step(gh, LOCK_REPO), at(3))
        self.assertTrue(go_on)
        self.assertIn("python3 -m bus.brain step verify", verdict)

    def test_the_journal_goes_through_once_every_step_ran(self):
        gh = tick_through_merge()
        for minute, step in enumerate(("verify", "failed", "asks", "brief", "triage", "sweep")):
            brain.tick_step(gh, LOCK_REPO, step, "nothing to do", holder="muse",
                            now=at(3 + minute * 0.2), sleep=lambda _: None)
        saved = brain.tick_step(gh, LOCK_REPO, "journal", "posted", holder="muse", now=at(5))
        self.assertEqual(saved["step"], "journal")
        self.assertEqual(brain.pending_steps(saved), [])
        self.assertFalse(brain.step_pending(saved, at(5))[1])

    def test_a_step_refused_for_a_budget_note_stays_pending_until_it_runs(self):
        gh = tick_through_merge()
        with self.assertRaises(brain.BudgetRefused):
            brain.tick_step(gh, LOCK_REPO, "verify", "out of time, skipping verify",
                            holder="muse", now=at(2.5))
        record = brain.tick_step(gh, LOCK_REPO)
        self.assertIn("verify", brain.pending_steps(record))
        brain.tick_step(gh, LOCK_REPO, "verify", "verified 1", holder="muse", now=at(3))
        self.assertNotIn("verify", brain.pending_steps(brain.tick_step(gh, LOCK_REPO)))

    def test_past_the_budget_the_journal_is_allowed_and_the_tick_may_end(self):
        gh = tick_through_merge()
        saved = brain.tick_step(gh, LOCK_REPO, "journal", "posted", holder="muse", now=at(8))
        self.assertEqual(saved["step"], "journal")
        verdict, go_on = brain.step_pending(saved, at(8))
        self.assertFalse(go_on)
        self.assertIn("past", verdict)

    def test_a_record_without_a_timed_lock_step_is_not_held_to_the_order(self):
        # Records written before step times, and ticks recorded without the
        # lock step, keep the old free order.
        gh = FakeGH({}, tick={"started_at": "2030-01-01T12:00:00Z", "step": "merge",
                              "step_index": 3, "notes": []})
        saved = brain.tick_step(gh, LOCK_REPO, "journal", holder="muse", now=at(2))
        self.assertEqual(saved["step"], "journal")

    def test_unlock_is_never_refused(self):
        gh = tick_through_merge()
        self.assertEqual(brain.tick_step(gh, LOCK_REPO, "unlock", holder="muse",
                                         now=at(3))["step"], "unlock")

    def test_step_pending_command_exits_zero_only_when_the_tick_must_go_on(self):
        gh = tick_through_merge()
        args = SimpleNamespace(lock_repo=LOCK_REPO)
        with unittest.mock.patch.object(brain, "datetime") as clock:
            clock.now.return_value = at(3)
            out = io.StringIO()
            with redirect_stdout(out):
                code = brain._cmd_step_pending(gh, args)
        self.assertEqual(code, 0)
        self.assertIn("not run yet: verify", out.getvalue())


LANES_UNDER_FLOOR = "\n".join([
    brain.LANES_MARKER, "Updated 2030-01-01 12:00 UTC", "",
    "| lane | state | heartbeat (UTC) | reason |", "|---|---|---|---|",
    "| anthropic | parked | 12:00 | Anthropic credit below floor |",
    "| anthropic-2 | parked | 12:00 | Anthropic credit below floor |",
    "| opencode-go | serving | 12:00 | Accepting work |",
    "| spark | serving | 12:00 | Accepting work |",
    "", "Anthropic spend: today $0.00; spent in all $95.10; remaining $4.90 of $100.00. "
    "Under the $5.00 floor: the anthropic slots take no work."])


class SpendFloorBriefTest(unittest.TestCase):
    def brief_args(self, lane):
        return SimpleNamespace(epic=7, file="-", lane=lane, difficulty=None, model=None,
                               base=None, not_before=None, refs=None, regresses=None,
                               after=None, priority=None, holder="muse", resolves=None,
                               repairs_incident=None, foundational=False,
                               repo="quadseven/loom", title="A ticket", lanes_repo=LOCK_REPO)

    def run_brief(self, lane):
        filed = []
        with unittest.mock.patch.object(brain, "_lane_status", return_value=LANES_UNDER_FLOOR), \
                unittest.mock.patch.object(brain, "_read", return_value="body"), \
                unittest.mock.patch.object(brain, "_check_brief_difficulty"), \
                unittest.mock.patch.object(brain, "_check_live_criterion"), \
                unittest.mock.patch.object(brain, "_check_test_named"), \
                unittest.mock.patch.object(brain, "brief",
                                           side_effect=lambda *a, **k: filed.append(a[5]) or "url"), \
                redirect_stdout(io.StringIO()):
            brain._cmd_brief(object(), self.brief_args(lane))
        return filed

    def test_under_the_floor_a_brief_to_a_non_anthropic_lane_goes_out(self):
        for lane in ("opencode-go", "spark", "openrouter", "workersai", "zai", "tinker", None):
            self.assertEqual(self.run_brief(lane), [lane], lane)

    def test_under_the_floor_a_brief_to_an_anthropic_lane_is_refused(self):
        for lane in ("anthropic", "anthropic-2", "anthropic-8"):
            with self.assertRaisesRegex(ValueError, "Anthropic credit below floor"):
                self.run_brief(lane)

    def test_the_floor_line_names_only_the_anthropic_slots(self):
        summary = spend.Summary([{"at": 0, "slot": "anthropic", "cost_usd": 96.0}],
                                spend.Budget(usd=100.0), now=10.0)
        line = summary.line()
        self.assertTrue(summary.under_floor())
        self.assertIn("only the anthropic slots", line)
        self.assertIn("every other lane takes briefs", line)


if __name__ == "__main__":
    unittest.main()
