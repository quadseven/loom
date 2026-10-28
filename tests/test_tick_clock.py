"""The brain tick's clock (loom#2104): step times, the elapsed line, the
budget guard and the journal's Steps line."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import __main__ as cli  # noqa: E402
from bus import brain, journal, tick_clock  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_bus import REPO, FakeGit, Scripted, reply  # noqa: E402

T = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(minutes, seconds=0):
    return T + timedelta(minutes=minutes, seconds=seconds)


def clock(when):
    """bus.brain's and bus.__main__'s datetime with now() fixed at `when`."""
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return when
    return Fixed


def held_lock():
    git = FakeGit()
    git.seed_lock("muse", "2030-01-01T13:00:00Z")
    return git, GitHub("tok", transport=git)


def run_step(gh, when, name, note=None):
    out = io.StringIO()
    args = argparse.Namespace(lock_repo=REPO, name=name, note=note, holder="muse", show=False)
    with mock.patch.object(brain, "datetime", clock(when)), contextlib.redirect_stdout(out):
        brain._cmd_step(gh, args)
    return out.getvalue()


class StepTimesTests(unittest.TestCase):
    def test_each_step_records_its_start_and_end_time(self):
        _, gh = held_lock()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0))
        brain.tick_step(gh, REPO, "status", holder="muse", now=at(0, 20))
        brain.tick_step(gh, REPO, "merge", "two ready", holder="muse", now=at(3))
        brain.tick_step(gh, REPO, "merge", "merged one", holder="muse", now=at(4))
        brain.tick_step(gh, REPO, "verify", holder="muse", now=at(6))
        record = brain.tick_step(gh, REPO)
        self.assertEqual(record["times"], [
            {"step": "lock", "started_at": "2030-01-01T12:00:00Z", "ended_at": "2030-01-01T12:00:20Z"},
            {"step": "status", "started_at": "2030-01-01T12:00:20Z", "ended_at": "2030-01-01T12:03:00Z"},
            {"step": "merge", "started_at": "2030-01-01T12:03:00Z", "ended_at": "2030-01-01T12:06:00Z"},
            {"step": "verify", "started_at": "2030-01-01T12:06:00Z", "ended_at": None}])
        brain.tick_step(gh, REPO, "unlock", holder="muse", now=at(8))
        times = brain.tick_step(gh, REPO)["times"]
        self.assertEqual(times[-2]["ended_at"], "2030-01-01T12:08:00Z")
        self.assertEqual(times[-1], {"step": "unlock", "started_at": "2030-01-01T12:08:00Z",
                                     "ended_at": "2030-01-01T12:08:00Z"})

    def test_a_new_lock_step_starts_the_times_over(self):
        _, gh = held_lock()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0))
        brain.tick_step(gh, REPO, "status", holder="muse", now=at(1))
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(30))
        self.assertEqual([t["step"] for t in brain.tick_step(gh, REPO)["times"]], ["lock"])

    def test_a_record_written_before_the_times_reads_with_none(self):
        saved = {"started_at": "2030-01-01T12:00:00Z", "step": "merge", "step_index": 3,
                 "notes": []}
        record = brain._step_record("holder=muse\nloom:tick=" + json.dumps(saved))
        self.assertEqual(record["times"], [])


class ElapsedLineTests(unittest.TestCase):
    def test_step_prints_the_elapsed_line_first_and_the_stage_runs(self):
        _, gh = held_lock()
        run_step(gh, at(0), "lock")
        out = run_step(gh, at(9), "triage").splitlines()
        self.assertEqual(out[0], "elapsed 9 of 25 minutes (960 seconds left)")
        self.assertEqual(json.loads(out[1])["step"], "triage")

    def test_the_minutes_round_down_so_a_printed_twenty_is_real(self):
        record = {"started_at": "2030-01-01T12:00:00Z"}
        self.assertEqual(tick_clock.elapsed_line(record, at(19, 59)),
                         "elapsed 19 of 25 minutes (301 seconds left)")
        self.assertTrue(tick_clock.elapsed_line(record, at(20)).startswith(
            "elapsed 20 of 25 minutes (300 seconds left): past 20"))
        self.assertIn("the tick is over", tick_clock.elapsed_line(record, at(26)))
        self.assertIn("no tick start recorded", tick_clock.elapsed_line({}, at(1)))

    def test_status_and_resume_print_the_tick_clock(self):
        _, gh = held_lock()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0))
        out = io.StringIO()
        with mock.patch.object(brain, "datetime", clock(at(4))), contextlib.redirect_stdout(out):
            brain._print_clock(gh, REPO)
        self.assertEqual(out.getvalue(), "Tick clock: elapsed 4 of 25 minutes (1260 seconds left)\n")

    def test_step_budget_says_whether_a_budget_deferral_is_allowed(self):
        record = {"started_at": "2030-01-01T12:00:00Z"}
        self.assertIn("budget: open, do the step", tick_clock.budget_status(record, at(8)))
        self.assertIn("budget: spent", tick_clock.budget_status(record, at(21)))
        self.assertIn("step-budget", brain.COMMANDS)


class BudgetGuardTests(unittest.TestCase):
    def test_a_budget_deferral_at_8_minutes_is_refused_and_flagged(self):
        _, gh = held_lock()
        run_step(gh, at(0), "lock")
        with self.assertRaises(brain.BudgetRefused) as caught:
            run_step(gh, at(8), "verify", "deferred verify on the 20-minute budget")
        self.assertIn("elapsed 8 of 25 minutes", str(caught.exception))
        record = brain.tick_step(gh, REPO)
        # The step is recorded, and its note says it was refused.
        self.assertEqual(record["step"], "verify")
        self.assertTrue(record["notes"][-1]["text"].startswith(
            "refused: the note defers on budget, but elapsed 8 of 25 minutes"),
            record["notes"][-1]["text"])

    def test_the_cli_says_the_refusal_loudly(self):
        _, gh = held_lock()
        run_step(gh, at(0), "lock")
        out = io.StringIO()
        args = argparse.Namespace(lock_repo=REPO, name="triage", note="skipped triage on budget",
                                  holder="muse", show=False)
        with mock.patch.object(brain, "datetime", clock(at(8))), \
                contextlib.redirect_stdout(out), self.assertRaises(brain.BudgetRefused):
            brain._cmd_step(gh, args)
        self.assertIn("BUDGET DEFERRAL REFUSED", out.getvalue())

    def test_a_budget_deferral_past_20_minutes_is_recorded_with_the_real_elapsed(self):
        _, gh = held_lock()
        run_step(gh, at(0), "lock")
        out = run_step(gh, at(21), "brief", "deferred brief on budget").splitlines()
        self.assertTrue(out[0].startswith("elapsed 21 of 25 minutes"))
        note = brain.tick_step(gh, REPO)["notes"][-1]["text"]
        self.assertEqual(note, "deferred brief on budget [elapsed 21 of 25 minutes]")

    def test_a_note_that_only_counts_a_budget_is_not_a_deferral(self):
        self.assertFalse(tick_clock.claims_budget("lane budget 3 of 5 slots filled"))
        self.assertTrue(tick_clock.claims_budget("verify deferred: out of time"))

    def test_defer_on_budget_is_refused_under_20_minutes_in_the_holders_tick(self):
        _, gh = held_lock()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0))
        with self.assertRaisesRegex(brain.BudgetRefused, "elapsed 8 of 25"):
            brain._check_defer_budget(gh, REPO, "muse", "deferred on budget", at(8))
        brain._check_defer_budget(gh, REPO, "muse", "deferred on budget", at(20))
        brain._check_defer_budget(gh, REPO, "muse", "waiting on the operator", at(8))
        brain._check_defer_budget(gh, REPO, "peer", "deferred on budget", at(8))


class JournalFooterTests(unittest.TestCase):
    def test_the_footer_lists_each_steps_minutes(self):
        record = {"started_at": "2030-01-01T12:00:00Z", "times": [
            {"step": "lock", "started_at": "2030-01-01T12:00:00Z", "ended_at": "2030-01-01T12:00:10Z"},
            {"step": "status", "started_at": "2030-01-01T12:00:10Z", "ended_at": "2030-01-01T12:03:40Z"},
            {"step": "journal", "started_at": "2030-01-01T12:03:40Z", "ended_at": None}]}
        self.assertEqual(tick_clock.footer(record, at(8)),
                         "- Steps: lock 0m10s, status 3m30s, journal 4m20s (running); "
                         "elapsed 8 of 25 minutes (1020 seconds left)")
        self.assertIsNone(tick_clock.footer({"started_at": "2030-01-01T12:00:00Z"}, at(8)))

    def test_the_journal_command_puts_the_holders_step_times_last(self):
        _, gh = held_lock()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0))
        brain.tick_step(gh, REPO, "merge", holder="muse", now=at(2))
        brain.tick_step(gh, REPO, "journal", holder="muse", now=at(7))
        with mock.patch.object(cli, "datetime", clock(at(8))):
            line = cli._steps_footer(gh, REPO, "muse")
            self.assertIsNone(cli._steps_footer(gh, REPO, "peer"))
        self.assertEqual(line, "- Steps: lock 2m00s, merge 5m00s, journal 1m00s (running); "
                               "elapsed 8 of 25 minutes (1020 seconds left)")
        posting = Scripted(reply(200, {"login": "me"}), reply(200, []), reply(201, {}))
        journal.post(GitHub("tok", transport=posting), REPO, 7,
                     journal.JOURNAL_MARKER + "\nMoved: one.\n- Steps: stale\n", "muse",
                     now=at(8), steps=line)
        body = json.loads(posting.calls[2][3])["body"].splitlines()
        self.assertEqual(body[-1], line)
        self.assertNotIn("- Steps: stale", body)


if __name__ == "__main__":
    unittest.main()
