"""Status and resume trust only LOOM_BRIEF_AUTHORS logins (loom#783).

A brain with the setting empty trusted nobody and said so nowhere; and the
status goal query read the Loom App's comments as "<slug>" while the setting
names it "<slug>[bot]", so App-written journals and reports never counted."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, handoff
from bus.github import GitHub, rest_login

REPO = "example-owner/example-repo"
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


class Recorder:
    """A fake transport that records every call and answers none."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        raise AssertionError("unexpected call %s %s" % (method, url))


def goal_node(author, body=handoff.JOURNAL_MARKER + "\nTick x, muse"):
    return {"comments": {"nodes": [{"body": body, "createdAt": "2030-01-01T11:00:00Z",
                                    "author": author}]}}


class TrustListTests(unittest.TestCase):
    def test_status_refuses_an_empty_trust_list_before_any_read(self):
        fake = Recorder()
        with self.assertRaisesRegex(ValueError, "status trusts no login: LOOM_BRIEF_AUTHORS is empty"):
            brain.status(GitHub("t", transport=fake), [REPO], REPO, env={"LOOM_BRIEF_AUTHORS": " , "})
        self.assertEqual([], fake.calls)

    def test_status_names_the_logins_it_trusts(self):
        self.assertEqual("Trusted logins (LOOM_BRIEF_AUTHORS): operator example-app[bot]",
                         brain.trusted_line(["operator", "example-app[bot]"]))


class AppLoginTests(unittest.TestCase):
    def test_a_rest_goal_comment_keeps_the_author_type(self):
        # Status reads goal comments through REST (loom#2464): the App's
        # "<slug>[bot]" reads as the GraphQL Bot "<slug>", a person as a User.
        app = brain._rest_comment_node({"user": {"login": "example-app[bot]", "type": "Bot"}})
        person = brain._rest_comment_node({"user": {"login": "example-app", "type": "User"}})
        self.assertEqual({"__typename": "Bot", "login": "example-app"}, app["author"])
        self.assertEqual("example-app[bot]", rest_login(app["author"]))
        self.assertEqual("example-app", rest_login(person["author"]))

    def test_a_bot_login_reads_in_its_rest_form(self):
        self.assertEqual("example-app[bot]", rest_login({"__typename": "Bot", "login": "example-app"}))
        self.assertEqual("example-app[bot]",
                         rest_login({"__typename": "Bot", "login": "example-app[bot]"}))
        # A person who takes the bare slug is not the App.
        self.assertEqual("example-app", rest_login({"__typename": "User", "login": "example-app"}))
        self.assertEqual("example-app", rest_login({"login": "example-app"}))
        self.assertEqual("", rest_login(None))

    def test_an_app_journal_counts_on_the_goal_ledger(self):
        node = goal_node({"__typename": "Bot", "login": "example-app"})
        led = brain.goal_ledger({"number": 30}, node, NOW, ["example-app[bot]"])
        self.assertEqual("2030-01-01T11:00:00Z", led.get("journal"))

    def test_a_person_with_the_app_slug_does_not_count(self):
        node = goal_node({"__typename": "User", "login": "example-app"})
        led = brain.goal_ledger({"number": 30}, node, NOW, ["example-app[bot]"])
        self.assertNotIn("journal", led)

    def test_status_goal_lines_read_app_handoffs(self):
        marker = handoff.defer_marker("merge", (REPO, 7), "pr-abc", "muse")
        node = goal_node({"__typename": "Bot", "login": "example-app"},
                         handoff.JOURNAL_MARKER + "\nTick x, muse\n" + marker)
        lines = brain._goal_lines([{"number": 30, "title": "Goal",
                                    "created_at": "2030-01-01T00:00:00Z"}],
                                  {"goals": {30: node}}, NOW, ["example-app[bot]"])
        self.assertTrue(any("stage merge: yielded by muse" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
