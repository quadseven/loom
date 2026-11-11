"""Test that the label pass removes one state label when a ticket holds
both loom:shipped and loom:failed (loom#2999)."""
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

from bus import label_pass, shadow, station  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
STATION_APPS = ["loom-app[bot]"]
TRUSTED_BRIEFERS = ["brain-user", "other-op"]


def issue(number, labels, state="open", body="", parent=None, assignees=()):
    found = {"number": number, "title": "Issue %d" % number, "state": state,
             "body": body, "user": {"login": "someone"},
             "labels": [{"name": x} for x in labels],
             "assignees": [{"login": x} for x in assignees]}
    if parent is not None:
        found["parent_issue_url"] = "https://api.github.com/repos/%s/issues/%d" % (REPO, parent)
    return found


def labeled(name, by):
    return {"event": "labeled", "actor": {"login": by}, "label": {"name": name}}


class Fake:
    def __init__(self):
        self.open, self.closed, self.events, self.writes = {}, {}, {}, []

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        rest = parts.path[len("/repos/%s/" % REPO):]
        query = parse_qs(parts.query)
        if method != "GET":
            self.writes.append((method, unquote(rest)))
            return 200, {}, b"{}"
        if rest == "issues":
            pool = self.closed if query.get("state") == ["closed"] else self.open
            return 200, {}, json.dumps(list(pool.values())).encode()
        if rest.endswith("/events"):
            return 200, {}, json.dumps(self.events.get(int(rest.split("/")[1]), [])).encode()
        if rest.endswith("/comments"):
            return 200, {}, b"[]"
        raise AssertionError("unexpected %s %s" % (method, url))


class DualStateTest(unittest.TestCase):
    """A ticket with both loom:shipped and loom:failed should keep only one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.fake = Fake()
        # Settings with both station apps and trusted briefers
        self.settings = station.Settings(
            os.path.join(tmp.name, "loom"),
            [REPO],
            STATION_APPS + TRUSTED_BRIEFERS,  # authors = station apps + trusted briefers
            ["example-owner"],
            briefers=TRUSTED_BRIEFERS
        )
        # Ticket with both loom:shipped (by station) and loom:failed (by brain)
        self.fake.open[1] = issue(1, ("loom:shipped", "loom:failed"))
        # Events: loom:shipped by station app, then loom:failed by brain user
        self.fake.events[1] = [
            labeled("loom:shipped", "loom-app[bot]"),
            labeled("loom:failed", "brain-user"),
        ]

    def run_clean_labels(self, shadow_on: bool):
        env = {"LOOM_SHADOW": "1" if shadow_on else "0",
               "LOOM_SHADOW_REPOS": REPO if shadow_on else "",
               "LOOM_SHADOW_LEDGER": os.path.join(self.tmp, "ledger.jsonl")}
        with mock.patch.dict(os.environ, env), mock.patch.object(station, "_say") as said:
            station.Station(GitHub("t", transport=self.fake), self.settings,
                            now=time.time()).clean_labels()
        return [c.args[0] for c in said.call_args_list], self.fake.writes

    def test_shadow_repo_keeps_station_shipped_over_brain_failed(self):
        """In a shadow repo, the station's own loom:shipped beats a later
        loom:failed from a trusted briefer."""
        said, writes = self.run_clean_labels(shadow_on=True)

        # In shadow, no writes are sent, but ledger records the removal
        self.assertEqual([], writes)
        ledger = shadow.read(os.path.join(self.tmp, "ledger.jsonl"))
        # Should record removal of loom:failed (not loom:shipped)
        removals = [(e["number"], e["label"]) for e in ledger if e["action"] == "unlabel"]
        self.assertIn((1, "loom:failed"), removals)
        self.assertNotIn((1, "loom:shipped"), removals)

    def test_non_shadow_repo_keeps_station_shipped_over_brain_failed(self):
        """Out of shadow, same logic: station's own write wins, loom:failed removed."""
        said, writes = self.run_clean_labels(shadow_on=False)

        # Should send DELETE for loom:failed
        deletes = [w for w in writes if w[0] == "DELETE"]
        self.assertIn(("DELETE", "issues/1/labels/loom:failed"), deletes)
        # Should NOT send DELETE for loom:shipped
        self.assertNotIn(("DELETE", "issues/1/labels/loom:shipped"), deletes)


if __name__ == "__main__":
    unittest.main()