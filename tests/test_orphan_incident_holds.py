"""A wait:incident hold with no ledger entry is released from GitHub's labels
(loom#3573).

The cluster station had no factory_held.json: the holds were recorded by the
station that ran on the desktops, and the label outlived the ledger, so 64
queued tickets carried wait:incident and nothing ever released them. The
release pass now reads the holds from GitHub too: a queued wait:incident
ticket with no ledger entry is released once its repo has no open base-check
or contract incident and its default-branch CI is green.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

from bus import brain
from bus import incident_probe
from bus import station
from bus.github import GitHub
from tests import test_bus_station as base
from shadow_env import setUpModule, tearDownModule  # noqa: F401

REPO = base.REPO
FACTORY = "example-owner/factory"
REPO_ID = 4242
SHA = "c" * 40


def incident(number, subject, kind="base-check", state="open"):
    return {"number": number, "state": state,
            "title": "Factory incident: %s validation %s" % (subject, kind),
            "labels": [{"name": "loom:factory"}],
            "body": "%s\n\nRepo: %s\nKind: %s\nTicket: a private repo ticket\n"
                    % (incident_probe.INCIDENT_MARKER, subject, kind)}


class Fake(base.FakeGitHub):
    """The station fake, plus the reads the orphan release makes: the queued
    wait:incident list, the factory repo's open incidents, the repo id, and
    the default head's checks."""

    def __init__(self):
        super().__init__()
        self.open_incidents = []
        self.runs = [{"status": "completed", "conclusion": "success"}]
        self.combined = {"state": "success", "total_count": 1}
        self.named = {}

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        query = unquote(parts.query)
        if method == "GET" and parts.path == "/repos/%s/issues" % FACTORY:
            self.calls.append((method, parts.path + "?" + parts.query))
            if "state=open" in query:
                return base.reply(200, [i for i in self.open_incidents if i["state"] == "open"])
            return base.reply(200, [])
        if method == "GET" and parts.path.startswith("/repos/%s/issues/" % FACTORY):
            number = int(parts.path.rsplit("/", 1)[1])
            found = self.named.get(number)
            return base.reply(200, found) if found else base.reply(404, {"message": "Not Found"})
        if method == "GET" and parts.path == "/repos/%s/issues" % REPO \
                and "labels=loom:queued,wait:incident" in query:
            self.calls.append((method, parts.path + "?" + parts.query))
            return base.reply(200, [i for i in self.issues.values() if i["state"] == "open"
                                    and {"loom:queued", "wait:incident"}
                                    <= set(self.names(i["number"]))])
        if method == "GET" and parts.path == "/repos/%s" % REPO:
            return base.reply(200, {"full_name": REPO, "private": True, "id": REPO_ID,
                                    "default_branch": "main"})
        if method == "GET" and parts.path == "/repos/%s/commits/main" % REPO:
            return base.reply(200, {"sha": SHA})
        if method == "GET" and parts.path == "/repos/%s/commits/%s/check-runs" % (REPO, SHA):
            return base.reply(200, {"total_count": len(self.runs), "check_runs": self.runs})
        if method == "GET" and parts.path == "/repos/%s/commits/%s/status" % (REPO, SHA):
            return base.reply(200, self.combined)
        return super().__call__(method, url, headers, body)


