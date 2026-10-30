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
TRUSTED = ("an-operator", "loom-app[bot]")


def at(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def issue(number, labels=("claim:engine-a",), updated=5 * HOUR, created=48 * HOUR,
          assignees=()):
    return {"number": number, "title": "Issue %d" % number, "state": "open",
            "created_at": at(created), "updated_at": at(updated),
            "user": {"login": "someone"},
            "labels": [{"name": x} for x in labels + ("loom:queued",)],
            "assignees": [{"login": x} for x in assignees]}


def labeled(name, by, ago):
    return {"event": "labeled", "actor": {"login": by}, "label": {"name": name},
            "created_at": at(ago)}


def commented(by, ago, body="a note"):
    return {"event": "commented", "user": {"login": by}, "actor": {"login": by},
            "body": body, "created_at": at(ago)}


def pull(by, ago, state="open"):
    return {"event": "cross-referenced", "actor": {"login": by}, "created_at": at(5 * HOUR),
            "source": {"type": "issue", "issue": {
                "pull_request": {}, "state": state, "user": {"login": by},
                "updated_at": at(ago)}}}


def wait_line(seconds_ahead):
    when = time.gmtime(time.time() + seconds_ahead)
    return "waiting: the deploy run until %s" % time.strftime("%Y-%m-%dT%H:%MZ", when)


def plan(found, line, now=None):
    return claim_referee.plan(found, line, now or time.time(), ENGINES, STATION, TRUSTED)


def labels_of(removals):
    return [r.label for r in removals]


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

    def test_an_open_pull_request_by_the_claimer_holds_however_old(self):
        line = [labeled("claim:engine-a", "engine-a[bot]", 6 * HOUR),
                pull("engine-a[bot]", 5 * HOUR)]
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
        self.assertFalse(claim_referee.needs_timeline(found, ENGINES))

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


class WhoCounts(unittest.TestCase):
    """The repo takes comments from strangers: only the claimer and trusted
    logins hold a claim, or end a hold."""

    def idle_claim(self, *more):
        return [labeled("claim:engine-a", "engine-a[bot]", 6 * HOUR)] + list(more)

    def expired(self, *more):
        return labels_of(plan(issue(1), self.idle_claim(*more))) == ["claim:engine-a"]

    def test_a_stranger_cannot_state_a_wait(self):
        self.assertTrue(self.expired(commented("a-stranger", 600, wait_line(HOUR))))

    def test_another_engine_cannot_state_a_wait(self):
        self.assertTrue(self.expired(commented("engine-b[bot]", 600, wait_line(HOUR))))

    def test_a_trusted_login_can_state_a_wait(self):
        self.assertFalse(self.expired(commented("An-Operator", 5 * HOUR, wait_line(HOUR))))

    def test_a_trusted_station_login_cannot_state_a_wait(self):
        self.assertTrue(self.expired(commented("loom-app[bot]", 5 * HOUR, wait_line(HOUR))))

    def test_a_stranger_comment_is_not_activity(self):
        self.assertTrue(self.expired(commented("a-stranger", 600)))

    def test_a_stranger_label_is_not_activity(self):
        self.assertTrue(self.expired(labeled("bug", "a-stranger", 600)))

    def test_a_strangers_pull_request_is_not_activity(self):
        self.assertTrue(self.expired(pull("a-stranger", 600)))

    def test_a_trusted_comment_is_activity(self):
        self.assertFalse(self.expired(commented("an-operator", 600)))

    def test_a_trusted_pull_request_holds(self):
        self.assertFalse(self.expired(pull("an-operator", 5 * HOUR)))

    def test_a_closed_pull_request_does_not_hold_or_count_its_update(self):
        self.assertTrue(self.expired(pull("engine-a[bot]", 600, state="closed")))

    def test_an_event_nobody_performed_is_not_activity(self):
        for kind in ("mentioned", "subscribed", "unsubscribed"):
            with self.subTest(kind=kind):
                event = {"event": kind, "actor": {"login": "an-operator"}, "created_at": at(600)}
                self.assertTrue(self.expired(event))
                event = {"event": kind, "actor": {"login": "engine-a[bot]"}, "created_at": at(600)}
                self.assertTrue(self.expired(event))

    def test_an_event_a_writer_performed_is_activity(self):
        for kind in ("unlabeled", "assigned", "closed", "reopened"):
            with self.subTest(kind=kind):
                event = {"event": kind, "actor": {"login": "an-operator"}, "created_at": at(600)}
                self.assertFalse(self.expired(event))

    def test_a_later_stranger_comment_does_not_end_a_trusted_wait(self):
        self.assertFalse(self.expired(commented("an-operator", 5 * HOUR, wait_line(HOUR)),
                                      commented("a-stranger", 600, wait_line(-3 * HOUR))))

    def test_a_later_shorter_line_does_not_end_a_trusted_wait(self):
        self.assertFalse(self.expired(commented("an-operator", 5 * HOUR, wait_line(HOUR)),
                                      commented("engine-a[bot]", 3 * HOUR, wait_line(-3 * HOUR))))

    def test_a_newer_wait_outlasts_a_spent_one(self):
        self.assertFalse(self.expired(commented("an-operator", 5 * HOUR, wait_line(-3 * HOUR)),
                                      commented("engine-a[bot]", 3 * HOUR, wait_line(HOUR))))

    def test_a_wait_is_capped_after_its_comment(self):
        ago = claim_referee.MAX_WAIT + claim_referee.GRACE + 600
        self.assertTrue(self.expired(commented("engine-a[bot]", ago, wait_line(30 * 86400))))

    def test_a_wait_holds_up_to_the_cap(self):
        ago = claim_referee.MAX_WAIT - 600
        self.assertFalse(self.expired(commented("engine-a[bot]", ago, wait_line(30 * 86400))))


class WaitParsing(unittest.TestCase):
    def times(self, body):
        return [time.strftime("%H:%M", time.gmtime(x)) for x in claim_referee.stated_times(body)]

    def test_a_line_is_found_among_others_in_any_case(self):
        self.assertEqual(["18:00"], self.times("Pushed.\n  WAITING:\tCI  Until  2026-10-30T18:00Z\nok"))

    def test_the_time_follows_the_last_until(self):
        self.assertEqual(["18:00"], self.times("waiting: hold until done until 2026-10-30T18:00Z"))

    def test_a_wait_needs_a_what_and_a_time(self):
        for body in ("waiting: until 2026-10-30T18:00Z", "waiting: CI until",
                     "waiting: CI", "not waiting: CI until 2026-10-30T18:00Z"):
            with self.subTest(body=body):
                self.assertEqual([], self.times(body))

    def test_a_line_over_the_limit_is_skipped(self):
        what = "x" * claim_referee.MAX_WAIT_LINE
        self.assertEqual([], self.times("waiting: %s until 2026-10-30T18:00Z" % what))

    def test_a_line_past_the_body_limit_is_not_read(self):
        padding = "\n" * claim_referee.MAX_BODY
        self.assertEqual([], self.times(padding + "waiting: CI until 2026-10-30T18:00Z"))

    def test_the_old_pattern_text_stays_cheap(self):
        started = time.monotonic()
        self.assertEqual([], self.times("waiting: a" + " until" * 1000000))
        self.assertLess(time.monotonic() - started, 1)


class NeverExpires(unittest.TestCase):
    LINE = [labeled("claim:engine-a", "engine-a[bot]", 6 * HOUR)]

    def test_a_ticket_with_a_lane_run(self):
        found = issue(1, labels=("claim:engine-a", "loom:running"))
        self.assertEqual([], plan(found, self.LINE))
        self.assertFalse(claim_referee.needs_timeline(found, ENGINES))

    def test_a_ticket_assigned_to_a_person(self):
        found = issue(1, assignees=("a-person",))
        self.assertEqual([], plan(found, self.LINE))
        self.assertFalse(claim_referee.needs_timeline(found, ENGINES))

    def test_a_ticket_assigned_to_an_app_still_expires(self):
        found = issue(1, assignees=("engine-a[bot]",))
        self.assertEqual(["claim:engine-a"], labels_of(plan(found, self.LINE)))

    def test_a_ticket_with_no_loom_label(self):
        found = issue(1)
        found["labels"] = [{"name": "claim:engine-a"}]
        self.assertEqual([], plan(found, self.LINE))
        self.assertFalse(claim_referee.needs_timeline(found, ENGINES))

    def test_a_duplicate_on_such_a_ticket_is_still_removed(self):
        found = issue(1, labels=("claim:engine-a", "claim:engine-b", "loom:running"))
        line = self.LINE + [labeled("claim:engine-b", "engine-b[bot]", 5 * HOUR)]
        self.assertEqual(["claim:engine-b"], labels_of(plan(found, line)))


class FailsClosed(unittest.TestCase):
    def test_one_claim_with_no_labeled_event_stays(self):
        self.assertEqual([], plan(issue(1), [commented("a-stranger", 6 * HOUR)]))

    def test_one_claim_with_an_unreadable_labeled_event_stays(self):
        event = labeled("claim:engine-a", "engine-a[bot]", 6 * HOUR)
        event["created_at"] = "not a time"
        self.assertEqual([], plan(issue(1), [event]))


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
        self.fake.issues[2] = issue(2, labels=("claim:engine-a", "loom:running"))
        self.fake.issues[3] = issue(3, labels=("claim:a-person",))
        self.fake.issues[4] = issue(4, labels=())
        self.run_step()
        self.assertEqual(["issues/1/timeline"], [r for r in self.fake.reads if "timeline" in r])

    def test_a_strangers_comment_bumping_the_update_time_hides_nothing(self):
        self.swept()
        self.idle(1)
        self.fake.issues[1] = issue(1, updated=60)
        self.run_step()
        self.assertEqual([("DELETE", "issues/1/labels/claim:engine-a", None)], self.fake.writes)

    def test_every_claim_is_read_within_two_passes_past_the_read_budget(self):
        self.swept()
        count = claim_referee.READS_PER_RUN + 10
        for number in range(1, count + 1):
            self.fake.issues[number] = issue(number)
            self.fake.timelines[number] = [labeled("claim:engine-a", "engine-a[bot]", 60)]
        now = time.time()
        self.run_step(now)
        self.run_step(now + claim_referee.EVERY)
        read = {r for r in self.fake.reads if r.endswith("/timeline")}
        self.assertEqual(count, len(read))

    def test_a_briefers_wait_holds_and_the_stations_and_a_strangers_do_not(self):
        self.swept()
        self.settings.briefers = ["a-briefer"]
        self.idle(1)
        self.fake.timelines[1].append(commented("a-briefer", HOUR, wait_line(HOUR)))
        self.idle(2)
        self.fake.timelines[2].append(commented("loom-app[bot]", HOUR, wait_line(HOUR)))
        self.idle(3)
        self.fake.timelines[3].append(commented("a-stranger", HOUR, wait_line(HOUR)))
        self.run_step()
        self.assertEqual([("DELETE", "issues/2/labels/claim:engine-a", None),
                          ("DELETE", "issues/3/labels/claim:engine-a", None)],
                         sorted(self.fake.writes))

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
