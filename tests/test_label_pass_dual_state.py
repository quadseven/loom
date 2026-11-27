"""Reconcile shipped and failed tickets in and out of shadow."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import label_pass, station  # noqa: E402
from bus.github import GitHub  # noqa: E402

SHADOW_REPO = "example-owner/shadow-repo"
LIVE_REPO = "example-owner/live-repo"
DUAL_STATE = ("loom:shipped", "loom:failed")


def dual(number, labels=DUAL_STATE):
    return {"number": number, "title": "Issue %d" % number, "state": "open", "body": "",
            "user": {"login": "someone"}, "assignees": [],
            "labels": [{"name": label} for label in labels]}


class Fake:
    def __init__(self, fill_shadow):
        self.writes = []
        self.issues = {repo: {1: dual(1)} for repo in (SHADOW_REPO, LIVE_REPO)}
        if fill_shadow:
            self.issues[SHADOW_REPO] = {
                n: dual(n, ("loom:queued", "loom:briefed"))
                for n in range(1, label_pass.PER_RUN + 2)
            }

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        owner, name, rest = parts.path[len("/repos/"):].split("/", 2)
        repo = "%s/%s" % (owner, name)
        if rest == "issues":
            if parse_qs(parts.query).get("state") == ["closed"]:
                return 200, {}, b"[]"
            return 200, {}, json.dumps(list(self.issues[repo].values())).encode()
        _, number, endpoint = rest.split("/", 2)
        issue = self.issues[repo][int(number)]
        if method == "GET" and endpoint == "events":
            events = [{"event": "labeled", "actor": {"login": login}, "label": label}
                      for login, label in zip(("station[bot]", "trusted-brain"), issue["labels"])]
            return 200, {}, json.dumps(events).encode()
        if method == "DELETE" and endpoint.startswith("labels/"):
            name = unquote(endpoint[len("labels/"):])
            issue["labels"].remove({"name": name})
            self.writes.append((method, repo, unquote(rest)))
            return 204, {}, b""
        raise AssertionError("unexpected %s %s" % (method, url))


class DualStateTest(unittest.TestCase):
    def run_pass(self, repos, fill_shadow=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = Fake(fill_shadow)
        settings = station.Settings(os.path.join(tmp.name, "loom"), repos,
                                    ["station[bot]", "trusted-brain"], ["example-owner"])
        env = {"LOOM_SHADOW": "1", "LOOM_SHADOW_REPOS": SHADOW_REPO,
               "LOOM_SHADOW_LEDGER": os.path.join(tmp.name, "ledger.jsonl")}
        with mock.patch.dict(os.environ, env), mock.patch.object(station, "_say"):
            station.Station(GitHub("t", transport=fake), settings,
                            now=time.time()).clean_labels()
        return fake

    def test_a_shipped_and_failed_ticket_in_shadow_leaves_one_state(self):
        fake = self.run_pass([SHADOW_REPO])
        self.assertEqual(["loom:failed"], label_pass.names_of(fake.issues[SHADOW_REPO][1]))

    def test_a_shadow_repo_full_of_stuck_issues_does_not_starve_the_next_repo(self):
        fake = self.run_pass([SHADOW_REPO, LIVE_REPO], fill_shadow=True)
        self.assertEqual([("DELETE", LIVE_REPO, "issues/1/labels/loom:shipped")], fake.writes)

    def test_a_state_removal_sent_in_shadow_counts_toward_the_pass_limit(self):
        with mock.patch.object(label_pass, "PER_RUN", 1):
            fake = self.run_pass([SHADOW_REPO, LIVE_REPO])
        self.assertEqual([("DELETE", SHADOW_REPO, "issues/1/labels/loom:shipped")], fake.writes)

    def test_the_same_two_labels_in_a_repo_outside_shadow_leave_one(self):
        fake = self.run_pass([LIVE_REPO])
        self.assertEqual(["loom:failed"], label_pass.names_of(fake.issues[LIVE_REPO][1]))


if __name__ == "__main__":
    unittest.main()