class OrphanHoldTest(base.Base):
    def setUp(self):
        super().setUp()
        self.fake = Fake()

    def hold(self, number=12, body_events=()):
        self.fake.add(number, lane="muse", body=base.PLAIN, state="queued")
        issue = self.fake.issues[number]
        issue["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"},
                           {"name": "wait:incident"}]
        if body_events:
            issue["body"] = base.status.with_body_events(issue["body"], list(body_events))

    def ledger(self, entries):
        path = os.path.join(self.home, "state", "station", "factory_held.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)

    def release(self):
        with mock.patch.object(station, "FACTORY_REPO", FACTORY):
            station.Station(GitHub("token", transport=self.fake),
                            self.settings).release_factory_held()

    def test_an_unrecorded_hold_with_no_incident_and_green_ci_is_released(self):
        self.hold()
        self.release()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "loom:queued"])

    def test_the_label_read_runs_at_most_once_per_interval(self):
        self.hold()
        self.fake.runs = [{"status": "completed", "conclusion": "failure"}]
        self.release()
        self.release()
        reads = [p for _, p in self.fake.calls if "wait:incident" in unquote(p)]
        self.assertEqual(len(reads), 1)

    def test_the_repos_open_base_check_incident_keeps_it_held(self):
        self.hold()
        alias = incident_probe.repo_alias(REPO_ID)
        self.fake.open_incidents = [incident(99, "private repo %s" % alias)]
        self.release()
        self.assertIn("wait:incident", self.fake.names(12))

    def test_an_open_incident_named_by_its_repo_keeps_a_public_repos_hold(self):
        self.hold()
        self.fake.open_incidents = [incident(99, REPO, kind="contract")]
        self.release()
        self.assertIn("wait:incident", self.fake.names(12))

    def test_another_repos_incident_and_an_environment_incident_do_not_hold_it(self):
        self.hold()
        self.fake.open_incidents = [incident(98, "example-owner/other"),
                                    incident(99, REPO, kind="environment")]
        self.release()
        self.assertNotIn("wait:incident", self.fake.names(12))

    def test_red_default_ci_keeps_it_held(self):
        self.hold()
        self.fake.runs = [{"status": "completed", "conclusion": "failure"}]
        self.release()
        self.assertIn("wait:incident", self.fake.names(12))

    def test_a_still_open_incident_the_ticket_names_keeps_it_held(self):
        self.hold(body_events=[("2030-01-01T00:00:00Z",
                                "Lane muse held: factory incident %s#77" % FACTORY)])
        self.fake.named[77] = incident(77, "private repo 00000000")
        self.release()
        self.assertIn("wait:incident", self.fake.names(12))

    def test_a_closed_incident_the_ticket_names_does_not(self):
        self.hold(body_events=[("2030-01-01T00:00:00Z",
                                "Lane muse held: factory incident %s#77" % FACTORY)])
        self.fake.named[77] = incident(77, "private repo 00000000", state="closed")
        self.release()
        self.assertNotIn("wait:incident", self.fake.names(12))

    def test_a_ledger_hold_is_left_to_the_ledger_path(self):
        self.hold()
        self.ledger({"%s#12" % REPO: {"incident": "%s#99" % FACTORY, "at": 1.0}})
        self.fake.named[99] = incident(99, REPO)
        self.release()
        self.assertIn("wait:incident", self.fake.names(12))

    def test_an_operator_claim_keeps_it_held(self):
        self.hold()
        self.fake.issues[12]["labels"].append({"name": "claim:%s" % base.AUTHORS[0]})
        self.release()
        self.assertIn("wait:incident", self.fake.names(12))

    def test_a_check_run_only_repo_with_no_commit_statuses_is_green(self):
        # GitHub answers "pending" with total_count 0 for a commit no status
        # was ever posted on; only check runs gate such a repo.
        self.hold()
        self.fake.combined = {"state": "pending", "total_count": 0}
        self.release()
        self.assertNotIn("wait:incident", self.fake.names(12))


class StatusCountTest(unittest.TestCase):
    """The status file's Held tickets section counts wait:incident tickets
    the station's hold ledger does not know."""

    def ticket(self, number, *names):
        return {"number": number, "state": "open", "title": "T",
                "labels": [{"name": n} for n in ("loom:ticket",) + names]}

    def test_unrecorded_holds_are_counted_and_ledger_holds_are_not(self):
        found = [self.ticket(1, "loom:queued", "wait:incident"),
                 self.ticket(2, "loom:queued", "wait:incident"),
                 self.ticket(3, "loom:queued"),
                 self.ticket(4, "loom:running", "wait:incident")]
        with tempfile.TemporaryDirectory() as home:
            env = {"LOOM_HOME": home}
            self.assertEqual(brain.unrecorded_incident_holds(REPO, found, env), 2)
            path = os.path.join(home, "state", "station", "factory_held.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"%s#1" % REPO: {"incident": None, "at": 1.0}}, fh)
            self.assertEqual(brain.unrecorded_incident_holds(REPO, found, env), 1)

    def test_the_held_section_names_the_count(self):
        lines = brain.held_lines({}, [], 3)
        self.assertEqual(lines[0], brain.HELD_HEADING)
        self.assertIn("- 3 queued wait:incident ticket(s) with no entry in the station's "
                      "hold ledger", lines)
        self.assertNotIn("- none", lines)
        self.assertEqual(brain.held_lines({}, []), [brain.HELD_HEADING, "- none"])


class AliasTest(unittest.TestCase):
    def test_the_alias_is_stable_hex_and_differs_per_repo(self):
        self.assertEqual(incident_probe.repo_alias(123), incident_probe.repo_alias(123))
        self.assertRegex(incident_probe.repo_alias(123), r"^[0-9a-f]{8}$")
        self.assertNotEqual(incident_probe.repo_alias(123), incident_probe.repo_alias(124))

    def test_a_bad_id_is_refused(self):
        for bad in (0, -1, True, "12", None):
            with self.assertRaises(ValueError):
                incident_probe.repo_alias(bad)

    def test_subjects_name_the_repo_and_its_alias(self):
        self.assertEqual(incident_probe.subjects(REPO, None), [REPO])
        self.assertEqual(incident_probe.subjects(REPO, 123),
                         [REPO, "private repo %s" % incident_probe.repo_alias(123)])


if __name__ == "__main__":
    unittest.main()
