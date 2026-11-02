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
from bus.github import GitHub, GitHubError  # noqa: E402
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

    # Real journal phrasings that name another budget, or negate one: never
    # a deferral on the tick's time, so never refused.
    OTHER_BUDGETS = (
        "skipped quadseven/loom after the 120s per-repo budget (LOOM_REPO_BUDGET)",
        "The App installation budget ran out (0 of 5000), deferred brief",
        "GitHub API budget low, deferring sweep",
        "deferred on cap, not budget",
        "budget: 4 of 20 minutes used, no deferral",
    )
    TIME_DEFERRALS = (
        "deferred verify on the 20-minute budget",
        "skipped triage on budget",
        "deferred brief on budget",
        "verify deferred: out of time",
        "asks deferred, tick budget spent",
        "over the 20-minute limit, deferred",
        "time is up, triage left for next tick",
    )

    # loom#2109: real deferrals the first cut let through. A bare "budget"
    # next to a defer word is the likeliest wording around the guard.
    MISSED_DEFERRALS = (
        "deferred: budget",
        "deferred, budget",
        "budget spent, skipping brief",
        "budget exhausted, deferred",
        "skipped for time",
        "ran long, deferring",
        "deferred, not enough time",
        "skipped - tick time up",
        "deferred: 21 minutes elapsed",
    )
    # loom#2109: a per-minute API budget is a count, not the tick's clock.
    PER_MINUTE_BUDGETS = (
        "skipped because the minute budget of the API ran out",
        "over budget on API calls, skipped",
        "deferred, per-minute budget used",
    )

    def test_other_budgets_and_negations_are_not_a_claim(self):
        for note in self.OTHER_BUDGETS:
            with self.subTest(note=note):
                self.assertFalse(tick_clock.claims_budget(note))

    def test_tick_time_deferrals_are_a_claim(self):
        for note in self.TIME_DEFERRALS:
            with self.subTest(note=note):
                self.assertTrue(tick_clock.claims_budget(note))

    def test_bare_budget_and_plain_time_deferrals_are_a_claim(self):
        for note in self.MISSED_DEFERRALS:
            with self.subTest(note=note):
                self.assertTrue(tick_clock.claims_budget(note))

    def test_bare_budget_deferrals_before_20_minutes_are_refused(self):
        _, gh = held_lock()
        run_step(gh, at(0), "lock")
        for note in self.MISSED_DEFERRALS:
            with self.subTest(note=note), self.assertRaises(brain.BudgetRefused):
                run_step(gh, at(8), "verify", note)

    def test_per_minute_api_budgets_are_not_a_claim(self):
        for note in self.PER_MINUTE_BUDGETS + self.OTHER_BUDGETS:
            with self.subTest(note=note):
                self.assertFalse(tick_clock.claims_budget(note))

    def test_a_bare_budget_qualified_by_another_noun_is_not_a_claim(self):
        for noun in ("repo", "API", "App installation", "lane", "token", "review"):
            for note in ("deferred, %s budget spent" % noun,
                         "skipped: the budget for the %s ran out" % noun):
                with self.subTest(note=note):
                    self.assertFalse(tick_clock.claims_budget(note))
        self.assertTrue(tick_clock.claims_budget("deferred review on budget"))
        self.assertTrue(tick_clock.claims_budget("deferred: budget, review next tick"))

    def test_other_budget_notes_at_8_minutes_are_recorded_not_refused(self):
        _, gh = held_lock()
        run_step(gh, at(0), "lock")
        for minute, note in enumerate(self.OTHER_BUDGETS, start=4):
            with self.subTest(note=note):
                out = run_step(gh, at(minute), "status", note).splitlines()
                self.assertTrue(out[0].startswith("elapsed %d of 25 minutes" % minute))
                self.assertEqual(brain.tick_step(gh, REPO)["notes"][-1]["text"], note)

    def test_tick_time_deferrals_at_8_minutes_are_still_refused(self):
        _, gh = held_lock()
        run_step(gh, at(0), "lock")
        for note in self.TIME_DEFERRALS:
            with self.subTest(note=note), self.assertRaises(brain.BudgetRefused):
                run_step(gh, at(8), "verify", note)

    def test_a_refused_step_prints_the_elapsed_line_too(self):
        _, gh = held_lock()
        run_step(gh, at(0), "lock")
        out = io.StringIO()
        args = argparse.Namespace(lock_repo=REPO, name="verify", note="deferred on budget",
                                  holder="muse", show=False)
        with mock.patch.object(brain, "datetime", clock(at(8))), \
                contextlib.redirect_stdout(out), self.assertRaises(brain.BudgetRefused):
            brain._cmd_step(gh, args)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "elapsed 8 of 25 minutes (1020 seconds left)")
        self.assertTrue(lines[1].startswith("BUDGET DEFERRAL REFUSED"))

    def test_an_unread_tick_record_lets_the_defer_go_out(self):
        class Broken:
            requests = 0

            def rest(self, *args, **kwargs):
                raise GitHubError("offline")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            brain._check_defer_budget(Broken(), REPO, "muse", "deferred on budget", at(8))
        self.assertIn("defer not checked against the clock", err.getvalue())

    def test_defer_on_budget_is_refused_under_20_minutes_in_the_holders_tick(self):
        _, gh = held_lock()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0))
        with self.assertRaisesRegex(brain.BudgetRefused, "elapsed 8 of 25"):
            brain._check_defer_budget(gh, REPO, "muse", "deferred on budget", at(8))
        brain._check_defer_budget(gh, REPO, "muse", "deferred on budget", at(20))
        brain._check_defer_budget(gh, REPO, "muse", "waiting on the operator", at(8))
        brain._check_defer_budget(gh, REPO, "peer", "deferred on budget", at(8))


