"""A repeating base-check incident files one cause ticket in its repo (loom#2983)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_bus_station as tbs  # noqa: E402
from bus import incident_probe, station  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPEAT = incident_probe.REPEAT_MARKER + (
    "\nStill failing: %d times since 2030-01-01T00:00:00Z, last at 2030-01-02T00:00:00Z.\n")


class CauseTicket(tbs.Base):
    open_incident = tbs.IncidentProbeTest.open_incident
    hold = tbs.IncidentProbeTest.hold
    station = tbs.IncidentProbeTest.station

    def setUp(self):
        super().setUp()
        self.filed = mock.Mock(return_value="https://github.com/%s/issues/501" % tbs.REPO)

    def incident(self, kind="base-check", count=3, body_extra=""):
        author = {"login": tbs.AUTHORS[0]}
        comments = [{"id": 1, "user": author, "body": REPEAT % count,
                     "created_at": "2030-01-01T00:00:00Z"}] if count else []
        self.open_incident(kind=kind, comments=comments)
        self.fake.issues[99]["user"] = author
        self.fake.issues[99]["body"] += body_extra
        self.hold(12)

    def read(self):
        runner = self.station()
        runner._goals = [(tbs.REPO, {"number": 7})]
        with mock.patch.object(station, "FACTORY_REPO", tbs.REPO), \
                mock.patch.object(station.brain, "brief_intake", self.filed):
            for _ in range(2):
                runner._read_probed_incident("%s#99" % tbs.REPO, {}, ["%s#12" % tbs.REPO])

    def test_a_third_repeat_with_no_cause_files_one_ticket_and_writes_the_cause_line(self):
        self.incident()
        self.read()
        self.assertEqual(self.filed.call_count, 1)
        args = self.filed.call_args.args
        self.assertEqual(args[1:3], (tbs.REPO, 7))
        self.assertIn("check: make check (exit 1)", args[4])
        self.assertIn("Cause: %s#501\n" % tbs.REPO, self.fake.issues[99]["body"])

    def test_a_cause_line_or_too_few_repeats_or_another_kind_file_nothing(self):
        for kind, count, extra in (("base-check", 3, "Cause: a#1\n"), ("base-check", 2, ""),
                                   ("environment", 3, "")):
            self.filed.reset_mock()
            self.incident(kind=kind, count=count, body_extra=extra)
            self.read()
            self.assertEqual(self.filed.call_count, 0, (kind, count, extra))


class CauseWriteFails(tbs.Base):
    open_incident = tbs.IncidentProbeTest.open_incident
    hold = tbs.IncidentProbeTest.hold
    station = tbs.IncidentProbeTest.station

    def test_a_failed_cause_line_write_retries_the_write_not_the_ticket(self):
        author = {"login": tbs.AUTHORS[0]}
        comments = [{"id": 1, "user": author, "body": REPEAT % 3,
                     "created_at": "2030-01-01T00:00:00Z"}]
        self.open_incident(comments=comments)
        self.fake.issues[99]["user"] = author
        self.hold(12)
        filed = mock.Mock(return_value="https://github.com/%s/issues/501" % tbs.REPO)
        runner = self.station()
        runner._goals = [(tbs.REPO, {"number": 7})]
        real = runner.gh.rest
        failing = [True]

        def rest(method, path, *args, **kw):
            if method == "PATCH" and path.endswith("/issues/99") and failing[0]:
                failing[0] = False
                raise station.GitHubError(500)
            return real(method, path, *args, **kw)

        with mock.patch.object(station, "FACTORY_REPO", tbs.REPO), \
                mock.patch.object(station.brain, "brief_intake", filed), \
                mock.patch.object(runner.gh, "rest", rest):
            for _ in range(2):
                runner._read_probed_incident("%s#99" % tbs.REPO, {}, ["%s#12" % tbs.REPO])
        self.assertEqual(filed.call_count, 1)
        self.assertIn("Cause: %s#501\n" % tbs.REPO, self.fake.issues[99]["body"])
