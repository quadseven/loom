"""Escalation skips lanes the station scorecard has demoted."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub

REPO = "example-owner/example-repo"
BRAIN = "example-brain"
BODY = ("## Why\nwhy: the page query is slow for every visitor.\n## What\nwhat\n"
        "## Acceptance criteria\n- [ ] done\n- [ ] tested\n- [ ] live\n## Size\nS\n## Out of scope\n- none")


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class Routed:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[1].split("?")[0]
        self.calls.append((method, path, json.loads(body) if body else None))
        for (want_method, suffix), answer in self.routes.items():
            if method == want_method and path.endswith(suffix):
                return answer
        return reply(200, [] if method == "GET" else {})

    def sent(self, method, suffix):
        return [entry[2] for entry in self.calls
                if entry[0] == method and entry[1].endswith(suffix)]


def event(at):
    return {"event": "labeled", "label": {"name": "loom:failed"}, "created_at": at}


def ticket(model="claude-sonnet-5-5"):
    return {"number": 9, "state": "open", "title": "T",
            "body": ("Model: %s\n\n" % model) + BODY,
            "created_at": "2030-01-01T00:00:00Z",
            "labels": [{"name": name} for name in
                       ("loom:ticket", "lane:claude", "loom:failed")]}


def routes(free, model="claude-sonnet-5-5"):
    lane_rows = "\n".join("| %s | %s | - |" % (lane, "yes" if lane in free else "no")
                          for lane in ("claude", "codex", "codex-2", "anthropic", "anthropic-2"))
    lane_status = "<!-- loom:lanes v1 -->\n| lane | available | heartbeat (UTC) |\n|---|---|---|\n" + lane_rows
    comments = [{"body": lane_status, "created_at": "2030-01-01T00:00:00Z",
                 "user": {"login": BRAIN}}]
    claim = {"body": "Claimed for lane claude (body sha256 0123456789ab)",
             "created_at": "2030-01-01T00:11:00Z", "user": {"login": BRAIN}}
    return {
        ("GET", "/repos/quadseven/loom/issues"): reply(200, [
            {"number": 99, "labels": [{"name": "loom:lanes"}]}]),
        ("GET", "/issues/99/comments"): reply(200, comments),
        ("GET", "/repos/%s/issues/9" % REPO): reply(200, ticket(model)),
        ("GET", "/issues/9/events"): reply(200, [event("2030-01-01T00:10:00Z"),
                                                      event("2030-01-01T00:40:00Z")]),
        ("GET", "/issues/9/comments"): reply(200, [claim]),
        ("GET", "/repos/%s/pulls" % REPO): reply(200, []),
    }


class EscalateDemotedLaneTests(unittest.TestCase):
    def setUp(self):
        brain._LANE_LOAD.clear()
        self.addCleanup(brain._LANE_LOAD.clear)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = os.path.join(self.temp.name, "state", "station")
        os.makedirs(state)
        with open(os.path.join(state, "scorecard.json"), "w") as fh:
            json.dump({"demoted": {"claude": {"at": 1, "why": "test demotion"}}}, fh)

    def run_escalate(self, free, model="claude-sonnet-5-5"):
        fake = Routed(routes(free, model))
        env = {"LOOM_HOME": self.temp.name, "LOOM_REPOS": "",
               "LOOM_SELF_REPO": "quadseven/loom", "LOOM_BRIEF_AUTHORS": BRAIN}
        with mock.patch.dict(os.environ, env, clear=False):
            outcome = brain.escalate(GitHub("token", transport=fake), REPO, 9)
        return fake, outcome

    def test_demoted_claude_is_not_selected_when_status_says_yes(self):
        with mock.patch.object(brain, "_history", return_value=mock.Mock(
                reasons=["failed 2 times"], attempts=2, tried=[], rungs=set(), next_text=lambda: "")):
            fake, outcome = self.run_escalate(("claude", "codex"), "gpt-6-luna")
        self.assertEqual("rung 2 gpt-6-luna on codex", outcome)

    def test_demoted_first_rung_is_skipped_for_free_codex_rung(self):
        with mock.patch.object(brain, "_history", return_value=mock.Mock(
                reasons=["failed 2 times"], attempts=2, tried=[], rungs=set(), next_text=lambda: "")):
            fake, outcome = self.run_escalate(("claude", "codex"))
        self.assertEqual("rung 2 gpt-6-luna on codex", outcome)
        self.assertEqual([{"labels": ["loom:ticket", "loom:briefed", "lane:codex"]}],
                         fake.sent("PUT", "/issues/9/labels"))
