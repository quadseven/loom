"""The station runs each shipped ticket's named live proof (loom#1608).

bus/live_proof.py reads the proof a ticket's live criteria name. These tests
drive it through Station.close_done_tickets with a fake log source: a ticket
whose log strings appear after the deploy, once the window it names has
passed, is verified with a station proof and closed quoting the lines; one
whose proof is not met gets one status line and stays open; a step that
would write never runs.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import evidence, live_proof, proof, station, status  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

import test_bus_station as tbs  # noqa: E402

REPO = tbs.REPO
HOUR = 3600
LOG_BODY = ("Difficulty: easy\n\n## Why\nBecause.\n\n## Acceptance criteria\n"
            "- [ ] A test passes\n"
            "- [ ] Live: within 24 hours of the deploy the log prints `daily status ready`\n")
WRITE_BODY = LOG_BODY + "- [ ] Live: `kubectl delete pod web-0` brings the pod back ready\n"


def stamp(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


class FakeLog:
    """A log source: the lines it holds that contain the literal, recorded
    with the deploy time each read was asked from."""

    def __init__(self, lines):
        self.lines = lines
        self.reads = []

    def __call__(self, literal, since):
        self.reads.append((literal, since))
        return [line for line in self.lines if literal in line]


class StationLiveProofTest(tbs.Base):
    NUMBER = 1

    def setUp(self):
        super().setUp()
        self.fake.private = False
        self.now = float(int(time.time()))

    def ship(self, body, hours_ago):
        """A shipped ticket, its loom:shipped label applied hours_ago, with
        its merged pull request."""
        self.fake.add(self.NUMBER, body=body, state="shipped")
        self.shipped_at = self.now - hours_ago * HOUR
        self.fake.events[self.NUMBER].append(dict(
            tbs.labeled("loom:shipped", "alice-example"), created_at=stamp(self.shipped_at)))
        self.sha = "%040x" % 41
        self.fake.pulls.append({"number": 41, "state": "closed", "merged_at": stamp(self.shipped_at),
                                "merge_commit_sha": self.sha, "body": "Refs %s#1" % REPO,
                                "head": {"ref": "loom/1-fix"}})

    def close_pass(self, log, runner=None):
        st = station.Station(GitHub("token", transport=self.fake), self.settings, now=self.now)
        sources = live_proof.Sources(log=log, gh=st.gh, runner=runner)
        with mock.patch.object(station.Station, "_proof_sources", return_value=sources,
                               create=True):
            st.close_done_tickets()
        return st

    def status_text(self):
        return "\n".join(c["body"] for c in self.fake.comments[self.NUMBER]
                         if status.is_status(c.get("body")))

    def plain_comments(self):
        return [c["body"] for c in self.fake.comments[self.NUMBER]
                if not status.is_status(c.get("body"))]

    def test_a_log_needle_closes_with_the_lines_quoted_once_its_window_has_passed(self):
        self.ship(LOG_BODY, hours_ago=30)
        log = FakeLog(["%s worker: daily status ready" % stamp(self.now - 2 * HOUR),
                       "%s worker: another line" % stamp(self.now - HOUR)])
        self.close_pass(log)
        issue = self.fake.issues[self.NUMBER]
        self.assertEqual((issue["state"], issue.get("state_reason")), ("closed", "completed"))
        self.assertEqual(log.reads, [("daily status ready", self.shipped_at)])
        # The station's proof record, then the close note quoting the line.
        proofs = [c for c in self.plain_comments() if proof.PROOF_MARKER in c]
        self.assertEqual(len(proofs), 1)
        self.assertIn("Verified by: `station`", proofs[0])
        self.assertIn(self.sha, proofs[0])
        # The record carries the quoted line as the live criterion's evidence.
        record = proof.read(proofs[0])
        [live] = [c for c in record["criteria"] if c["criterion"].startswith("Live:")]
        self.assertIn("> %s worker: daily status ready" % stamp(self.now - 2 * HOUR), live["evidence"])
        [note] = [c for c in self.plain_comments() if "Every acceptance criterion has evidence" in c]
        self.assertIn("    > %s worker: daily status ready" % stamp(self.now - 2 * HOUR), note)
        self.assertNotIn("another line", note)
        self.assertIn("- [x] Live: within 24 hours", issue["body"])
        self.assertNotIn("force-close", self.fake.names(self.NUMBER))

    def test_the_log_is_not_read_before_the_window_has_passed(self):
        self.ship(LOG_BODY, hours_ago=3)
        log = FakeLog(["%s worker: daily status ready" % stamp(self.now - HOUR)])
        self.close_pass(log)
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "open")
        self.assertEqual(log.reads, [])
        self.assertIn("its window has not passed", self.status_text())
        self.assertIn("next check %s" % stamp(self.shipped_at + 24 * HOUR), self.status_text())

    def test_an_unmet_proof_gets_one_status_line_and_stays_open(self):
        self.ship(LOG_BODY, hours_ago=30)
        log = FakeLog(["%s worker: another line" % stamp(self.now - HOUR)])
        self.close_pass(log)
        issue = self.fake.issues[self.NUMBER]
        self.assertEqual(issue["state"], "open")
        self.assertNotIn("loom:verified", self.fake.names(self.NUMBER))
        self.assertEqual(self.plain_comments(), [])
        text = self.status_text()
        self.assertEqual(text.count("Live proof missing:"), 1)
        self.assertIn("Live: within 24 hours of the deploy the log prints `daily status ready`", text)
        self.assertIn("no log line since the deploy names `daily status ready`", text)
        self.assertIn("next check %s" % stamp(self.now + live_proof.RETRY), text)
        # The next pass before that time reads nothing and says nothing more.
        self.close_pass(log)
        self.assertEqual(len(log.reads), 1)
        self.assertEqual(self.status_text().count("Live proof missing:"), 1)
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "open")

    def test_a_write_step_never_reaches_the_runner_and_the_ticket_stays_open(self):
        self.ship(WRITE_BODY, hours_ago=30)
        log = FakeLog(["%s worker: daily status ready" % stamp(self.now - 2 * HOUR)])
        runner = mock.Mock(side_effect=AssertionError("a write step ran"))
        with mock.patch("subprocess.run", side_effect=AssertionError("a command ran")):
            self.close_pass(log, runner=runner)
        runner.assert_not_called()
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "open")
        self.assertNotIn("loom:verified", self.fake.names(self.NUMBER))
        text = self.status_text()
        self.assertIn("Live proof missing:", text)
        self.assertIn("`kubectl delete pod web-0` is not a read-only step", " ".join(text.split()))

    def test_a_shipped_ticket_with_no_runnable_proof_is_left_to_the_done_close(self):
        body = LOG_BODY.replace("prints `daily status ready`", "shows the daily line")
        self.ship(body, hours_ago=30)
        log = FakeLog([])
        self.close_pass(log)
        self.assertEqual(log.reads, [])
        self.assertNotIn("Live proof missing:", self.status_text())
        self.assertIn("loom:human", self.fake.names(self.NUMBER))


class EvidenceDropsVerifyNotesTest(unittest.TestCase):
    """A verify run must not read the station's own verify-result lines, which
    quote the Live criterion, as new observations of the string (loom#2052)."""

    NEEDLE = "budget 0 of 5000 left"
    PASS_LINE = "[2030-01-02T03:04:05Z] station: GitHub App budget 0 of 5000 left"
    NOTE = ("[2030-01-02T06:00:00Z] station: verify of quadseven/example issue 1 on lane spark "
            "left it shipped: Missing evidence: no \"budget 0 of 5000 left\" line; "
            "tried again after 6 hours")

    def command(self, *lines):
        return "printf '%s\\n' " + " ".join("'%s'" % line for line in lines)

    def test_a_verify_note_quoting_the_string_is_dropped_and_a_real_line_kept(self):
        command = self.command(self.PASS_LINE, self.NOTE)
        lines = evidence.lines_since(command, self.NEEDLE, 0, 1_900_000_000, 24)
        self.assertEqual(lines, [self.PASS_LINE])
        text, seen, _ = evidence.gather_window({"r": command}, "r", "Live: `%s`" % self.NEEDLE)
        self.assertTrue(seen)
        self.assertIn(self.PASS_LINE, text)
        self.assertNotIn("verify of", text)

    def test_a_verify_note_with_no_time_prefix_is_dropped_too(self):
        bare = self.NOTE.split("] ", 1)[1]
        self.assertTrue(bare.startswith("station: verify of "))
        text, seen, _ = evidence.gather_window(
            {"r": self.command(self.PASS_LINE, bare)}, "r", "Live: `%s`" % self.NEEDLE)
        self.assertTrue(seen)
        self.assertIn(self.PASS_LINE, text)
        self.assertNotIn("verify of", text)

    def test_a_line_that_only_mentions_verify_in_the_middle_is_kept(self):
        timing = "[2030-01-02T03:04:06Z] station: step verify took 12s (budget 0 of 5000 left)"
        lines = evidence.lines_since(self.command(timing), self.NEEDLE, 0, 1_900_000_000, 24)
        self.assertEqual(lines, [timing])


class EvidenceLinesSinceTest(unittest.TestCase):
    """The log source built on the configured evidence command keeps only the
    lines written since the deploy."""

    def test_only_lines_stamped_at_or_after_the_deploy_count(self):
        deploy = 1_900_000_000
        command = ("printf '%s\\n' '" + stamp(deploy - 60) + " ready' '"
                   + stamp(deploy + 60) + " ready' '"
                   + time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(deploy + 120))
                   + ".123+00:00 ready' 'ready with no time' \"since=$LOOM_EVIDENCE_SINCE\"")
        lines = evidence.lines_since(command, "ready", deploy, deploy + 3600, 24)
        self.assertEqual(lines, [stamp(deploy + 60) + " ready",
                                 time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(deploy + 120))
                                 + ".123+00:00 ready"])

    def test_an_unstamped_line_counts_when_the_whole_log_read_is_after_the_deploy(self):
        deploy = 1_900_000_000
        command = "printf 'loom-evidence-window-hours: 2\\nready with no time\\n'"
        self.assertEqual(evidence.lines_since(command, "ready", deploy, deploy + 3 * 3600, 24),
                         ["ready with no time"])
        self.assertEqual(evidence.lines_since(command, "ready", deploy, deploy + 3600, 24), [])

    def test_a_command_that_fails_gives_no_answer(self):
        self.assertIsNone(evidence.lines_since("exit 3", "ready", 0, 1, 24))

    def test_the_station_log_source_runs_the_configured_command(self):
        tmp = tbs.Base("setUp")
        tmp.setUp()
        self.addCleanup(tmp.doCleanups)
        tmp.settings.evidence_cmds = {REPO: "printf '%s\\n' \"$LOOM_EVIDENCE_NEEDLE seen\""}
        st = station.Station(GitHub("token", transport=tmp.fake), tmp.settings,
                             now=time.time())
        tmp.settings.evidence_window_hours = 1
        log = st._proof_sources(REPO).log
        self.assertEqual(log("daily status ready", st.now - 2 * HOUR), ["daily status ready seen"])
        self.assertEqual(log("daily status ready", st.now - 0.5 * HOUR), [])
        self.assertIsNone(station.Station(GitHub("token", transport=tmp.fake), station.Settings(
            tmp.home, [REPO], list(tbs.AUTHORS), list(tbs.OWNERS)))._proof_sources(REPO).log)


if __name__ == "__main__":
    unittest.main()