class BoundedTickTests(unittest.TestCase):
    """loom#2469: a lane machine tick bounded by Muse's next slot records its
    own length, and the clock and the budget guard read it."""

    def test_a_bounded_lock_step_records_its_length_and_the_clock_reads_it(self):
        _, gh = held_lock()
        record = brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0), tick_minutes=13)
        self.assertEqual(13, record["tick_minutes"])
        self.assertEqual(13, brain.tick_step(gh, REPO)["tick_minutes"])
        self.assertTrue(tick_clock.elapsed_line(record, at(9)).startswith(
            "elapsed 9 of 13 minutes (240 seconds left): past 8"))
        self.assertIn("budget: spent", tick_clock.budget_status(record, at(8)))
        self.assertIn("refused until 8 minutes", tick_clock.budget_status(record, at(7)))

    def test_a_budget_deferral_in_a_bounded_tick_is_allowed_from_its_own_budget(self):
        _, gh = held_lock()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0), tick_minutes=13)
        with self.assertRaises(brain.BudgetRefused):
            brain.tick_step(gh, REPO, "verify", "deferred verify on budget", holder="muse", now=at(7))
        record = brain.tick_step(gh, REPO, "brief", "deferred brief on budget", holder="muse",
                                 now=at(9))
        self.assertEqual("deferred brief on budget [elapsed 9 of 13 minutes]",
                         record["notes"][-1]["text"])

    def test_a_length_at_or_past_the_default_is_the_default_tick(self):
        _, gh = held_lock()
        record = brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0), tick_minutes=40)
        self.assertNotIn("tick_minutes", record)
        self.assertIn("of 25 minutes", tick_clock.elapsed_line(record, at(1)))

    def test_a_new_lock_step_drops_the_last_ticks_length(self):
        _, gh = held_lock()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0), tick_minutes=13)
        record = brain.tick_step(gh, REPO, "lock", holder="muse", now=at(30))
        self.assertNotIn("tick_minutes", record)

    def test_a_length_under_five_minutes_is_refused(self):
        _, gh = held_lock()
        with self.assertRaises(ValueError):
            brain.tick_step(gh, REPO, "lock", holder="muse", now=at(0), tick_minutes=4)

    def test_an_unreadable_saved_length_reads_as_the_default(self):
        for bad in ("13", 3, True, 25, None):
            with self.subTest(bad=bad):
                saved = {"started_at": "2030-01-01T12:00:00Z", "step": "lock", "step_index": 1,
                         "notes": [], "tick_minutes": bad}
                record = brain._step_record("holder=muse\nloom:tick=" + json.dumps(saved))
                self.assertEqual(25, tick_clock.tick_minutes(record))

    def test_the_cli_takes_the_length(self):
        _, gh = held_lock()
        out = io.StringIO()
        args = argparse.Namespace(lock_repo=REPO, name="lock", note=None, holder="muse",
                                  show=False, tick_minutes=13)
        with mock.patch.object(brain, "datetime", clock(at(0))), contextlib.redirect_stdout(out):
            brain._cmd_step(gh, args)
        self.assertTrue(out.getvalue().startswith("elapsed 0 of 13 minutes"), out.getvalue())


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

    def test_a_report_or_reply_reads_no_tick_record(self):
        class NoReads:
            def rest(self, *args, **kwargs):
                raise AssertionError("a report reads no tick record")
        self.assertIsNone(cli._steps_footer(NoReads(), REPO, "muse",
                                            journal.REPORT_MARKER + "\nReport x, muse\n"))
        self.assertIsNone(cli._steps_footer(NoReads(), REPO, "muse",
                                            "<!-- loom:reply v1 discord=12 -->\nok\n"))


if __name__ == "__main__":
    unittest.main()
