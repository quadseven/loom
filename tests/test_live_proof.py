"""bus/live_proof.py: a shipped ticket's named live proof, run read-only.

The tickets are shaped like the shipped ones an overnight sweep found built
but still open (#1608): a Live line naming a log string and a window, a
gh api read, and criteria naming a write step that must never run.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import criteria_gate, live_proof  # noqa: E402

DEPLOYED = 1_800_000_000.0
HOUR = 3600.0
LOG_LIVE = "Live: within 24 hours after the deploy, the log shows `sync: done (3 files)`."


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


class LogProofCloses(unittest.TestCase):
    def test_log_needle_closes_with_quoted_lines_once_the_window_has_passed(self):
        log = FakeLog([(DEPLOYED - HOUR, "sync: done (3 files) before the deploy"),
                       (DEPLOYED + 2 * HOUR, "12:00:01Z sync: done (3 files)"),
                       (DEPLOYED + 3 * HOUR, "12:00:02Z unrelated line")])
        body = ticket("A test covers the sync count.", LOG_LIVE)
        outcome = live_proof.prove(body, DEPLOYED, live_proof.Sources(log=log),
                                   now=DEPLOYED + 25 * HOUR)
        self.assertTrue(outcome.proven)
        self.assertEqual(outcome.missing, [])
        note = live_proof.close_note(outcome)
        self.assertIn("    > 12:00:01Z sync: done (3 files)", note)
        self.assertNotIn("before the deploy", note)
        self.assertNotIn("unrelated", note)
        self.assertEqual(log.calls, [("sync: done (3 files)", DEPLOYED)])
        # The close gate (#1501) agrees: the proof's criterion has evidence.
        keys = [criteria_gate.key(t) for t in outcome.evidence]
        self.assertEqual(criteria_gate.unverified(body, keys), [])

    def test_the_log_is_not_read_before_the_window_has_passed(self):
        log = FakeLog([(DEPLOYED + HOUR, "sync: done (3 files)")])
        outcome = live_proof.prove(ticket(LOG_LIVE), DEPLOYED, live_proof.Sources(log=log),
                                   now=DEPLOYED + 2 * HOUR)
        self.assertFalse(outcome.proven)
        self.assertEqual(log.calls, [])
        self.assertEqual(outcome.next_at, DEPLOYED + 24 * HOUR)

    def test_a_private_line_is_never_quoted(self):
        log = FakeLog([(DEPLOYED + HOUR, "sync: done (3 files) from 192.0.2.7:443")])
        outcome = live_proof.prove(ticket(LOG_LIVE), DEPLOYED, live_proof.Sources(log=log),
                                   now=DEPLOYED + 25 * HOUR)
        self.assertFalse(outcome.proven)
        self.assertNotIn("192.0.2.7", live_proof.status_line(outcome))


class UnmetProofSaysWhatIsMissing(unittest.TestCase):
    def test_one_status_line_names_the_missing_criterion_and_the_next_check(self):
        log = FakeLog([(DEPLOYED + HOUR, "sync: started")])
        now = DEPLOYED + 30 * HOUR
        outcome = live_proof.prove(ticket("A test covers it.", LOG_LIVE), DEPLOYED,
                                   live_proof.Sources(log=log), now=now)
        self.assertFalse(outcome.proven)
        self.assertEqual([t for t, _ in outcome.missing], [LOG_LIVE])
        line = live_proof.status_line(outcome)
        self.assertNotIn("\n", line)
        self.assertIn("Live proof missing: " + LOG_LIVE[:40], line)
        self.assertIn("no log line since the deploy names `sync: done (3 files)`", line)
        self.assertIn("next check 2027-01-16T20:00:00Z", line)
        self.assertEqual(outcome.next_at, now + live_proof.RETRY)

    def test_an_operator_criterion_keeps_it_open_with_no_next_check(self):
        log = FakeLog([(DEPLOYED + HOUR, "sync: done (3 files)")])
        outcome = live_proof.prove(ticket(LOG_LIVE, "The operator signs off on the rollout."),
                                   DEPLOYED, live_proof.Sources(log=log),
                                   now=DEPLOYED + 25 * HOUR)
        self.assertFalse(outcome.proven)
        self.assertIsNone(outcome.next_at)
        self.assertIn("(an operator action); no further check: a person decides.",
                      live_proof.status_line(outcome))

    def test_a_proven_ticket_has_no_status_line(self):
        outcome = live_proof.Outcome(True, {"x": "y"}, [], None)
        self.assertEqual(live_proof.status_line(outcome), "")


class WriteStepsAreRefused(unittest.TestCase):
    WRITES = (
        "Live: `gh api -X POST /repos/o/r/dispatches` answers `204`.",
        "Live: `gh pr merge 12 --squash` merges it.",
        "Live: `kubectl rollout restart deploy/web` brings `Running`.",
        "Live: `kubectl delete pod web-0` leaves `Running`.",
        "Live: `kubectl get secret web -o yaml` shows `token`.",
        "Live: `make deploy` prints `deployed`.",
        "Live: `systemctl restart loom` prints `active`.",
        "Live: `curl -X POST https://example.com/roll` answers `ok`.",
        "Live: `DELETE FROM runs WHERE id = 1` leaves `0` rows.",
        "Live: `SELECT id INTO copy FROM runs` returns `1`.",
        "Live: `SELECT id FROM runs; DROP TABLE runs` returns `1`.",
    )

    def test_every_write_step_is_refused_and_never_runs(self):
        refusing = Refusing(self)
        sources = live_proof.Sources(log=refusing, query=refusing, gh=refusing,
                                     fetch=refusing, runner=refusing)
        for text in self.WRITES:
            with self.subTest(text=text):
                step, why = live_proof.step_of(text)
                self.assertIsNone(step)
                self.assertIn("not", why)
                outcome = live_proof.prove(ticket(text), DEPLOYED, sources,
                                           now=DEPLOYED + 48 * HOUR)
                self.assertFalse(outcome.proven)
                self.assertIsNone(outcome.next_at)
                self.assertIn("read-only", live_proof.status_line(outcome))

    def test_a_hand_built_write_step_is_refused_by_the_runner(self):
        refusing = Refusing(self)
        sources = live_proof.Sources(log=refusing, query=refusing, gh=refusing,
                                     fetch=refusing, runner=refusing)
        for step in (live_proof.Step("kubectl", "kubectl scale deploy/web --replicas=0", (), 0),
                     live_proof.Step("query", "UPDATE runs SET done = 1", (), 0),
                     live_proof.Step("gh", "/repos/o/r -X PATCH", (), 0),
                     live_proof.Step("shell", "rm -rf /", (), 0)):
            with self.subTest(step=step):
                ok, seen = live_proof.run(step, DEPLOYED, sources)
                self.assertFalse(ok)
                self.assertTrue(seen.startswith("refused: "), seen)

    def test_read_only_steps_run(self):
        gh = SimpleNamespace(rest=lambda method, path: SimpleNamespace(
            data={"state": "enabled"}) if method == "GET" else self.fail(method))
        body = ticket("Live: `gh api /repos/o/r/actions/workflows/1` shows `enabled`.",
                      "Live: `SELECT state FROM rolls` returns `done`.")
        sources = live_proof.Sources(gh=gh, query=lambda sql: ["done"])
        outcome = live_proof.prove(body, DEPLOYED, sources, now=DEPLOYED + HOUR)
        self.assertTrue(outcome.proven, outcome.missing)

    def test_plain_log_strings_are_not_commands(self):
        for span in ("make sure the sync ran", "update: applied", "select a lane",
                     "restart skipped"):
            with self.subTest(span=span):
                self.assertEqual(live_proof.refusal(span), "")


if __name__ == "__main__":
    unittest.main()
