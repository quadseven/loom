"""A repo held in shadow does not use up the label pass's per-run budget.

In shadow a removal is a ledger line and is never sent, so the same stuck
issues come first on every pass. If each of them counted against
label_pass.PER_RUN, a shadow repo with that many such issues starved every
repo after it: a ticket holding two state labels there kept both for as
long as the shadow repo held them.
"""
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


def dual(number):
    return {"number": number, "title": "Issue %d" % number, "state": "open", "body": "",
            "user": {"login": "someone"}, "assignees": [],
            "labels": [{"name": "loom:shipped"}, {"name": "loom:failed"}]}


class Fake:
    def __init__(self):
        self.writes = []

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        owner, name, rest = parts.path[len("/repos/"):].split("/", 2)
        repo = "%s/%s" % (owner, name)
        if method != "GET":
            self.writes.append((method, repo, unquote(rest)))
            return 200, {}, b"{}"
        if rest == "issues":
            if parse_qs(parts.query).get("state") == ["closed"]:
                return 200, {}, b"[]"
            count = label_pass.PER_RUN + 1 if repo == SHADOW_REPO else 1
            return 200, {}, json.dumps([dual(n) for n in range(1, count + 1)]).encode()
        if rest.endswith("/events"):
            return 200, {}, json.dumps([
                {"event": "labeled", "actor": {"login": "me"}, "label": {"name": "loom:shipped"}},
                {"event": "labeled", "actor": {"login": "me"}, "label": {"name": "loom:failed"}},
            ]).encode()
        raise AssertionError("unexpected %s %s" % (method, url))


class DualStateTest(unittest.TestCase):
    def run_pass(self, repos, shadow_repos):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = Fake()
        settings = station.Settings(os.path.join(tmp.name, "loom"), repos, ["me"],
                                    ["example-owner"])
        env = {"LOOM_SHADOW": "1", "LOOM_SHADOW_REPOS": shadow_repos,
               "LOOM_SHADOW_LEDGER": os.path.join(tmp.name, "ledger.jsonl")}
        with mock.patch.dict(os.environ, env), mock.patch.object(station, "_say"):
            station.Station(GitHub("t", transport=fake), settings,
                            now=time.time()).clean_labels()
        return fake.writes

    def test_a_shadow_repo_full_of_stuck_issues_does_not_starve_the_next_repo(self):
        writes = self.run_pass([SHADOW_REPO, LIVE_REPO], SHADOW_REPO)
        self.assertEqual([("DELETE", LIVE_REPO, "issues/1/labels/loom:shipped")], [
            w for w in writes if w[1] == LIVE_REPO])
        self.assertEqual([], [w for w in writes if w[1] == SHADOW_REPO])

    def test_the_same_two_labels_in_a_repo_outside_shadow_leave_one(self):
        writes = self.run_pass([LIVE_REPO], SHADOW_REPO)
        self.assertEqual(1, len(writes))


if __name__ == "__main__":
    unittest.main()
