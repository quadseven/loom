"""The station referees engine claims (bus/claim_referee.py, loom#2277).

Several engines work one backlog and GitHub has no conditional write, so two
could claim one ticket, and a claim held by a stopped engine blocked the
ticket for good. The station keeps the earliest of two engine claims, expires
an engine claim idle for two hours unless a comment states a wait, and never
touches a person's claim.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import claim_referee, station  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
HOUR = 3600
ENGINES = ("claim:engine-a", "claim:engine-b")
STATION = ("loom-app[bot]",)


def at(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def issue(number, labels=("claim:engine-a",), updated=5 * HOUR, created=48 * HOUR):
    return {"number": number, "title": "Issue %d" % number, "state": "open",
            "created_at": at(created), "updated_at": at(updated),
            "user": {"login": "someone"}, "labels": [{"name": x} for x in labels],
            "assignees": []}


def labeled(name, by, ago):
    return {"event": "labeled", "actor": {"login": by}, "label": {"name": name},
            "created_at": at(ago)}


def commented(by, ago, body="a note"):
    return {"event": "commented", "user": {"login": by}, "actor": {"login": by},
            "body": body, "created_at": at(ago)}


def plan(found, line, now=None):
    return claim_referee.plan(found, line, now or time.time(), ENGINES, STATION)


class Duplicates(unittest.TestCase):
    def test_two_engine_claims_keep_the_earliest_and_remove_the_other(self):
        found = issue(1, labels=("claim:engine-b", "claim:engine-a"))
        line = [labeled("claim:engine-a", "engine-a[bot]", 30 * 60),
                labeled("claim:engine-b", "engine-b[bot]", 20 * 60)]
        removed = plan(found, line)
        self.assertEqual(["claim:engine-b"], [r.label for r in removed])
        self.assertTrue(removed[0].duplicate)
        self.assertIn("claim:engine-a", removed[0].why)

    def test_the_earliest_is_the_newest_putting_on_of_each_label(self):
        # engine-a was put on early, taken off and put on again after engine-b.
        found = issue(1, labels=("claim:engine-a", "claim:engine-b"))
        line = [labeled("claim:engine-a", "engine-a[bot]", 50 * 60),
                labeled("claim:engine-b", "engine-b[bot]", 40 * 60),
                labeled("claim:engine-a", "engine-a[bot]", 10 * 60)]
        self.assertEqual(["claim:engine-a"], [r.label for r in plan(found, line)])

    def test_a_claim_with_no_labeled_event_is_left_for_a_later_pass(self):
        found = issue(1, labels=("claim:engine-a", "claim:engine-b"))
        self.assertEqual([], plan(found, [labeled("claim:engine-a", "x", 600)]))

    def test_a_person_claim_beside_an_engine_claim_is_no_duplicate(self):
        found = issue(1, labels=("claim:engine-a", "claim:a-person"))
        line = [labeled("claim:a-person", "a-person", 50 * 60),
                labeled("claim:engine-a", "engine-a[bot]", 10 * 60)]
        self.assertEqual([], plan(found, line))


class Expiry(unittest.TestCase):
    def test_an_idle_engine_claim_expires(self):
        line = [labeled("claim:engine-a", "engine-a[bot]", 3 * HOUR),
                labeled("loom:queued", "loom-app[bot]", 30 * 60)]
        removed = plan(issue(1), line)
        self.assertEqual(["claim:engine-a"], [r.label for r in removed])
        self.assertFalse(removed[0].duplicate)

    def test_recent_activity_keeps_the_claim(self):
        line = [labeled("claim:engine-a", "engine-a[bot]", 3 * HOUR),
                commented("engine-a[bot]", HOUR)]
        self.assertEqual([], plan(issue(1), line))

    def test_an_open_pull_request_updated_recently_keeps_the_claim(self):
        ref = {"event": "cross-referenced", "actor": {"login": "engine-a[bot]"},
               "created_at": at(5 * HOUR),
               "source": {"type": "issue", "issue": {
                   "pull_request": {}, "state": "open", "updated_at": at(600)}}}
        line = [labeled("claim:engine-a", "engine-a[bot]", 6 * HOUR), ref]
        self.assertEqual([], plan(issue(1), line))

    def test_a_live_stated_wait_keeps_an_idle_claim(self):
        until = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(time.time() + HOUR))
        line = [labeled("claim:engine-a", "engine-a[bot]", 6 * HOUR),
                commented("engine-a[bot]", 5 * HOUR,
                          "Pushed the branch.\nwaiting: the deploy run until %s" % until)]
        self.assertEqual([], plan(issue(1), line))

    def test_a_wait_holds_through_its_grace_then_expires(self):
        until = time.time() - 10 * 60
        body = "waiting: CI until %s" % time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(until))
        line = [labeled("claim:engine-a", "engine-a[bot]", 6 * HOUR),
                commented("engine-a[bot]", 5 * HOUR, body)]
        self.assertEqual([], plan(issue(1), line))
        later = until + claim_referee.GRACE + 60
        self.assertEqual(["claim:engine-a"], [r.label for r in plan(issue(1), line, later)])

    def test_the_station_cannot_state_a_wait(self):
        until = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(time.time() + HOUR))
        line = [labeled("claim:engine-a", "engine-a[bot]", 6 * HOUR),
                commented("loom-app[bot]", 5 * HOUR, "waiting: x until %s" % until)]
        self.assertEqual(["claim:engine-a"], [r.label for r in plan(issue(1), line)])

    def test_a_persons_claim_is_never_touched(self):
        found = issue(1, labels=("claim:a-person",), updated=90 * 24 * HOUR)
        line = [labeled("claim:a-person", "a-person", 90 * 24 * HOUR)]
        self.assertEqual([], plan(found, line))
        self.assertFalse(claim_referee.needs_timeline(found, ENGINES, time.time()))

    def test_wait_times(self):
        cases = {"2026-10-30T18:00Z": "2026-10-30T18:00:00Z",
                 "2026-10-30T18:00:00Z.": "2026-10-30T18:00:00Z",
                 "2026-10-30 18:00 UTC": "2026-10-30T18:00:00Z",
                 "2026-10-30T14:00-04:00": "2026-10-30T18:00:00Z",
                 "2026-10-30": "2026-10-30T23:59:59Z"}
        for text, want in cases.items():
            with self.subTest(text=text):
                got = claim_referee.parse_until(text)
                self.assertEqual(want, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(got)))
        self.assertIsNone(claim_referee.parse_until("tomorrow"))


class Settings(unittest.TestCase):
    def test_modes_and_engine_labels_from_the_environment(self):
        self.assertEqual("on", claim_referee.mode(""))
        self.assertEqual("dry", claim_referee.mode("dry"))
        self.assertEqual("off", claim_referee.mode("off"))
        self.assertEqual(claim_referee.ENGINE_CLAIMS, claim_referee.engines(""))
        self.assertEqual(("claim:x", "claim:y"), claim_referee.engines("claim:X claim:y nope"))
        found = station.Settings.from_env({})
        self.assertEqual("on", found.claim_referee)
        self.assertEqual(claim_referee.ENGINE_CLAIMS, found.engine_claims)


class Fake:
    def __init__(self):
        self.issues, self.timelines, self.writes, self.reads = {}, {}, [], []

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        rest = parts.path[len("/repos/%s/" % REPO):]
        query = parse_qs(parts.query)
        if method != "GET":
            self.writes.append((method, unquote(rest), json.loads(body) if body else None))
            return 200, {}, b"{}"
        self.reads.append(rest)
        if rest == "labels":
            names = sorted({x["name"] for i in self.issues.values() for x in i["labels"]})
            return 200, {}, json.dumps([{"name": n} for n in names]).encode()
        if rest == "issues":
            label = query.get("labels", [""])[0]
            found = [i for i in self.issues.values() if label in [x["name"] for x in i["labels"]]]
            return 200, {}, json.dumps(found).encode()
        if rest.endswith("/comments"):
            return 200, {}, b"[]"
        if rest.endswith("/timeline"):
            number = int(rest.split("/")[1])
            return 200, {}, json.dumps(self.timelines.get(number, [])).encode()
        raise AssertionError("unexpected %s %s" % (method, url))


class Step(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.join(tmp.name, "loom")
        self.fake = Fake()
        self.settings = station.Settings(self.home, [REPO], ["loom-app[bot]"],
                                         ["example-owner"], claim_referee="on",
                                         engine_claims=ENGINES)

    def state_path(self):
        return os.path.join(self.home, "state", "station", claim_referee.STATE_FILE)

    def swept(self):
        os.makedirs(os.path.dirname(self.state_path()), exist_ok=True)
        with open(self.state_path(), "w") as fh:
            json.dump({"swept": True}, fh)

    def run_step(self, now=None):
        station.Station(GitHub("t", transport=self.fake), self.settings,
                        now=now or time.time()).referee_claims()

    def idle(self, number, labels=("claim:engine-a",)):
        self.fake.issues[number] = issue(number, labels=labels)
        self.fake.timelines[number] = [labeled(x, "engine[bot]", 3 * HOUR) for x in labels]

    def test_a_duplicate_is_removed_with_a_short_note_after_the_first_pass(self):
        self.swept()
        self.fake.issues[1] = issue(1, labels=("claim:engine-a", "claim:engine-b"), updated=60)
        self.fake.timelines[1] = [labeled("claim:engine-a", "engine-a[bot]", 30 * 60),
                                  labeled("claim:engine-b", "engine-b[bot]", 20 * 60)]
        self.run_step()
        methods = [(m, p) for m, p, _ in self.fake.writes]
        self.assertEqual([("DELETE", "issues/1/labels/claim:engine-b"),
                          ("POST", "issues/1/comments")], methods)
        self.assertIn("claim:engine-a", self.fake.writes[-1][2]["body"])

    def test_the_first_pass_is_capped_and_posts_no_comments(self):
        for number in range(1, 20):
            self.idle(number)
        self.run_step()
        self.assertTrue(self.fake.writes)
        self.assertEqual({"DELETE"}, {m for m, _, _ in self.fake.writes})
        self.assertEqual(claim_referee.FIRST_PER_RUN, len(self.fake.writes))
        with open(self.state_path()) as fh:
            self.assertFalse(json.load(fh).get("swept"))

    def test_the_first_pass_ends_when_a_run_finds_nothing_more(self):
        self.idle(1)
        self.run_step()
        with open(self.state_path()) as fh:
            self.assertTrue(json.load(fh).get("swept"))

    def test_expiry_posts_no_comment(self):
        self.swept()
        self.idle(1)
        self.run_step()
        self.assertEqual([("DELETE", "issues/1/labels/claim:engine-a", None)], self.fake.writes)

    def test_timelines_are_read_only_for_claimed_tickets_that_may_be_due(self):
        self.idle(1)
        self.fake.issues[2] = issue(2, updated=60)          # one claim, touched a minute ago
        self.fake.issues[3] = issue(3, labels=("claim:a-person",))
        self.fake.issues[4] = issue(4, labels=("loom:queued",))
        self.run_step()
        self.assertEqual(["issues/1/timeline"], [r for r in self.fake.reads if "timeline" in r])

    def test_dry_mode_writes_nothing(self):
        self.settings.claim_referee = "dry"
        self.idle(1)
        self.run_step()
        self.assertEqual([], self.fake.writes)

    def test_off_reads_nothing(self):
        self.settings.claim_referee = "off"
        self.idle(1)
        self.run_step()
        self.assertEqual([], self.fake.reads)

    def test_a_run_waits_its_interval(self):
        self.swept()
        self.idle(1)
        now = time.time()
        self.run_step(now)
        self.fake.writes.clear()
        self.idle(2)
        self.run_step(now + 60)
        self.assertEqual([], self.fake.writes)


if __name__ == "__main__":
    unittest.main()
