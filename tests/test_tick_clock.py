"""The brain tick's real clock (loom#2105): step times in the tick record,
the elapsed line every step prints first, the budget-deferral guard, and
the journal Steps footer. The tick record lives on the lock commit, so a
stateful fake transport carries it between calls."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import __main__ as bus_main
from bus import brain, tick_clock
from bus.github import GitHub, GitHubError
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
T = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class LockTransport:
    """A lock branch whose one commit message carries the tick record."""

    def __init__(self, message="holder=muse expires=2030-01-01T13:00:00Z"):
        self.message = message
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        self.calls.append((method, path))
        if method == "GET" and "/git/ref/heads/" in path:
            return reply(200, {"object": {"sha": "c1"}})
        if method == "GET" and "/git/commits/" in path:
            return reply(200, {"message": self.message, "tree": {"sha": "tree"}})
        if method == "POST" and path.endswith("/git/commits"):
            self.message = json.loads(body)["message"]
            return reply(201, {"sha": "c2"})
        if method == "PATCH" and "/git/refs/heads/" in path:
            return reply(200, {})
        raise AssertionError("unexpected call %s %s" % (method, path))


def lock_at(start, holder="muse"):
    """A GitHub client whose tick was locked at `start` (a datetime)."""
    saved = {"started_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "step": "lock",
             "step_index": 1, "notes": [],
             "times": [{"step": "lock", "started_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "ended_at": None}]}
    message = ("holder=%s expires=2030-01-01T13:00:00Z\nloom:tick=%s"
               % (holder, json.dumps(saved)))
    return GitHub("t", transport=LockTransport(message))


class TickClockTests(unittest.TestCase):
    def test_minutes_used_rounds_down(self):
        record = {"started_at": "2030-01-01T12:00:00Z"}
        self.assertEqual(tick_clock.minutes_used(record, T + timedelta(minutes=9, seconds=59)), 9)
        self.assertEqual(tick_clock.minutes_used(record, T + timedelta(minutes=20)), 20)
        self.assertEqual(tick_clock.minutes_used({}, T), 0)
        self.assertEqual(tick_clock.elapsed_line(record, T + timedelta(minutes=9)),
                         "elapsed 9 of 25 minutes (960 seconds left)")

    def test_tick_time_deferrals_are_a_claim(self):
        for note in ("deferred verify on budget", "on the 20-minute budget, triage waits",
                     "deferred on the tick budget", "time budget spent, deferring asks",
                     "out of time, deferred brief", "time is up, triage left for next tick",
                     "over the 20-minute limit, deferred"):
            self.assertTrue(tick_clock.is_budget_deferral(note), note)

    def test_other_budgets_and_negations_are_not_a_claim(self):
        for note in ("skipped quadseven/loom after the 120s per-repo budget (LOOM_REPO_BUDGET)",
                     "The App installation budget ran out (0 of 5000), deferred brief",
                     "GitHub API budget low, deferring sweep",
                     "deferred on cap, not budget",
                     "budget: 4 of 20 minutes used, no deferral",
                     "reviewing changes", None, ""):
            self.assertFalse(tick_clock.is_budget_deferral(note), note)

    def test_the_footer_lists_each_steps_minutes(self):
        record = {"times": [
            {"step": "lock", "started_at": "2030-01-01T12:00:00Z",
             "ended_at": "2030-01-01T12:00:10Z"},
            {"step": "status", "started_at": "2030-01-01T12:00:10Z",
             "ended_at": "2030-01-01T12:03:40Z"},
            {"step": "triage", "started_at": "2030-01-01T12:03:40Z", "ended_at": None}]}
        self.assertEqual(tick_clock.step_times_line(record),
                         "- Steps: lock 0m10s, status 3m30s; elapsed 3m40s")
        self.assertIsNone(tick_clock.step_times_line({"times": []}))
        self.assertIsNone(tick_clock.step_times_line({}))


class TickStepClockTests(unittest.TestCase):
    def setUp(self):
        self.gh = GitHub("t", transport=LockTransport())

    def step(self, name, at, note=None):
        return brain.tick_step(self.gh, REPO, name, note, "muse", at)

    def test_each_step_records_its_start_and_end_time(self):
        self.step("lock", T)
        self.step("status", T + timedelta(seconds=10))
        record = self.step("triage", T + timedelta(minutes=3, seconds=40))
        times = record["times"]
        self.assertEqual([e["step"] for e in times], ["lock", "status", "triage"])
        self.assertEqual(times[0]["ended_at"], times[1]["started_at"])
        self.assertEqual(times[1]["ended_at"], "2030-01-01T12:03:40Z")
        self.assertIsNone(times[2]["ended_at"])
        shown = brain.tick_step(self.gh, REPO)
        self.assertEqual(shown["times"], times)

    def test_a_new_lock_step_starts_the_times_over(self):
        self.step("lock", T)
        self.step("status", T + timedelta(minutes=1))
        record = self.step("lock", T + timedelta(minutes=2))
        self.assertEqual([e["step"] for e in record["times"]], ["lock"])
        self.assertEqual(record["notes"], [])

    def test_step_prints_the_elapsed_line_first_and_the_stage_runs(self):
        self.step("lock", T)
        record = self.step("triage", T + timedelta(minutes=9))
        self.assertEqual(record["step"], "triage")
        self.assertEqual(tick_clock.elapsed_line(record, T + timedelta(minutes=9)),
                         "elapsed 9 of 25 minutes (960 seconds left)")

    def test_status_and_resume_print_the_tick_clock(self):
        self.step("lock", datetime.now(timezone.utc))
        out = io.StringIO()
        with redirect_stdout(out):
            brain._print_tick_clock(self.gh, REPO)
        self.assertTrue(out.getvalue().startswith("Tick clock: elapsed 0 of 25 minutes"),
                        out.getvalue())

    def test_a_budget_deferral_at_8_minutes_is_refused_and_flagged(self):
        self.step("lock", T)
        record = self.step("triage", T + timedelta(minutes=8), "deferred verify on budget")
        self.assertTrue(record.get("budget_refused"))
        text = record["notes"][-1]["text"]
        self.assertTrue(text.startswith("refused:"), text)
        self.assertIn("elapsed 8 of 25 minutes", text)
        self.assertIn("deferred verify on budget", text)

    def test_other_budget_notes_at_8_minutes_are_recorded_not_refused(self):
        self.step("lock", T)
        for note in ("skipped quadseven/loom after the 120s per-repo budget (LOOM_REPO_BUDGET)",
                     "The App installation budget ran out (0 of 5000), deferred brief",
                     "GitHub API budget low, deferring sweep",
                     "deferred on cap, not budget",
                     "budget: 4 of 20 minutes used, no deferral"):
            record = self.step("triage", T + timedelta(minutes=8), note)
            self.assertEqual(record["notes"][-1]["text"], note)
            self.assertFalse(record.get("budget_refused"))

    def test_tick_time_deferrals_at_8_minutes_are_still_refused(self):
        self.step("lock", T)
        for note in ("over the 20-minute limit, deferred",
                     "time is up, triage left for next tick"):
            record = self.step("triage", T + timedelta(minutes=8), note)
            self.assertTrue(record.get("budget_refused"), note)

    def test_a_budget_deferral_past_20_minutes_is_recorded_with_the_real_elapsed(self):
        self.step("lock", T)
        record = self.step("triage", T + timedelta(minutes=21), "deferred verify on budget")
        self.assertFalse(record.get("budget_refused"))
        self.assertEqual(record["notes"][-1]["text"],
                         "deferred verify on budget [elapsed 21 of 25 minutes]")

    def test_a_refused_step_prints_the_elapsed_line_too(self):
        self.step("lock", datetime.now(timezone.utc))
        out = io.StringIO()
        args = brain.argparse.Namespace(lock_repo=REPO, name="triage",
                                        note="deferred verify on budget",
                                        holder="muse", show=False)
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as raised:
                brain._cmd_step(self.gh, args)
        self.assertEqual(raised.exception.code, 1)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("elapsed 0 of 25 minutes"), lines)
        self.assertIn("BUDGET DEFERRAL REFUSED", lines)

    def test_the_cli_says_the_refusal_loudly(self):
        self.step("lock", datetime.now(timezone.utc))
        out = io.StringIO()
        args = brain.argparse.Namespace(lock_repo=REPO, name="status", note=None,
                                        holder="muse", show=False)
        with redirect_stdout(out):
            brain._cmd_step(self.gh, args)
        self.assertTrue(out.getvalue().splitlines()[0].startswith("elapsed "), out.getvalue())


class DeferClockTests(unittest.TestCase):
    def args(self, note):
        return brain.argparse.Namespace(stage="verify", item=REPO + "#7", holder="muse",
                                        note=note, lock_repo=REPO)

    def test_defer_on_budget_is_refused_under_20_minutes_in_the_holders_tick(self):
        gh = lock_at(datetime.now(timezone.utc) - timedelta(minutes=8))
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as raised:
                brain._cmd_defer(gh, self.args("deferred on budget"))
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("elapsed 8 of 25 minutes", out.getvalue())
        self.assertIn("BUDGET DEFERRAL REFUSED", out.getvalue())

    def test_an_unread_tick_record_lets_the_defer_go_out(self):
        err = io.StringIO()
        with unittest.mock.patch.object(brain, "tick_step",
                                        side_effect=GitHubError("offline")), \
                unittest.mock.patch.object(brain, "defer", return_value="defer-marker") as sent, \
                redirect_stderr(err):
            brain._cmd_defer(object(), self.args("deferred on budget"))
        self.assertTrue(sent.called)
        self.assertIn("warning", err.getvalue())


class JournalStepsTests(unittest.TestCase):
    def run_journal(self, text, tick_record):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(text)
            path = fh.name
        self.addCleanup(os.unlink, path)
        args = bus_main.argparse.Namespace(command="journal", target=REPO + "#5",
                                           file=path, holder="muse", lock_repo=REPO)
        posted = {}
        with unittest.mock.patch.object(
                bus_main.brain, "tick_step", return_value=tick_record) as read, \
                unittest.mock.patch.object(
                    bus_main.journal, "post",
                    side_effect=lambda gh, repo, num, body, *a, **k: posted.update(body=body)):
            bus_main._run(args, object())
        return posted.get("body"), read

    def test_the_journal_command_puts_the_holders_step_times_last(self):
        record = {"times": [
            {"step": "lock", "started_at": "2030-01-01T12:00:00Z",
             "ended_at": "2030-01-01T12:00:10Z"},
            {"step": "status", "started_at": "2030-01-01T12:00:10Z",
             "ended_at": "2030-01-01T12:03:40Z"}]}
        body, read = self.run_journal("did the work\n", record)
        self.assertTrue(read.called)
        self.assertTrue(body.rstrip("\n").endswith(
            "- Steps: lock 0m10s, status 3m30s; elapsed 3m40s"), body)

    def test_a_report_or_reply_reads_no_tick_record(self):
        for text in ("<!-- loom:report v1 -->\nReport body\n",
                     "<!-- loom:reply v1 discord=123 -->\nReply body\n"):
            body, read = self.run_journal(text, {"times": []})
            self.assertFalse(read.called, text)
            self.assertEqual(body, text)


if __name__ == "__main__":
    unittest.main()
