"""loom#483: a goal with too much unproven work finishes it before Loom
starts more. The brain refuses an ordinary brief when the goal holds more
than LOOM_UNVERIFIED_LIMIT (default 30) tickets shipped without a proof;
regression briefs are admitted as today. A merged ticket waits on a deploy,
not on a verifier, so it never counts (2026-10-17)."""
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
import sub_issue_graphql  # noqa: E402

REPO = "example-owner/example-repo"
BODY = ("## Why\nwhy: the page query is slow for every visitor.\n## What\nwhat\n"
        "## Acceptance criteria\n- [ ] done\n- [ ] tested\n- [ ] live\n## Size\nS\n## Out of scope\n- none")


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


EPIC = 2


class Fake:
    """Replies in order; the reads that resolve open epic #2 under goal #1
    are answered apart and not recorded."""

    def __init__(self, *replies, subs=None):
        self.replies = list(replies)
        self.calls = []
        # Sub-issues status reads in one GraphQL query, by issue number.
        self.subs = dict(subs or {})

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        query = json.loads(body or b"{}").get("query", "") if path == "/graphql" else ""
        if method == "POST" and sub_issue_graphql.asks(query):
            return reply(200, sub_issue_graphql.answer(query, lambda n: self.subs.get(n, [])))
        if method == "GET" and path == "/repos/%s/issues/%d" % (REPO, EPIC):
            return reply(200, {"number": EPIC, "state": "open", "labels": [{"name": "epic"}],
                               "body": "Done when: done.\n\nPart of #1\n"})
        if method == "GET" and (path.startswith("/repos/%s/issues/%d/sub_issues" % (REPO, EPIC))
                                or path.startswith("/repos/%s/issues?labels=loom:ticket&" % REPO)
                                or path.startswith("/repos/%s/issues?labels=epic&state=all" % REPO)):
            return reply(200, [])
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
    def test_the_limit_defaults_to_30_and_reads_the_environment(self):
        self.assertEqual(30, brain.unverified_limit({}))
        self.assertEqual(3, brain.unverified_limit({"LOOM_UNVERIFIED_LIMIT": "3"}))
        for bad in ("0", "-2", "x", ""):
            self.assertEqual(30, brain.unverified_limit({"LOOM_UNVERIFIED_LIMIT": bad}))

    def brief(self, fake, body=BODY, **kw):
        return brain.brief(GitHub("t", transport=fake), REPO, EPIC, "Ticket", body, "codex", **kw)

    def test_brief_refuses_a_goal_over_the_unproven_limit(self):
        children = [ticket(10 + n, "shipped") for n in range(31)]
        fake = Fake(reply(200, goal()), reply(200, children))
        with self.assertRaisesRegex(ValueError, "31 tickets shipped without a proof.*limit 30"):
            self.brief(fake)
        self.assertFalse(any(c[0] in ("POST", "PUT", "PATCH") for c in fake.calls))

    def test_merged_tickets_never_hold_the_limit(self):
        # 2026-10-17: merged tickets pinned two goals over the limit while
        # every GPU lane sat idle; they wait on a deploy, not a verifier.
        children = [ticket(10 + n, "merged") for n in range(60)] + [ticket(90, "shipped")]
        fake = Fake(reply(200, goal()), reply(200, children),
                    reply(201, {"id": 202, "number": 42, "html_url": "https://example.com/t/42"}),
                    reply(201, {}))
        self.assertEqual(self.brief(fake), "https://example.com/t/42")

    def test_brief_admits_a_goal_at_the_unproven_limit(self):
        children = [ticket(10 + n, "shipped") for n in range(30)]
        fake = Fake(reply(200, goal()), reply(200, children),
                    reply(201, {"id": 202, "number": 42, "html_url": "https://example.com/t/42"}),
                    reply(201, {}))
        url = self.brief(fake)
        self.assertEqual(url, "https://example.com/t/42")

    def test_a_regression_brief_is_admitted_past_the_unproven_limit(self):
        regressed = ticket(5, "shipped")
        children = [ticket(10 + n, "shipped") for n in range(31)] + [regressed]
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
        gh = GitHub("t", transport=Fake(subs={1: children}))
        now = datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc)
        return brain._goal_lines([goal()], {"goals": {1: {}}}, now, None, gh, REPO)

    def test_status_shows_each_goals_unproven_count_against_the_limit(self):
        children = [ticket(10 + n, "shipped") for n in range(31)] + [ticket(60, "merged")]
        text = "\n".join(self.lines(children))
        self.assertIn("unproven work 31/30", text)
        self.assertIn("OVER LIMIT", text)
        self.assertIn("no new build ticket", text)

    def test_a_goal_under_the_limit_shows_the_count_with_no_warning(self):
        children = [ticket(10 + n, "shipped") for n in range(3)]
        text = "\n".join(self.lines(children))
        self.assertIn("unproven work 3/30", text)
        self.assertNotIn("OVER LIMIT", text)


if __name__ == "__main__":
    unittest.main()
