"""Another operator's idle claims come back to this Loom (bus/stale_claims.py).

An issue labelled claim:<login> or assigned to another operator was that
operator's forever: a claim nobody worked held its issue with no way back.
The operator asked for claims left alone to return (2026-10-21).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import stale_claims, station  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
DAY = 24 * 3600


def at(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def issue(number, labels=("claim:other-op",), assignees=(), created=10 * DAY):
    return {"number": number, "title": "Issue %d" % number, "state": "open",
            "created_at": at(created), "user": {"login": "other-op"},
            "labels": [{"name": x} for x in labels],
            "assignees": [{"login": x} for x in assignees]}


def labeled(name, by, ago):
    return {"event": "labeled", "actor": {"login": by}, "label": {"name": name},
            "created_at": at(ago)}


def commented(by, ago):
    return {"event": "commented", "user": {"login": by}, "actor": {"login": by},
            "created_at": at(ago)}


def referenced(by, ago, state="closed", pull=True):
    source = {"user": {"login": by}, "state": state}
    if pull:
        source["pull_request"] = {}
    return {"event": "cross-referenced", "actor": {"login": by},
            "source": {"type": "issue", "issue": source}, "created_at": at(ago)}


class Rules(unittest.TestCase):
    def test_a_claim_its_claimer_left_alone_past_the_limit_is_stale(self):
        line = [labeled("claim:other-op", "other-op", 3 * DAY), commented("other-op", 2 * DAY)]
        self.assertTrue(stale_claims.stale(issue(1), line, "other-op", "claim:other-op",
                                           time.time(), DAY))

    def test_a_recent_act_of_the_claimer_keeps_the_claim(self):
        for line in ([commented("other-op", 3600)],
                     [labeled("claim:other-op", "someone", 3600)],
                     [referenced("other-op", 3600)]):
            with self.subTest(line=line[0]["event"]):
                self.assertFalse(stale_claims.stale(issue(1), line, "other-op",
                                                    "claim:other-op", time.time(), DAY))

    def test_an_open_pull_request_of_the_claimers_keeps_the_claim_however_old(self):
        line = [referenced("other-op", 5 * DAY, state="open")]
        self.assertFalse(stale_claims.stale(issue(1), line, "other-op", "claim:other-op",
                                            time.time(), DAY))

    def test_someone_elses_recent_comment_does_not_keep_the_claim(self):
        line = [labeled("claim:other-op", "other-op", 3 * DAY), commented("bot", 60)]
        self.assertTrue(stale_claims.stale(issue(1), line, "other-op", "claim:other-op",
                                           time.time(), DAY))

    def test_our_own_claims_are_never_anyones_else(self):
        found = stale_claims.claimers(issue(1, labels=("claim:me", "claim:Other-Op"),
                                            assignees=("me", "other-op", "briefer")),
                                      ["me"], ["briefer"])
        self.assertEqual({"other-op": "claim:Other-Op", "briefer": ""}, found)

    def test_an_issue_also_assigned_to_an_unknown_person_is_never_released(self):
        # Found by the automated security review: any assignee not ours was
        # a releasable claimer, so a stranger's assignment would be removed.
        found = stale_claims.claimers(issue(1, assignees=("other-op", "stranger")),
                                      ["me"], ["other-op"])
        self.assertEqual({}, found)

    def test_the_setting_reads_whole_hours_and_zero_is_off(self):
        self.assertEqual(24, stale_claims.idle_hours(""))
        self.assertEqual(48, stale_claims.idle_hours("48"))
        self.assertEqual(0, stale_claims.idle_hours("0"))
        self.assertEqual(24, stale_claims.idle_hours("soon"))


class Fake:
    def __init__(self):
        self.issues, self.timelines, self.writes = {}, {}, []
        self.pulls = []

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        rest = parts.path[len("/repos/%s/" % REPO):]
        query = parse_qs(parts.query)
        if method != "GET":
            self.writes.append((method, unquote(rest), json.loads(body) if body else None))
            return 200, {}, b"{}"
        if rest == "labels":
            names = sorted({x["name"] for i in self.issues.values() for x in i["labels"]})
            return 200, {}, json.dumps([{"name": n} for n in names]).encode()
        if rest == "issues":
            label = query.get("labels", [""])[0]
            who = query.get("assignee", [""])[0]
            found = [i for i in self.issues.values()
                     if (label and label in [x["name"] for x in i["labels"]])
                     or (who and who in [a["login"] for a in i["assignees"]])]
            return 200, {}, json.dumps(found).encode()
        if rest == "pulls":
            return 200, {}, json.dumps(self.pulls).encode()
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
        self.fake = Fake()
        self.settings = station.Settings(os.path.join(tmp.name, "loom"), [REPO], ["me"],
                                         ["example-owner"], briefers=["other-op"],
                                         claim_idle_hours=24)

    def run_step(self):
        station.Station(GitHub("t", transport=self.fake), self.settings,
                        now=time.time()).release_stale_claims()

    def test_an_idle_claim_is_released_and_a_worked_one_kept(self):
        self.fake.issues[1] = issue(1, assignees=("other-op",))
        self.fake.timelines[1] = [labeled("claim:other-op", "other-op", 3 * DAY)]
        self.fake.issues[2] = issue(2)
        self.fake.timelines[2] = [commented("other-op", 3600)]
        self.fake.issues[3] = issue(3, labels=("claim:me",))
        self.run_step()
        self.assertEqual([("DELETE", "issues/1/labels/claim:other-op", None),
                          ("DELETE", "issues/1/assignees", {"assignees": ["other-op"]})],
                         self.fake.writes)

    def test_the_step_is_off_at_zero_hours(self):
        self.settings.claim_idle_hours = 0
        self.fake.issues[1] = issue(1)
        self.run_step()
        self.assertEqual([], self.fake.writes)

    def test_from_env_turns_it_on_at_24_hours(self):
        self.assertEqual(24, station.Settings.from_env({}).claim_idle_hours)


def own(number, labels=("claim:me", "loom:briefed"), assignees=(), created=10 * DAY):
    return issue(number, labels=labels, assignees=assignees, created=created)


class OwnRules(unittest.TestCase):
    """This Loom's own claim lapses when no lane works the ticket (loom#1217)."""

    def lapsed(self, found, line, pulls=()):
        return stale_claims.lapsed(found, line, pulls, "claim:me", time.time(), DAY)

    def test_a_claim_with_no_run_and_no_pull_request_activity_lapses(self):
        line = [labeled("claim:me", "me", 3 * DAY), labeled("loom:briefed", "bot", 2 * DAY)]
        self.assertTrue(self.lapsed(own(1), line, [at(2 * DAY)]))

    def test_a_run_in_progress_keeps_the_claim(self):
        found = own(1, labels=("claim:me", "loom:running"))
        self.assertFalse(self.lapsed(found, [labeled("claim:me", "me", 3 * DAY)]))

    def test_a_pull_request_updated_within_the_limit_keeps_the_claim(self):
        line = [labeled("claim:me", "me", 3 * DAY)]
        self.assertFalse(self.lapsed(own(1), line, [at(2 * DAY), at(3600)]))

    def test_a_recent_event_on_the_ticket_keeps_the_claim(self):
        for line in ([labeled("loom:running", "bot", 3600)], [commented("someone", 3600)]):
            with self.subTest(line=line[0]["event"]):
                self.assertFalse(self.lapsed(own(1), line))

    def test_a_claim_without_a_loom_label_never_lapses_here(self):
        found = own(1, labels=("claim:me",))
        self.assertFalse(self.lapsed(found, [labeled("claim:me", "me", 9 * DAY)]))

    def test_a_wait_label_does_not_keep_the_claim(self):
        found = own(1, labels=("claim:me", "wait:model-limit"))
        self.assertTrue(self.lapsed(found, [labeled("claim:me", "me", 3 * DAY)]))

    def test_own_claims_are_the_labels_naming_our_logins(self):
        found = stale_claims.own_claims(issue(1, labels=("claim:Me", "claim:other-op")), ["me"])
        self.assertEqual({"me": "claim:Me"}, found)


class OwnStep(unittest.TestCase):
    setUp = Step.setUp
    run_step = Step.run_step

    def test_an_unworked_claim_is_dropped_with_a_status_note(self):
        self.fake.issues[1] = own(1, assignees=("me",))
        self.fake.timelines[1] = [labeled("claim:me", "me", 3 * DAY)]
        self.fake.issues[2] = own(2)
        self.fake.timelines[2] = [labeled("claim:me", "me", 3 * DAY)]
        self.fake.pulls = [{"number": 9, "body": "Refs #2", "updated_at": at(60)}]
        self.fake.issues[3] = own(3, labels=("claim:me", "loom:running"))
        self.run_step()
        methods = [(m, p) for m, p, _ in self.fake.writes]
        self.assertEqual([("DELETE", "issues/1/labels/claim:me"),
                          ("DELETE", "issues/1/assignees"),
                          ("POST", "issues/1/comments")], methods)
        note = self.fake.writes[-1][2]["body"]
        self.assertIn("Released the claim label claim:me", note)
        self.assertIn("24 hours", note)


if __name__ == "__main__":
    unittest.main()
