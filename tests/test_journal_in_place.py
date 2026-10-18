"""Journals edit in place: one comment per holder, goal home and UTC day
(2026-10-18: a new comment each tick, 24 in 3 hours on 13 goal homes, each
emailed the operator; an edit sends no email)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, handoff, invariants, journal, station  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
M = journal.JOURNAL_MARKER


def tick(at, holder="lane-machine", *lines):
    return "\n".join([M, "Tick %s, %s" % (at, holder)] + list(lines))


class Fake:
    """Answers GET /user, the day's comments read, PATCH and POST."""

    def __init__(self, comments, login="me"):
        self.comments = comments
        self.login = login
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1]
        self.calls.append((method, path, json.loads(body) if body else None))
        if method == "GET" and path == "/user":
            return 200, {}, json.dumps({"login": self.login}).encode()
        if method == "GET" and "/comments" in path:
            return 200, {}, json.dumps(self.comments).encode()
        if method in ("PATCH", "POST"):
            return 200 if method == "PATCH" else 201, {}, b"{}"
        raise AssertionError("unexpected %s %s" % (method, path))

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m in ("PATCH", "POST")]


def comment(ident, body, created, login="me"):
    return {"id": ident, "body": body, "created_at": created, "user": {"login": login}}


class WriterTests(unittest.TestCase):
    def test_a_second_tick_the_same_day_edits_the_first_newest_first(self):
        first = tick("2030-01-01T09:00:00Z", "lane-machine", "- Decided: first")
        fake = Fake([comment(41, first, "2030-01-01T09:00:05Z")])
        second = tick("2030-01-01T10:00:00Z", "lane-machine", "- Decided: second")
        journal.post(GitHub("t", transport=fake), REPO, 7, second)
        [(method, path, body)] = fake.writes()
        self.assertEqual((method, path), ("PATCH", "/repos/%s/issues/comments/41" % REPO))
        text = body["body"]
        self.assertTrue(text.startswith(second))
        self.assertIn(handoff.EARLIER_OPEN, text)
        self.assertLess(text.index("- Decided: second"), text.index("- Decided: first"))
        # A third tick keeps all three, newest first.
        fake.comments = [comment(41, text, "2030-01-01T09:00:05Z")]
        third = tick("2030-01-01T11:00:00Z", "lane-machine", "- Decided: third")
        journal.post(GitHub("t", transport=fake), REPO, 7, third)
        method, path, body = fake.writes()[-1]
        # The day's journal is edited again, never posted anew.
        self.assertEqual((method, path), ("PATCH", "/repos/%s/issues/comments/41" % REPO))
        text = body["body"]
        self.assertTrue(text.startswith(third))
        self.assertEqual(text.count(handoff.EARLIER_OPEN), 1)
        order = [text.index("- Decided: %s" % n) for n in ("third", "second", "first")]
        self.assertEqual(order, sorted(order))

    def test_the_first_tick_of_a_new_utc_day_posts_a_new_comment(self):
        fake = Fake([comment(41, tick("2030-01-01T23:50:00Z"), "2030-01-01T23:50:05Z")])
        journal.post(GitHub("t", transport=fake), REPO, 7, tick("2030-01-02T00:10:00Z"))
        [(method, path, _)] = fake.writes()
        self.assertEqual((method, path), ("POST", "/repos/%s/issues/7/comments" % REPO))

    def test_another_holders_or_another_logins_journal_is_never_edited(self):
        today = "2030-01-01T09:00:05Z"
        fake = Fake([comment(41, tick("2030-01-01T09:00:00Z", "muse"), today),
                     comment(42, tick("2030-01-01T09:30:00Z"), today, login="stranger")])
        journal.post(GitHub("t", transport=fake), REPO, 7, tick("2030-01-01T10:00:00Z"))
        self.assertEqual([m for m, _, _ in fake.writes()], ["POST"])

    def test_the_edited_journal_stays_under_the_comment_limit(self):
        old = tick("2030-01-01T09:00:00Z", "lane-machine", "x" * 40000)
        older = journal.merged(old, tick("2030-01-01T08:00:00Z", "lane-machine", "y" * 30000))
        new = tick("2030-01-01T10:00:00Z", "lane-machine", "z" * 15000)
        body = journal.merged(new, older)
        self.assertLessEqual(len(body), journal.JOURNAL_MAX)
        self.assertTrue(body.startswith(new))
        self.assertIn("x" * 100, body)       # the newer earlier tick is kept
        self.assertNotIn("y" * 100, body)    # the oldest is dropped


class ReaderTests(unittest.TestCase):
    def edited(self, *ticks):
        """A journal comment edited in place through the real writer."""
        body = ticks[0]
        for t in ticks[1:]:
            body = journal.merged(t, body)
        return body

    def test_resume_reads_each_tick_and_the_newest_marker_wins(self):
        item = (REPO, 9)
        body = self.edited(
            tick("2030-01-01T09:00:00Z", "lane-machine",
                 handoff.defer_marker("failed", item, "r1", "lane-machine")),
            tick("2030-01-01T10:00:00Z", "lane-machine", "- Decided: newest", "- Model: m-2",
                 handoff.done_marker("failed", item, "lane-machine")))
        found = handoff.journals([{"body": body, "at": "2030-01-01T09:00:05Z",
                                   "login": "allowed-bot"}], ["allowed-bot"])
        self.assertEqual([j["at"] for j in found], ["2030-01-01T09:00:00Z", "2030-01-01T10:00:00Z"])
        self.assertEqual(handoff.resume_items(handoff.marks(found)), [])
        self.assertEqual(handoff.decided_line(found[-1]["body"]), "newest")
        self.assertEqual(handoff.journal_model(found[-1]["body"]), "m-2")

    def test_the_goal_ledger_takes_the_newest_tick_time(self):
        body = self.edited(tick("2030-01-01T09:00:00Z"), tick("2030-01-01T15:00:00Z"))
        node = {"comments": {"nodes": [{"body": body, "createdAt": "2030-01-01T09:00:05Z",
                                        "author": {"login": "brain-bot"}}]}}
        now = datetime(2030, 1, 1, 16, 0, tzinfo=timezone.utc)
        led = brain.goal_ledger({"number": 1, "created_at": "2029-12-01T00:00:00Z"}, node, now,
                                ["brain-bot"])
        self.assertEqual(led["journal"], "2030-01-01T15:00:00Z")

    def test_two_failed_ticks_inside_one_edited_journal_still_breach(self):
        now = time.time()
        stamp = lambda s: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - s))
        body = self.edited(
            tick(stamp(3600), "lane-machine", "- Step merge: failed GitHub said 502"),
            tick(stamp(600), "lane-machine", "- Step merge: failed GitHub said 502"))
        comments = [{"id": 5, "body": body, "created_at": stamp(3590),
                     "user": {"login": "alice-example"}}]

        def transport(method, url, headers, payload):
            if "/comments" in url:
                return 200, {}, json.dumps(comments).encode()
            return 200, {}, b"[]"

        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], ["alice-example"], ["example-owner"])
            st = station.Station(GitHub("t", transport=transport), settings, now=now)
            journals, _ = st._invariant_comments([(REPO, {"number": 1})])
        self.assertEqual(len(journals), 2)
        breaches = invariants.brain_failing(invariants.Facts(now=now, journals=journals))
        self.assertEqual([b.invariant for b in breaches], ["brain-failing"])


if __name__ == "__main__":
    unittest.main()
