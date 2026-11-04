import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import brain, station  # noqa: E402
from test_bus_brain import REPO  # noqa: E402


class StatusRefsPullTests(unittest.TestCase):
    def test_status_includes_refs_pull_on_non_loom_branch(self):
        pull = {"number": 6, "title": "Build fix", "body": "Refs #5",
                "head": {"ref": "muse/5-x"}}
        self.assertTrue(brain._status_ticket_pr(REPO, pull, {5}))
        self.assertEqual(brain._ticket_prs(REPO, [pull]), {5: pull})


class StationRefsPullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = station.Settings(self.tmp.name, [REPO], ["alice-example"], ["example-owner"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_station_links_trusted_refs_pull_before_claiming_ticket(self):
        issue = {"number": 5, "state": "open", "title": "Work", "body": "brief",
                 "labels": [{"name": "loom:ticket"}, {"name": "loom:queued"}]}
        pull = {"number": 8, "state": "open", "body": "Refs #5",
                "user": {"login": "alice-example"},
                "head": {"ref": "muse/5-x"}}
        runner = station.Station(None, self.settings)
        runner.gh = type("GitHub", (), {"api": "https://api.github.com",
            "rest": lambda _self, method, path, body=None: type("Response", (), {
                "data": issue if path.endswith("/issues/5") else {}})(),
            "paginate": lambda _self, path: [pull] if "/pulls?state=open" in path else [],
        })()
        runner._pr_tickets = lambda _repo: []
        runner._issue = lambda _repo, _number: issue
        runner._open_pulls = lambda _repo: [pull]
        runner._paginate = lambda path: [issue] if "issues?labels=loom:ticket" in path else []
        runner._owned_elsewhere = lambda _repo, _ticket: False
        runner._check_trust = lambda _repo, _ticket: (True, "")
        runner._reconcile_repo(REPO)
        self.assertIn("loom:pr", [label["name"] for label in issue["labels"]])
        self.assertNotIn("loom:queued", [label["name"] for label in issue["labels"]])

