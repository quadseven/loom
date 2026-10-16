"""loom#483: a goal with too much unproven work finishes it before Loom
starts more. The brain refuses an ordinary brief when the goal holds more
than LOOM_UNVERIFIED_LIMIT (default 15) tickets at merged or shipped
without a proof; regression briefs are admitted as today."""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub

REPO = "example-owner/example-repo"
BODY = ("## Why\nwhy: the page query is slow for every visitor.\n## What\nwhat\n"
        "## Acceptance criteria\n- [ ] done\n- [ ] tested\n- [ ] live\n## Size\nS\n## Out of scope\n- none")


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class Fake:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, json.loads(body) if body else None))
        return self.replies.pop(0)


def goal():
    return {"number": 1, "id": 101, "state": "open", "title": "Goal",
            "labels": [{"name": "loom:goal"}]}


def ticket(number, *states):
    return {"number": number, "state": "open", "id": 1000 + number,
            "html_url": "https://example.com/t/%d" % number,
            "body": BODY,
            "labels": [{"name": "loom:ticket"}] + [{"name": "loom:" + s} for s in states]}


class UnprovenLimitTests(unittest.TestCase):
    def test_the_limit_defaults_to_15_and_reads_the_environment(self):
        self.assertEqual(15, brain.unverified_limit({}))
        self.assertEqual(3, brain.unverified_limit({"LOOM_UNVERIFIED_LIMIT": "3"}))
        for bad in ("0", "-2", "x", ""):
            self.assertEqual(15, brain.unverified_limit({"LOOM_UNVERIFIED_LIMIT": bad}))

    def brief(self, fake, body=BODY, **kw):
        return brain.brief(GitHub("t", transport=fake), REPO, 1, "Ticket", body, "codex", **kw)

    def test_brief_refuses_a_goal_over_the_unproven_limit(self):
        children = [ticket(10 + n, "merged" if n % 2 else "shipped") for n in range(16)]
        fake = Fake(reply(200, goal()), reply(200, children))
        with self.assertRaisesRegex(ValueError, "16 tickets at merged or shipped without a proof.*limit 15"):
            self.brief(fake)
        self.assertFalse(any(c[0] in ("POST", "PUT", "PATCH") for c in fake.calls))

    def test_brief_admits_a_goal_at_the_unproven_limit(self):
        children = [ticket(10 + n, "merged") for n in range(15)]
        fake = Fake(reply(200, goal()), reply(200, children),
                    reply(201, {"id": 202, "number": 42, "html_url": "https://example.com/t/42"}),
                    reply(201, {}))
        url = self.brief(fake)
        self.assertEqual(url, "https://example.com/t/42")

    def test_a_regression_brief_is_admitted_past_the_unproven_limit(self):
        regressed = ticket(5, "shipped")
        children = [ticket(10 + n, "shipped") for n in range(16)] + [regressed]
        body = BODY.replace("## Why\nwhy", "## Why\nRegresses #5\np95 rose from 40 s to 900 s")
        fake = Fake(reply(200, goal()),      # the goal home
                    reply(200, children),   # its children
                    reply(200, regressed),   # the regressed ticket
                    reply(200, {}),          # PATCH its body
                    reply(200, {}))         # PUT its labels
        url = self.brief(fake, body, regresses=5)
        self.assertEqual(url, "https://example.com/t/5")


class StatusTests(unittest.TestCase):
    def lines(self, children):
        gh = GitHub("t", transport=Fake(reply(200, children)))
        now = datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc)
        return brain._goal_lines([goal()], {"goals": {1: {}}}, now, None, gh, REPO)

    def test_status_shows_each_goals_unproven_count_against_the_limit(self):
        children = [ticket(10 + n, "merged" if n % 2 else "shipped") for n in range(16)]
        text = "\n".join(self.lines(children))
        self.assertIn("unproven work 16/15", text)
        self.assertIn("OVER LIMIT", text)
        self.assertIn("no new build ticket", text)

    def test_a_goal_under_the_limit_shows_the_count_with_no_warning(self):
        children = [ticket(10 + n, "shipped") for n in range(3)]
        text = "\n".join(self.lines(children))
        self.assertIn("unproven work 3/15", text)
        self.assertNotIn("OVER LIMIT", text)


if __name__ == "__main__":
    unittest.main()
