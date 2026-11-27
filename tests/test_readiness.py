"""The readiness classifier: a ticket is ready by what it says, not by a label."""
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import readiness  # noqa: E402

DAY = 24 * 3600
OURS = ("loom-app",)


def at(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def issue(*labels, assignees=()):
    return {"number": 1, "state": "open", "created_at": at(10 * DAY),
            "labels": [{"name": x} for x in ("loom:ticket",) + labels],
            "assignees": [{"login": x} for x in assignees]}


def judge(ticket, timeline=(), pulls=(), merged=False):
    return readiness.classify(ticket, timeline, pulls, time.time(), DAY, OURS, merged)


class Classify(unittest.TestCase):
    def test_a_plain_ticket_is_ready(self):
        self.assertEqual(judge(issue("loom:queued")).kind, readiness.READY)

    def test_a_stale_claim_is_cleared(self):
        line = [{"event": "labeled", "actor": {"login": "loom-app"},
                 "label": {"name": "claim:loom-app"}, "created_at": at(3 * DAY)}]
        self.assertEqual(judge(issue("claim:loom-app", "wait:ci"), line).kind,
                         readiness.CLEAR_CLAIM)

    def test_a_fresh_claim_is_held(self):
        line = [{"event": "labeled", "actor": {"login": "loom-app"},
                 "label": {"name": "claim:loom-app"}, "created_at": at(3600)}]
        self.assertEqual(judge(issue("claim:loom-app", "wait:ci"), line).kind,
                         readiness.HELD)

    def test_a_stated_wait_keeps_an_own_app_claim_active(self):
        line = [{"event": "labeled", "actor": {"login": "loom-app"},
                 "label": {"name": "claim:loom-app"}, "created_at": at(3 * DAY)},
                {"event": "commented", "user": {"login": "loom-app"},
                 "body": "waiting: CI until " + at(-3600), "created_at": at(3600)}]
        verdict = readiness.classify(issue("claim:loom-app"), line, [], time.time(), DAY,
                                     OURS, own_apps=OURS)
        self.assertEqual(verdict.kind, readiness.HELD)

    def test_a_ticket_with_an_open_pull_request_is_not_ready(self):
        self.assertEqual(judge(issue(), pulls=[at(60)]).kind, readiness.HAS_PR)

    def test_a_ticket_needing_a_person_is_not_ready(self):
        self.assertEqual(judge(issue("needs:human")).kind, readiness.NEEDS_PERSON)

    def test_an_already_done_ticket_is_flagged_not_ready(self):
        self.assertEqual(judge(issue(), merged=True).kind, readiness.DONE)
        self.assertEqual(judge(issue("loom:shipped")).kind, readiness.DONE)

    def test_another_operators_claim_is_held(self):
        self.assertEqual(judge(issue("claim:someone-else")).kind, readiness.HELD)

    def test_loom_ready_is_not_the_ticket_marker(self):
        self.assertNotEqual(readiness.READY_MARKER, "loom:ready")


class Report(unittest.TestCase):
    def test_a_tally_counts_per_repo(self):
        tally = readiness.Tally()
        tally.add("o/r", readiness.Verdict(readiness.READY, ""))
        tally.add("o/r", readiness.Verdict(readiness.READY, ""))
        tally.add("o/r", readiness.Verdict(readiness.CLEAR_CLAIM, ""))
        tally.add("o/r", readiness.Verdict(readiness.DONE, ""))
        self.assertEqual(tally.line("o/r"), "o/r: 2 ready, 1 claims cleared, 1 flagged done")

    def test_the_marker_changes_only_when_it_differs(self):
        ready = readiness.Verdict(readiness.READY, "")
        self.assertEqual(readiness.marker_change(ready, issue()), "add")
        self.assertIsNone(readiness.marker_change(ready, issue(readiness.READY_MARKER)))
        self.assertEqual(readiness.marker_change(
            readiness.Verdict(readiness.HAS_PR, ""), issue(readiness.READY_MARKER)), "remove")


class Command(unittest.TestCase):
    def test_module_command_classifies_a_ticket_from_json(self):
        payload = {"issue": issue("needs:human"), "timeline": [], "pull_updates": [],
                   "now": time.time(), "idle_seconds": DAY, "own_logins": OURS}
        result = subprocess.run([sys.executable, "-B", "-m", "bus.readiness"],
                                input=json.dumps(payload), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"kind": "needs-person", "reason": "a person holds or must answer it"})


if __name__ == "__main__":
    unittest.main()
