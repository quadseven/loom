"""A factory incident nobody owns is escalated after 12 hours (loom#3538).

loom#3114 held every ticket of one repo for six days. Its repo was private,
so the cause ticket never filed, no probe ran on an unchanged base, and
nothing named an owner or told anyone. An open base-check incident older
than ESCALATE_AFTER, with no claim label and no commit on its repo's default
branch for as long, files a hard-difficulty fix ticket in the held repo,
writes one Escalated line on the incident, and pages the operator once.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_bus_station as tbs  # noqa: E402
from bus import incident_probe, station  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

DAY = 86400


def _stamp(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


class Escalation(tbs.Base):
    open_incident = tbs.IncidentProbeTest.open_incident
    hold = tbs.IncidentProbeTest.hold
    station = tbs.IncidentProbeTest.station

    def setUp(self):
        super().setUp()
        self.filed = mock.Mock(return_value="https://github.com/%s/issues/501" % tbs.REPO)
        self.paged = mock.Mock(return_value=True)
        self.committed = time.time() - DAY

    def incident(self, age=DAY, private=False, labels=(), body_extra=""):
        self.open_incident(created=_stamp(time.time() - age))
        issue = self.fake.issues[99]
        issue["user"] = {"login": tbs.AUTHORS[0]}
        if private:
            issue["body"] = issue["body"].replace("Repo: %s" % tbs.REPO,
                                                  "Repo: private repo 0123abcd")
        issue["body"] += body_extra
        issue["labels"] += [{"name": name} for name in labels]
        self.hold(12, 13)

    def read(self, passes=2):
        runner = self.station()
        runner._goals = [(tbs.REPO, {"number": 7})]
        commit_at = mock.Mock(side_effect=lambda repo: self.committed)
        with mock.patch.object(station, "FACTORY_REPO", tbs.REPO), \
                mock.patch.object(station.brain, "brief_intake", self.filed), \
                mock.patch.object(runner, "_default_commit_at", commit_at), \
                mock.patch.object(runner, "_page_operator", self.paged):
            entry = {}
            keys = ["%s#12" % tbs.REPO, "%s#13" % tbs.REPO]
            for _ in range(passes):
                runner._read_probed_incident("%s#99" % tbs.REPO, entry, keys)
        return entry

    def test_an_unowned_incident_past_twelve_hours_files_a_hard_ticket_and_pages_once(self):
        self.incident()
        self.read(passes=3)
        self.assertEqual(self.filed.call_count, 1)
        self.assertEqual(self.filed.call_args.args[1:3], (tbs.REPO, 7))
        self.assertEqual(self.filed.call_args.kwargs.get("difficulty"), "hard")
        body = self.fake.issues[99]["body"]
        self.assertIn("Cause: %s#501\n" % tbs.REPO, body)
        self.assertEqual(body.count("\nEscalated: "), 1)
        self.assertEqual(self.paged.call_count, 1)
        line = self.paged.call_args.args[0]
        self.assertIn("%s#99" % tbs.REPO, line)
        self.assertIn("2 tickets", line)

    def test_a_private_repo_incident_is_escalated_without_naming_the_repo(self):
        self.incident(private=True)
        self.read()
        self.assertEqual(self.filed.call_count, 1)
        self.assertEqual(self.filed.call_args.args[1], tbs.REPO)
        body = self.fake.issues[99]["body"]
        self.assertIn("Cause: issue 501 of the held private repo\n", body)
        self.assertNotIn(tbs.REPO + "#501", body)
        self.assertNotIn(tbs.REPO, self.paged.call_args.args[0].replace("%s#99" % tbs.REPO, ""))

    def test_a_named_cause_is_not_filed_again_but_the_incident_still_escalates(self):
        self.incident(body_extra="Cause: a#1\n")
        self.read()
        self.assertEqual(self.filed.call_count, 0)
        self.assertEqual(self.fake.issues[99]["body"].count("\nEscalated: "), 1)
        self.assertEqual(self.paged.call_count, 1)

    def test_a_young_claimed_escalated_or_fresh_base_incident_is_left_alone(self):
        cases = (dict(age=6 * 3600), dict(labels=("claim:claude-code",)),
                 dict(body_extra="Escalated: 2030-01-01T00:00:00Z\n"), dict(fresh=True))
        for case in cases:
            self.filed.reset_mock()
            self.paged.reset_mock()
            fresh = case.pop("fresh", False)
            self.committed = time.time() - (3600 if fresh else DAY)
            self.incident(**case)
            self.read()
            self.assertEqual(self.filed.call_count, 0, case)
            self.assertEqual(self.paged.call_count, 0, case)

    def test_an_unread_base_commit_escalates_nothing(self):
        self.committed = None
        self.incident()
        self.read()
        self.assertEqual(self.filed.call_count, 0)
        self.assertEqual(self.paged.call_count, 0)

    def test_a_failed_page_is_sent_on_a_later_read_and_then_not_again(self):
        self.incident()
        self.paged.return_value = False
        entry = self.read(passes=1)
        self.assertEqual(self.paged.call_count, 1)
        self.paged.return_value = True
        runner = self.station()
        runner._goals = [(tbs.REPO, {"number": 7})]
        with mock.patch.object(station, "FACTORY_REPO", tbs.REPO), \
                mock.patch.object(station.brain, "brief_intake", self.filed), \
                mock.patch.object(runner, "_default_commit_at", lambda repo: self.committed), \
                mock.patch.object(runner, "_page_operator", self.paged):
            for _ in range(2):
                runner._read_probed_incident("%s#99" % tbs.REPO, entry,
                                             ["%s#12" % tbs.REPO, "%s#13" % tbs.REPO])
        self.assertEqual(self.paged.call_count, 2)
        self.assertEqual(self.filed.call_count, 1)


class Unowned(unittest.TestCase):
    def test_the_rule_reads_age_claim_and_marker(self):
        now = 10 * DAY
        issue = {"state": "open", "created_at": _stamp(now - DAY), "labels": [],
                 "body": "x\n"}
        self.assertTrue(incident_probe.unowned(issue, now))
        self.assertFalse(incident_probe.unowned(dict(issue, created_at=_stamp(now - 3600)), now))
        self.assertFalse(incident_probe.unowned(
            dict(issue, labels=[{"name": "claim:someone"}]), now))
        self.assertFalse(incident_probe.unowned(dict(issue, body="Escalated: then\n"), now))


if __name__ == "__main__":
    unittest.main()
