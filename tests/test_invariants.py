from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, invariants, metrics, station  # noqa: E402
from bus.github import GitHubError, Response  # noqa: E402
from bus.invariants import Check, Event, Facts, GateHold, Journal, Posted, Ref, Run, SilentRun, Wait  # noqa: E402

REPO = "example-owner/factory"
OTHER = "example-owner/hidden"
GOAL = 30
AUTHOR = "trusted-example"
T0 = 1_800_000_000.0
DAY_S = 24 * 3600


def stamp(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def journal(at, holder="muse", step="- Step status: failed (GitHub answered 502)"):
    return "\n".join(["<!-- loom:journal v1 -->", "Tick %s, %s" % (stamp(at), holder), step,
                      "- Shipped: none", "- Cost: 40 requests, 6 minutes"])


def ticket(number, state="open"):
    return {"number": number, "state": state, "labels": [{"name": "loom:ticket"}],
            "body": "Why."}


def epic(number, state="open"):
    return {"number": number, "state": state, "title": "Epic: part %d" % number,
            "labels": [{"name": "epic"}], "body": "Done when: x.\n\nPart of #%d\n" % GOAL}


class FakeGH:
    """The GitHub reads and writes the invariant check makes, on one repo."""

    def __init__(self):
        self.rate = {}
        self.issues = {}
        self.comments = {}
        self.goal_issues = {REPO: [{"number": GOAL, "title": "Loom keeps goals moving",
                                    "labels": [{"name": "loom:goal"}],
                                    "sub_issues_summary": {"total": 40}}]}
        self.pulls = {}
        self.check_runs = {}
        self.private = {OTHER}
        self.calls = []
        self.writes = []
        self.search_items = []
        # The open review threads the review bot left on a pull request.
        self.threads = []
        # (repo, issue number) -> its sub-issues, for the goal walk.
        self.subs = {}
        # repo -> its open loom:ticket issues.
        self.tickets = {}

    def paginate(self, path):
        self.calls.append(("GET", path))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments\?", path)
        if m:
            return list(self.comments.get((m.group(1), int(m.group(2))), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:goal&", path)
        if m:
            return list(self.goal_issues.get(m.group(1), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:invariant&state=(all|open)", path)
        if m:
            return [i for i in self.issues.values()
                    if m.group(2) == "all" or i["state"] == "open"]
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:ticket&state=open", path)
        if m:
            return list(self.tickets.get(m.group(1), []))
        # The goal walk (goal, epics, tickets).
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/sub_issues\?", path)
        if m:
            return list(self.subs.get((m.group(1), int(m.group(2))), []))
        if re.match(r"/repos/[^/]+/[^/]+/issues\?labels=epic&", path):
            return []
        raise AssertionError("unexpected paginate %s" % path)

    def rest(self, method, path, body=None):
        self.calls.append((method, path))
        if method != "GET":
            self.writes.append((method, path, body))
        if method == "POST" and path == "/repos/%s/issues" % REPO:
            number = 100 + len(self.issues)
            self.issues[number] = dict(body, number=number, id=9000 + number, state="open",
                                       closed_at=None)
            return Response(201, {"number": number, "id": 9000 + number})
        if method == "GET" and path.startswith("/search/issues?"):
            return Response(200, {"items": self.search_items})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/sub_issues", path)
        if method == "POST" and m:
            child = next((i for i in self.issues.values() if i.get("id") == body["sub_issue_id"]),
                         None)
            if child is not None:
                self.subs.setdefault((m.group(1), int(m.group(2))), []).append(
                    dict(child, labels=[x if isinstance(x, dict) else {"name": x}
                                        for x in child.get("labels", [])]))
            return Response(201, {})
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)", path)
        if m and method == "GET":
            issue = self.issues.get(int(m.group(1)))
            if issue is None:
                raise GitHubError("Not Found", 404)
            # As GitHub answers: label objects, and when the issue was opened.
            return Response(200, dict(issue, created_at=issue.get("created_at", stamp(T0)),
                                      labels=[x if isinstance(x, dict) else {"name": x}
                                              for x in issue.get("labels", [])]))
        if m and method == "PATCH":
            self.issues[int(m.group(1))].update(body)
            return Response(200, {})
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/comments/(\d+)", path)
        if method == "PATCH" and m:
            for notes in self.comments.values():
                for note in notes:
                    if note.get("id") == int(m.group(1)):
                        note["body"] = body["body"]
            return Response(200, {})
        if method == "POST" and path.endswith("/comments"):
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", path)
            if m:
                self.next_comment = getattr(self, "next_comment", 500) + 1
                self.comments.setdefault((m.group(1), int(m.group(2))), []).append(
                    {"id": self.next_comment, "body": body["body"], "created_at": stamp(T0 + 10),
                     "user": {"login": AUTHOR}})
            return Response(201, {"id": getattr(self, "next_comment", 1)})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls\?state=open&per_page=100", path)
        if m:
            return Response(200, self.pulls.get(m.group(1), []))
        m = re.match(r"/repos/[^/]+/[^/]+/commits/([0-9a-f]+)/check-runs\?", path)
        if m:
            return Response(200, {"check_runs": self.check_runs.get(m.group(1), [])})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)", path)
        if m:
            return Response(200, {"private": m.group(1) in self.private})
        raise AssertionError("unexpected %s %s" % (method, path))

    def filed(self):
        return [i for i in self.issues.values()]

    def graphql(self, query):
        self.calls.append(("POST", "/graphql"))
        return {"repository": {"pullRequest": {"reviewThreads": {"nodes": self.threads}}}}

    def comment(self, number, at, body, repo=REPO, author=AUTHOR):
        self.comments.setdefault((repo, number), []).append(
            {"body": body, "created_at": stamp(at), "user": {"login": author}})


class StationBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.gh = FakeGH()
        self.said = []
        self.settings = station.Settings(self.home, [REPO, OTHER], [AUTHOR], ["example-owner"],
                                         invariant_goal="%s#%d" % (REPO, GOAL), self_repo=REPO)

    def run_at(self, now):
        st = station.Station(self.gh, self.settings, now=now)
        orig = station._say
        station._say = self.said.append
        try:
            st.check_invariants()
        finally:
            station._say = orig
        return st

    def write(self, path, text, at=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if at is not None:
            os.utime(path, (at, at))

    def lane_ticket(self, number, labels=("loom:ticket", "loom:pr"), state="open"):
        """The issue that carries a lane pull request."""
        self.gh.issues[number] = {"number": number, "state": state, "body": "Why.",
                                  "labels": [{"name": n} for n in labels]}

    def lane_pull(self, number, ticket, opened, labels=(), author=AUTHOR):
        """An open, non-draft lane pull request opened at `opened`, on the
        branch its ticket number names."""
        self.gh.pulls.setdefault(REPO, []).append(
            {"number": number, "created_at": stamp(opened),
             "head": {"ref": "loom/%d-thing" % ticket, "sha": "%x" % number},
             "labels": [{"name": n} for n in labels], "user": {"login": author}})

    def bot_threads(self, count=1):
        """`count` unresolved review threads the review bot opened."""
        self.gh.threads = [{"id": "T%d" % i, "comments": {"nodes": [
            {"author": {"login": "grug-tribe[bot]"}, "body": "naming %d" % i}]}}
            for i in range(count)]

    def breaches(self):
        """The invariant issues this pass filed."""
        return [i for i in self.gh.filed() if invariants.LABEL in i.get("labels", [])]


class AcceptanceTest(StationBase):
    def test_event_log_hashes_inputs_and_ignores_untrusted_rows(self):
        st = station.Station(self.gh, self.settings, now=T0)
        private_value = "private-host.example.invalid"
        st._event("rewrite_skipped", private_value, private_value, private_value, private_value)
        with open(st.event_log, encoding="utf-8") as stream:
            raw = stream.read()
        self.assertNotIn(private_value, raw)
        with open(st.event_log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": T0, "kind": "not_claiming", "reason": private_value}) + "\n")
            stream.write(json.dumps({"at": "not-a-time", "kind": "not_claiming",
                                     "reason": "0" * 64}) + "\n")
        events = st._read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, hashlib.sha256(private_value.encode()).hexdigest())

    def unwritable_event_log(self):
        """A home whose logs directory cannot be made: a plain file sits on
        its path, so every event-log open raises an OSError."""
        os.makedirs(self.home, exist_ok=True)
        with open(os.path.join(self.home, "logs"), "w", encoding="utf-8") as fh:
            fh.write("not a directory")

    def test_an_event_log_that_cannot_be_written_does_not_fail_the_decision(self):
        # loom#763: _event let a disk error escape into _refuse and the
        # rewrite loop, so one lost log line failed the whole decision.
        self.unwritable_event_log()
        st = station.Station(self.gh, self.settings, now=T0)
        orig = station._say
        station._say = self.said.append
        try:
            st._event("rewrite_skipped", "no agent command for this repo", "x/y#1")
        finally:
            station._say = orig
        self.assertTrue(any("event log" in line for line in self.said), self.said)

    def test_an_event_log_that_cannot_be_read_leaves_only_events_unknown(self):
        # loom#763: _invariant_facts reads each source on its own, but the
        # event log was read outside that guard, so one unreadable log dir
        # made every invariant find nothing this pass.
        self.unwritable_event_log()
        st = station.Station(self.gh, self.settings, now=T0)
        orig = station._say
        station._say = self.said.append
        try:
            facts = st._invariant_facts({})
        finally:
            station._say = orig
        self.assertIsNone(facts.events)
        self.assertIsNotNone(facts.held)
        self.assertTrue(any("could not read events" in line for line in self.said), self.said)

    def test_repeated_rewrite_skip_files_once_across_two_hours(self):
        path = os.path.join(self.home, "logs", "events.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as stream:
            for at in range(int(T0 - 3600), int(T0) + 1, 60):
                stream.write(json.dumps({"at": at, "kind": "rewrite_skipped",
                             "reason": hashlib.sha256(b"no agent command for this repo").hexdigest()}) + "\n")
        self.run_at(T0)
        with open(path, "a", encoding="utf-8") as stream:
            for at in range(int(T0), int(T0 + 3601), 60):
                stream.write(json.dumps({"at": at, "kind": "rewrite_skipped",
                             "reason": hashlib.sha256(b"no agent command for this repo").hexdigest()}) + "\n")
        self.run_at(T0 + 3600)
        made = [i for i in self.gh.issues.values()
                if "repeated-failure" in i.get("body", "")]
        self.assertEqual(len(made), 1)

    def write_events(self, rows):
        path = os.path.join(self.home, "logs", "events.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            for at, kind, reason in rows:
                stream.write(json.dumps({"at": at, "kind": kind,
                                         "reason": hashlib.sha256(reason.encode()).hexdigest()}) + "\n")

    def repeated_failure_tickets(self):
        return [i for i in self.gh.issues.values() if "repeated-failure" in i.get("body", "")]

    def test_repeated_failure_fires_between_irregular_passes(self):
        self.write_events((at, "rewrite_skipped", "no agent command for this repo")
                          for at in range(int(T0 - 3650), int(T0) + 1, 290))
        self.run_at(T0)
        self.assertEqual(len(self.repeated_failure_tickets()), 1)

    def test_a_rewrite_success_with_another_reason_suppresses_every_rewrite_failure(self):
        self.write_events([(at, kind, "no agent command for this repo")
                           for at in range(int(T0 - 3600), int(T0) + 1, 60)
                           for kind in ("rewrite_skipped", "rewrite_failed")]
                          + [(T0 - 1800, "rewrite_succeeded", "rewrote the body")])
        self.run_at(T0)
        self.assertEqual(self.repeated_failure_tickets(), [])

    def test_a_model_wait_success_suppresses_repeated_model_wait_failure(self):
        self.write_events([(at, "model_wait", "no free lane takes model claude-sonnet-5-5")
                           for at in range(int(T0 - 7200), int(T0 - 3600) + 1, 60)]
                          + [(T0 - 1800, "model_wait_succeeded", "found free lane opencode-go-3 for model claude-sonnet-5-5")])
        self.run_at(T0)
        self.assertEqual(self.repeated_failure_tickets(), [])

    def test_thirteen_metrics_lines_for_one_day_file_one_ticket_and_a_second_pass_none(self):
        for i in range(13):
            self.gh.comment(GOAL, T0 - 3600 + i * 60,
                            "%s\n2027-01-15: 6/9 pass; streak 0/7\n" % metrics.MARKER)
        self.run_at(T0)
        filed = self.gh.filed()
        self.assertEqual(len(filed), 1, self.said)
        self.assertIn("13 times for 2027-01-15", filed[0]["title"])
        self.assertEqual(filed[0]["labels"], ["loom:triage", "loom:invariant"])
        self.assertIn("Part of #%d" % GOAL, filed[0]["body"])
        writes = list(self.gh.writes)
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(len(self.gh.filed()), 1)
        self.assertEqual(self.gh.writes, writes)

    def test_two_journals_with_a_failed_status_step_file_a_ticket_naming_the_holder(self):
        self.gh.comment(GOAL, T0 - 2400, journal(T0 - 2400, holder="night-brain"))
        self.gh.comment(GOAL, T0 - 600, journal(T0 - 600, holder="night-brain"))
        self.run_at(T0)
        filed = self.gh.filed()
        self.assertEqual(len(filed), 1, self.said)
        self.assertIn("night-brain", filed[0]["title"])
        self.assertIn("status failed: (GitHub answered 502)", filed[0]["body"])

    def test_a_lane_with_twelve_runs_and_none_done_files_a_ticket_with_its_last_reason(self):
        for i in range(12):
            name = "example-owner.factory-%d-1" % (200 + i)
            at = T0 - 7200 + i * 300
            self.write(os.path.join(self.home, "briefs", "spark", "failed", name + ".md"),
                       "Issue: %s#%d\n" % (REPO, 200 + i), at - 300)
            reason = "spark answered 503" if i == 11 else "agent made no commit"
            self.write(os.path.join(self.home, "logs", "spark", name + ".log"),
                       "[%s] example-owner.factory-%d failed: [work] %s\n" % (stamp(at), 200 + i, reason),
                       at)
        self.run_at(T0)
        filed = self.gh.filed()
        self.assertEqual(len(filed), 1, self.said)
        self.assertIn("Lane spark: 12 runs in 24 hours and none done", filed[0]["title"])
        self.assertIn("spark answered 503", filed[0]["body"])
        self.assertNotIn("example-owner/factory", filed[0]["body"])


class StationTest(StationBase):
    def test_nothing_broken_files_nothing_and_checks_at_most_every_few_minutes(self):
        self.run_at(T0)
        self.assertEqual(self.gh.writes, [])
        self.gh.calls.clear()
        self.run_at(T0 + 10)
        self.assertEqual(self.gh.calls, [])

    def test_settings_read_the_goal_and_plan_check_from_the_environment(self):
        s = station.Settings.from_env({"LOOM_HOME": self.home, "LOOM_STATUS_REPO": REPO,
                                       "LOOM_INVARIANT_GOAL": "%s#5" % REPO,
                                       "LOOM_PLAN_CHECK": "My Plan"})
        self.assertEqual((s.invariant_goal, s.plan_check), ("%s#5" % REPO, "My Plan"))
        s = station.Settings.from_env({"LOOM_HOME": self.home, "LOOM_STATUS_REPO": REPO})
        self.assertEqual(s.plan_check, invariants.DEFAULT_PLAN_CHECK)

    def test_the_metrics_goal_in_the_self_repo_is_the_default_goal(self):
        self.settings.invariant_goal = ""
        self.settings.metrics_goal = "%s#12" % REPO
        self.assertEqual(station.Station(self.gh, self.settings, now=T0)._invariant_goal(), (REPO, 12))
        self.settings.metrics_goal = "%s#12" % OTHER
        self.assertEqual(station.Station(self.gh, self.settings, now=T0)._invariant_goal(), (REPO, GOAL))

    def test_a_private_goal_home_is_named_in_words_only(self):
        self.gh.goal_issues[OTHER] = [{"number": 3, "title": "Hidden", "labels": []}]
        self.gh.subs[(OTHER, 3)] = [ticket(7)]
        self.run_at(T0)
        filed = self.gh.filed()
        self.assertEqual(len(filed), 1, self.said)
        text = filed[0]["title"] + filed[0]["body"]
        self.assertIn("issue 3 of a private repo", text)
        self.assertNotIn("hidden", text)

    def test_a_lane_plan_check_failed_over_half_an_hour_ago_files_a_ticket(self):
        self.gh.pulls[REPO] = [
            {"number": 7, "head": {"ref": "loom/7-thing", "sha": "abc123"}, "user": {"login": AUTHOR}},
            {"number": 8, "head": {"ref": "loom/8-thing", "sha": "def456"}, "user": {"login": AUTHOR}},
            {"number": 9, "head": {"ref": "my-branch", "sha": "fed789"}, "user": {"login": AUTHOR}}]
        failed = {"name": invariants.DEFAULT_PLAN_CHECK, "status": "completed", "conclusion": "failure",
                  "output": {"title": "Size section missing"}}
        self.gh.check_runs["abc123"] = [dict(failed, completed_at=stamp(T0 - 3600))]
        self.gh.check_runs["def456"] = [dict(failed, completed_at=stamp(T0 - 600))]
        self.gh.check_runs["fed789"] = [dict(failed, completed_at=stamp(T0 - 3600))]
        self.run_at(T0)
        filed = self.gh.filed()
        self.assertEqual(len(filed), 1, self.said)
        self.assertIn("pull request #7", filed[0]["title"])
        self.assertIn("Size section missing", filed[0]["body"])

    def test_a_lane_held_over_two_hours_with_nothing_of_its_tier_queued_files_nothing(self):
        usage = os.path.join(self.home, "state", "usage", "agy.json")
        for at in (T0, T0 + 3 * 3600):
            self.write(usage, '{"lane": "agy", "at": %d, "available": false, "reason": "limit: quota"}'
                       % at)
            self.run_at(at)
        self.assertEqual(self.gh.filed(), [])
        self.assertTrue(any("/issues?labels=loom:ticket" in c[1] for c in self.gh.calls))

    def out_of_credit(self, lanes, at):
        """Each lane's heartbeat at `at`: held out of credit, as run_lane.sh
        writes it."""
        for lane in lanes:
            self.write(os.path.join(self.home, "state", "usage", lane + ".json"), json.dumps(
                {"lane": lane, "at": at, "available": False,
                 "reason": "limit: out of credit; unavailable until %s" % stamp(at + DAY_S)}))

    def queued_ticket(self, number, *labels, author=AUTHOR, difficulty="hard"):
        self.gh.tickets.setdefault(REPO, []).append(
            {"number": number, "state": "open", "user": {"login": author},
             "body": "Difficulty: %s\n\n## Why\nx\n" % difficulty,
             "labels": [{"name": n} for n in ("loom:ticket",) + labels]})

    def held_out_of_credit(self):
        """Two anthropic slots out of credit for three hours, with one hard
        ticket queued: the breach the no-top-up rule is about."""
        self.queued_ticket(653, "loom:queued")
        for at in (T0, T0 + 3 * 3600):
            self.out_of_credit(("anthropic", "anthropic-2"), at)
            self.run_at(at)

    def test_an_out_of_credit_lane_held_while_its_tier_waits_files_by_default(self):
        self.held_out_of_credit()
        (filed,) = self.breaches()
        self.assertIn("2 lanes held by a limit", filed["title"])
        for lane in ("anthropic", "anthropic-2"):
            self.assertIn("- %s: held by a limit since" % lane, filed["body"])

    def test_no_topup_skips_lanes_held_out_of_credit_on_that_account(self):
        self.settings.no_topup = ["anthropic"]
        self.held_out_of_credit()
        self.assertEqual(self.breaches(), [])
        # Nothing to file, so the queued tickets are not even read.
        self.assertFalse(any("/issues?labels=loom:ticket" in c[1] for c in self.gh.calls))

    def test_no_topup_still_files_a_lane_of_that_account_held_by_another_limit(self):
        self.settings.no_topup = ["anthropic"]
        self.queued_ticket(653, "loom:queued")
        usage = os.path.join(self.home, "state", "usage", "anthropic-3.json")
        for at in (T0, T0 + 3 * 3600):
            self.write(usage, json.dumps({"lane": "anthropic-3", "at": at, "available": False,
                                          "reason": "limit: rate limited"}))
            self.run_at(at)
        (filed,) = self.breaches()
        self.assertIn("1 lane held by a limit", filed["title"])
        self.assertIn("- anthropic-3: held by a limit since", filed["body"])

    def test_a_lane_whose_limit_hold_expired_is_not_held(self):
        # A hold whose time has passed is no hold: the lane is free again and
        # the lane-held invariant stops firing, as the Usage view reads the
        # same heartbeat the same way. 2026-10-20: a lane held by a limit the
        # provider had already lifted stayed held for days.
        self.queued_ticket(653, "loom:queued", difficulty="medium")
        usage = os.path.join(self.home, "state", "usage", "opencode-go.json")
        for at in (T0, T0 + 3 * 3600):
            self.write(usage, json.dumps(
                {"lane": "opencode-go", "at": at, "available": False,
                 "reason": "limit: the provider's monthly limit (429 GoUsageLimitError)",
                 "retry_at": at - 3600}))
            self.run_at(at)
        self.assertEqual(self.breaches(), [])

    def test_a_lane_held_by_a_limit_that_has_not_expired_is_held(self):
        self.queued_ticket(653, "loom:queued", difficulty="medium")
        usage = os.path.join(self.home, "state", "usage", "opencode-go.json")
        for at in (T0, T0 + 3 * 3600):
            self.write(usage, json.dumps(
                {"lane": "opencode-go", "at": at, "available": False,
                 "reason": "limit: the provider's monthly limit (429 GoUsageLimitError)",
                 "retry_at": at + 7 * 86400}))
            self.run_at(at)
        (filed,) = self.breaches()
        self.assertIn("- opencode-go: held by a limit since", filed["body"])

    def test_a_lane_held_by_a_limit_with_no_end_is_held(self):
        # A hold with no known end (an expired key, a limit the provider gave
        # no reset for) is held until the lane says otherwise.
        self.queued_ticket(653, "loom:queued", difficulty="medium")
        usage = os.path.join(self.home, "state", "usage", "opencode-go.json")
        for at in (T0, T0 + 3 * 3600):
            self.write(usage, json.dumps(
                {"lane": "opencode-go", "at": at, "available": False,
                 "reason": "limit: the provider's monthly limit (429 GoUsageLimitError)"}))
            self.run_at(at)
        (filed,) = self.breaches()
        self.assertIn("- opencode-go: held by a limit since", filed["body"])

    def test_a_ticket_queued_beside_a_later_state_or_claimed_elsewhere_is_not_counted(self):
        self.queued_ticket(653, "loom:queued", "loom:shipped")
        self.queued_ticket(629, "loom:queued", "claim:another-operator")
        self.queued_ticket(609, "loom:queued", "loom:merged")
        self.queued_ticket(41, "loom:queued")
        for at in (T0, T0 + 3 * 3600):
            self.out_of_credit(("anthropic",), at)
            self.run_at(at)
        (filed,) = self.breaches()
        self.assertIn("1 tickets of its tier queued: #41\n", filed["body"])
        for shipped in ("#653", "#629", "#609"):
            self.assertNotIn(shipped, filed["body"])

    def test_no_topup_is_read_from_the_environment(self):
        self.assertEqual(station.Settings.from_env({"LOOM_NO_TOPUP": "Anthropic, zen"}).no_topup,
                         ["anthropic", "zen"])
        self.assertEqual(station.Settings.from_env({}).no_topup, [])

    def test_a_lane_pull_open_seven_hours_whose_ticket_is_not_a_loom_ticket_files_a_breach(self):
        self.lane_ticket(481, labels=("epic",))
        self.lane_pull(534, 481, T0 - 7 * 3600)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("Lane pull request #534 open 7.0 hours", filed["title"])
        self.assertIn("no open loom:ticket carries it", filed["title"])
        self.assertIn("#481, which is not an open loom:ticket", filed["body"])
        self.assertIn("`lane-pr-stalled`", filed["body"])

    def test_a_lane_pull_whose_ticket_was_closed_is_a_breach_for_the_same_reason(self):
        self.lane_ticket(481, state="closed")
        self.lane_pull(534, 481, T0 - 7 * 3600)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("no open loom:ticket carries it", filed["title"])
        self.assertIn("#481, which is not an open loom:ticket", filed["body"])

    def test_a_lane_pull_past_the_ladder_files_a_breach_and_five_hours_old_does_not(self):
        self.lane_ticket(506, labels=("loom:ticket", "loom:failed"))
        self.lane_ticket(507, labels=("loom:ticket", "loom:failed"))
        self.lane_pull(657, 506, T0 - 7 * 3600)
        self.lane_pull(658, 507, T0 - 5 * 3600)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertEqual("Lane pull request #657 open 7.0 hours: ticket past the model ladder",
                         filed["title"])
        self.assertIn("past the last rung of the model ladder", filed["body"])
        self.assertIn("#506", filed["body"])
        self.assertNotIn("#658", filed["title"] + filed["body"])

    def test_a_lane_pull_with_open_review_threads_and_no_fix_round_files_a_breach(self):
        self.lane_ticket(602)
        self.lane_pull(660, 602, T0 - 7 * 3600)
        self.bot_threads(2)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("2 review threads, no fix round", filed["title"])
        self.assertLessEqual(len(filed["title"]), 72)
        self.assertIn("2 open review threads and no fix round queued", filed["body"])
        self.assertIn("bus.brain fix", filed["body"])

    def test_a_lane_pull_with_its_fix_round_queued_or_waiting_to_merge_files_nothing(self):
        self.lane_ticket(603, labels=("loom:ticket", "loom:briefed", "lane:zen"))
        self.lane_pull(661, 603, T0 - 7 * 3600)
        self.bot_threads()
        self.gh.comment(603, T0 - 3600,
                        "<!-- loom:fix v1 -->\n<!-- loom:fix-head %x -->" % 661)
        self.run_at(T0)
        self.assertEqual(self.breaches(), [], self.said)
        # The same pull request clean: the brain merges it.
        self.gh.threads = []
        self.lane_ticket(603)
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(self.breaches(), [], self.said)

    def test_a_stale_lane_pull_nothing_can_read_is_skipped_rather_than_reported(self):
        self.lane_pull(670, 699, T0 - 7 * 3600)  # the ticket it names is gone
        self.lane_pull(671, 481, T0 - 7 * 3600, author="stranger")
        self.run_at(T0)
        self.assertEqual(self.breaches(), [], self.said)
        self.assertTrue(any("unreadable" in line for line in self.said), self.said)

    def unlinked_goal(self):
        """A goal home with one open ticket under no epic."""
        self.gh.subs[(REPO, GOAL)] = [ticket(7)]

    def test_a_goal_whose_tickets_all_sit_under_epics_is_healthy(self):
        self.gh.subs[(REPO, GOAL)] = [epic(40)]
        self.gh.subs[(REPO, 40)] = [ticket(7), ticket(8, state="closed")]
        self.run_at(T0)
        self.assertEqual(self.gh.filed(), [], self.said)

    def test_each_epic_shape_opens_its_invariant_once_and_clearing_closes_it(self):
        cases = {
            "ticket-outside-epic": (lambda: self.gh.subs.update({(REPO, GOAL): [ticket(7)]}),
                                    "1 open tickets sit under no epic across 1 goal homes"),
            "epic-over-cap": (lambda: self.gh.subs.update({
                (REPO, GOAL): [epic(40)],
                (REPO, 40): [ticket(n, state="closed") for n in range(50, 66)]}),
                "holds 16 tickets (cap 15)"),
            "goal-epics-over-cap": (lambda: self.gh.subs.update({
                (REPO, GOAL): [epic(n) for n in range(40, 47)]}),
                "has 7 open epics (cap 6)"),
            "closed-epic-open-tickets": (lambda: self.gh.subs.update({
                (REPO, GOAL): [epic(40, state="closed")], (REPO, 40): [ticket(7)]}),
                "Closed epic #40 holds 1 open tickets"),
        }
        for name, (shape, title) in cases.items():
            with self.subTest(name):
                self.setUp()
                shape()
                self.run_at(T0)
                filed = self.gh.filed()
                self.assertEqual(len(filed), 1, self.said)
                self.assertIn(title, filed[0]["title"])
                self.assertIn("`%s`" % name, filed[0]["body"])
                # Still broken a pass later: nothing more is filed.
                self.run_at(T0 + invariants.CHECK_EVERY)
                self.assertEqual(len(self.gh.filed()), 1)
                self.assertEqual(filed[0]["state"], "open")
                # Cleared: once the shape is gone long enough, the issue closes.
                self.gh.subs.clear()
                self.run_at(T0 + 2 * invariants.CHECK_EVERY)
                self.run_at(T0 + invariants.CLEAR_AFTER + 3 * invariants.CHECK_EVERY)
                self.assertEqual(filed[0]["state"], "closed", self.said)

    def test_a_goal_home_exception_raises_its_epic_cap(self):
        self.gh.goal_issues[REPO][0]["body"] = "Epic cap: 8 until 2999-12-31\n"
        self.gh.subs[(REPO, GOAL)] = [epic(n) for n in range(40, 47)]
        self.run_at(T0)
        self.assertEqual(self.gh.filed(), [], self.said)

    def test_repeated_untrusted_labeler_refusals_open_the_invariant_once(self):
        refusals = os.path.join(self.home, "state", "station", "labeler_refusals.json")
        self.write(refusals, json.dumps({"loom-app[bot]": [T0 - 60 * n for n in range(1, 5)]}))
        self.run_at(T0)
        self.run_at(T0 + invariants.CHECK_EVERY)
        (issue,) = self.gh.filed()
        self.assertIn("loom-app[bot]", issue["title"])

    def test_breaches_wait_while_the_github_budget_is_low(self):
        self.unlinked_goal()
        self.gh.rate = {"core": (10, 5000, int(T0) + 3600)}
        self.run_at(T0)
        self.assertEqual(self.gh.filed(), [])
        self.gh.rate = {}
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(len(self.gh.filed()), 1)

    def test_the_station_standup_on_the_invariant_goal_lists_open_breaches(self):
        self.unlinked_goal()
        st = self.run_at(T0)
        number = self.gh.filed()[0]["number"]
        body = st._overdue_standup_body([], None, st._open_breaches(REPO, GOAL))
        self.assertIn("Invariants broken: #%d 1 open tickets sit under no epic across 1 goal homes."
                      % number, body)
        self.assertNotIn("Invariants broken", st._overdue_standup_body([], None))


class FilerTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        self.state = {}

    def file(self, breach, now=T0, write=True):
        invariants.Filer(self.gh, REPO, GOAL, self.state, now, self.said.append, write).run([breach])

    def breach(self, at=T0, evidence=("one line",)):
        return invariants.Breach("goal-full", "x/y#1", "Goal home #1 has 2 tickets not linked to it",
                                 tuple(evidence), at)

    def test_a_breach_files_under_the_goals_repairs_epic(self):
        self.gh.issues[GOAL] = {"number": GOAL, "id": 9000 + GOAL, "state": "open",
                                "title": "Goal", "labels": [{"name": "loom:goal"}], "body": ""}
        self.file(self.breach())
        epics = [i for i in self.gh.issues.values() if i.get("title") == brain.REPAIRS_TITLE]
        self.assertEqual(len(epics), 1)
        epic = epics[0]["number"]
        (filed,) = [i for i in self.gh.issues.values() if "loom:invariant" in i.get("labels", [])]
        self.assertTrue(filed["body"].endswith("Part of #%d" % epic))
        self.assertEqual([c["number"] for c in self.gh.subs[(REPO, epic)]], [filed["number"]])
        self.assertIn(epic, [c["number"] for c in self.gh.subs[(REPO, GOAL)]])
        # The next pass finds the same epic: none is opened twice.
        self.file(invariants.Breach("goal-full", "x/y#2", "Another", ("line",), T0))
        self.assertEqual(len([i for i in self.gh.issues.values()
                              if i.get("title") == brain.REPAIRS_TITLE]), 1)

    def rolling(self, title, evidence, at=T0):
        return invariants.Breach("ticket-outside-epic", invariants.OUTSIDE_EPIC_SUBJECT,
                                 title, tuple(evidence), at, rolling=True)

    def test_a_rolling_breach_updates_its_one_issue_in_place(self):
        self.file(self.rolling("1 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 1 open, #7",)))
        (number,) = self.gh.issues
        self.assertIn("Last seen by the station: %s" % invariants.utc(T0),
                      self.gh.issues[number]["body"])
        # Unchanged a pass later: nothing is written, not even the last-seen line.
        writes = len(self.gh.writes)
        self.file(self.rolling("1 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 1 open, #7",), at=T0 + 300), now=T0 + 300)
        self.assertEqual(len(self.gh.writes), writes)
        # A new ticket outside an epic: the same issue is rewritten, none is filed.
        self.file(self.rolling("2 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 2 open, #7, #8",), at=T0 + 600), now=T0 + 600)
        self.assertEqual(list(self.gh.issues), [number])
        issue = self.gh.issues[number]
        self.assertEqual(issue["title"], "2 open tickets sit under no epic across 1 goal homes")
        self.assertIn("Goal home #30: 2 open, #7, #8", issue["body"])
        self.assertNotIn("1 open, #7\n", issue["body"])
        self.assertIn("Last seen by the station: %s" % invariants.utc(T0 + 600), issue["body"])
        self.assertRegex(issue["body"], invariants.MARKER_RE)
        self.assertTrue(issue["body"].rstrip().endswith("Part of #%d" % GOAL))

    def test_a_closed_rolling_issue_reopens_with_the_current_list(self):
        self.file(self.rolling("1 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 1 open, #7",)))
        (number,) = self.gh.issues
        self.gh.issues[number].update(state="closed", closed_at=stamp(T0 + 60))
        self.file(self.rolling("3 open tickets sit under no epic across 2 goal homes",
                               ("Goal home #30: 2 open, #7, #8",), at=T0 + 30), now=T0 + 30)
        self.assertEqual(self.gh.issues[number]["state"], "closed")
        self.file(self.rolling("3 open tickets sit under no epic across 2 goal homes",
                               ("Goal home #30: 2 open, #7, #8",), at=T0 + 120), now=T0 + 120)
        issue = self.gh.issues[number]
        self.assertEqual(issue["state"], "open")
        self.assertIn("2 open, #7, #8", issue["body"])
        self.assertEqual(list(self.gh.issues), [number])

    def test_an_old_per_goal_issue_is_folded_into_the_rolling_one(self):
        old = invariants.Breach("ticket-outside-epic", "%s#30" % REPO,
                                "Goal home #30 has 1 open tickets under no epic", ("x",), T0)
        self.file(old)
        (old_number,) = self.gh.issues
        self.gh.issues[old_number].update(labels=[{"name": x} for x in invariants.LABELS])
        # Another invariant's quiet issue is left to the usual clear rule.
        other = invariants.Breach("lane-no-success", "zen", "Lane zen: 5 runs and none done",
                                  ("x",), T0)
        self.file(other)
        other_number = max(self.gh.issues)
        self.gh.issues[other_number].update(labels=[{"name": x} for x in invariants.LABELS])
        self.file(self.rolling("1 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 1 open, #7",), at=T0 + 300), now=T0 + 300)
        rolling = self.state[self.rolling("t", ()).key]
        self.assertEqual(self.gh.issues[old_number]["state"], "closed")
        (note,) = self.gh.comments[(REPO, old_number)]
        self.assertIn("#%d" % rolling, note["body"])
        self.assertNotIn("cleared", note["body"])
        self.assertEqual(self.gh.issues[rolling]["state"], "open")
        self.assertEqual(self.gh.issues[other_number]["state"], "open")

    def test_an_old_per_goal_issue_waits_while_the_rolling_issue_stays_closed(self):
        self.file(self.rolling("1 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 1 open, #7",)))
        (rolling,) = self.gh.issues
        self.gh.issues[rolling].update(state="closed", closed_at=stamp(T0 + 600))
        old = invariants.Breach("ticket-outside-epic", "%s#30" % REPO,
                                "Goal home #30 has 1 open tickets under no epic", ("x",), T0)
        self.file(old)
        old_number = max(self.gh.issues)
        self.gh.issues[old_number].update(labels=[{"name": x} for x in invariants.LABELS])
        # Evidence older than the close: nothing reopens, so nothing folds.
        self.file(self.rolling("1 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 1 open, #7",), at=T0 + 300), now=T0 + 300)
        self.assertEqual(self.gh.issues[rolling]["state"], "closed")
        self.assertEqual(self.gh.issues[old_number]["state"], "open")

    def test_a_long_breach_title_is_clipped_to_72_and_kept_whole_in_the_body(self):
        long = ("Repeated rewrite skip for one hour: the agent answered with no brief "
                "and no reason on every issue it was given")
        self.file(invariants.Breach("repeated-failure", "k|r", long, ("line",), T0))
        (filed,) = self.gh.issues.values()
        self.assertLessEqual(len(filed["title"]), 72)
        self.assertTrue(filed["title"].startswith("Repeated rewrite skip for one hour:"))
        self.assertIn(long, filed["body"])

    def test_a_closed_issue_reopens_only_for_evidence_newer_than_the_close(self):
        self.file(self.breach())
        (number,) = self.gh.issues
        self.gh.issues[number].update(state="closed", closed_at=stamp(T0 + 60))
        self.file(self.breach(at=T0))
        self.assertEqual(self.gh.issues[number]["state"], "closed")
        self.file(self.breach(at=T0 + 120))
        self.assertEqual(self.gh.issues[number]["state"], "open")
        self.assertEqual(len(self.gh.issues), 1)
        self.assertTrue(any(w[1].endswith("/%d/comments" % number) for w in self.gh.writes))

    def test_a_closed_ticket_is_never_reopened_patched_or_commented_on(self):
        # loom#1223: an invariant issue rewritten into a ticket was reopened by
        # the filer and closed by the ticket path 18 times in one day.
        self.file(self.breach())
        (number,) = self.gh.issues
        self.gh.issues[number].update(
            state="closed", closed_at=stamp(T0 + 60), state_reason="completed",
            labels=[{"name": x} for x in ("loom:ticket", "loom:invariant", "loom:rewritten")],
            body=self.gh.issues[number]["body"] + "\nA ticket brief now.")
        before = dict(self.gh.issues[number])
        self.gh.writes.clear()
        self.file(self.breach(at=T0 + 120), now=T0 + 120)
        self.assertFalse([w for w in self.gh.writes if "/issues/%d" % number in w[1]
                          and w[0] in ("PATCH", "POST")], self.gh.writes)
        self.assertEqual(self.gh.issues[number], before)
        self.assertNotIn((REPO, number), self.gh.comments)
        # The breach stands, so one separate invariant issue refs the ticket.
        (other,) = [n for n in self.gh.issues if n != number]
        self.assertIn("Refs #%d" % number, self.gh.issues[other]["body"])
        self.assertRegex(self.gh.issues[other]["body"], invariants.MARKER_RE)
        self.assertEqual(self.state[self.breach().key], other)
        # Later passes use that issue, not the ticket and not a third one.
        self.file(self.breach(at=T0 + 240), now=T0 + 240)
        self.assertEqual(sorted(self.gh.issues), sorted([number, other]))

    def test_an_open_ticket_is_not_rewritten_back_to_the_invariant_text(self):
        self.file(self.rolling("1 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 1 open, #7",)))
        (number,) = self.gh.issues
        self.gh.issues[number].update(
            labels=[{"name": x} for x in ("loom:ticket", "loom:invariant")],
            title="Ship the intake epic", body=self.gh.issues[number]["body"] + "\nbrief")
        self.gh.writes.clear()
        self.file(self.rolling("2 open tickets sit under no epic across 1 goal homes",
                               ("Goal home #30: 2 open, #7, #8",), at=T0 + 600), now=T0 + 600)
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.gh.issues[number]["title"], "Ship the intake epic")

    def test_a_plain_invariant_issue_beside_a_ticket_is_the_breachs_issue(self):
        self.file(self.breach())
        (ticket,) = self.gh.issues
        self.gh.issues[ticket].update(state="closed", closed_at=stamp(T0 + 60),
                                      labels=[{"name": "loom:ticket"}, {"name": "loom:invariant"}])
        self.file(self.breach(at=T0 + 120), now=T0 + 120)
        (plain,) = [n for n in self.gh.issues if n != ticket]
        self.state.clear()
        self.gh.issues[plain].update(labels=[{"name": x} for x in invariants.LABELS])
        self.file(self.breach(at=T0 + 180), now=T0 + 180)
        self.assertEqual(sorted(self.gh.issues), sorted([ticket, plain]))
        self.assertEqual(self.state[self.breach().key], plain)

    def test_a_breach_that_keeps_standing_reopens_its_issue_once_over_ten_passes(self):
        self.file(self.breach())
        (number,) = self.gh.issues
        self.gh.issues[number].update(labels=[{"name": x} for x in invariants.LABELS])
        self.gh.writes.clear()
        for i in range(1, 11):
            at = T0 + 100 * i
            # Whatever closes it (the ticket path, a person) closes it again.
            if self.gh.issues[number]["state"] == "open" and i > 1:
                self.gh.issues[number].update(state="closed", closed_at=stamp(at - 50))
            elif i == 1:
                self.gh.issues[number].update(state="closed", closed_at=stamp(at - 50))
            self.file(self.breach(at=at), now=at)
        reopens = [w for w in self.gh.writes if w[2] == {"state": "open"}]
        self.assertEqual(len(reopens), 1)
        notes = self.gh.comments[(REPO, number)]
        self.assertEqual(len(notes), 1)
        self.assertLessEqual(len([w for w in self.gh.writes if w[1].endswith("/comments")]), 1)
        self.assertTrue(any("not reopened" in line for line in self.said))

    def test_a_breach_that_clears_and_returns_edits_the_one_broken_again_note(self):
        self.file(self.breach(at=T0 - 7 * 3600), now=T0 - 7 * 3600)
        (number,) = self.gh.issues
        self.gh.issues[number].update(state="closed", closed_at=stamp(T0 - 6 * 3600),
                                      labels=[{"name": x} for x in invariants.LABELS])
        self.file(self.breach(at=T0 - 5 * 3600), now=T0 - 5 * 3600)
        self.assertEqual(self.gh.issues[number]["state"], "open")
        self.gh.comments[(REPO, number)].append(
            {"id": 900, "body": "The invariant cleared by x; its last breach was observed earlier.",
             "created_at": stamp(T0 - 3 * 3600)})
        self.gh.issues[number].update(state="closed", closed_at=stamp(T0 - 3 * 3600))
        self.file(self.breach(at=T0), now=T0)
        self.assertEqual(self.gh.issues[number]["state"], "open")
        notes = [c for c in self.gh.comments[(REPO, number)] if invariants.BROKEN_MARKER in c["body"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("Reopened 2 times; last at %s" % invariants.utc(T0), notes[0]["body"])

    def test_lost_state_finds_the_issue_by_its_marker(self):
        self.file(self.breach())
        self.state.clear()
        self.file(self.breach())
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(list(self.state.values()), list(self.gh.issues))

    def test_the_marker_carries_a_hash_not_the_subject(self):
        self.file(self.breach())
        body = next(iter(self.gh.issues.values()))["body"]
        self.assertNotIn("x/y#1", body)
        self.assertRegex(body, invariants.MARKER_RE)

    def test_a_line_failing_the_written_text_check_is_withheld(self):
        self.file(self.breach(evidence=("this fixes #12", "fine")))
        body = next(iter(self.gh.issues.values()))["body"]
        self.assertIn(invariants.WITHHELD, body)
        self.assertNotIn("fixes #12", body)

    def test_at_most_a_few_writes_a_pass(self):
        many = [invariants.Breach("lane-held", "lane-%d" % i, "Lane %d held" % i, (), T0)
                for i in range(invariants.WRITES_MAX + 3)]
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0, self.said.append).run(many)
        self.assertEqual(len(self.gh.issues), invariants.WRITES_MAX)

    def test_quiet_triage_issue_is_explained_and_closed_but_recurring_stays_open(self):
        breach = self.breach(at=T0 - 7 * 3600)
        self.file(breach, now=T0 - 7 * 3600)
        number = next(iter(self.gh.issues))
        issue = self.gh.issues[number]
        issue.update(created_at=stamp(T0 - 7 * 3600), labels=[{"name": x} for x in invariants.LABELS])
        self.gh.comments[(REPO, number)] = [
            {"body": "Broken again after this was closed: evidence", "created_at": stamp(T0 - 2 * 86400)},
            {"body": "Broken again after this was closed: evidence", "created_at": stamp(T0 - 86400)},
        ]
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0, self.said.append).run([])
        self.assertEqual(issue["state"], "open")
        self.assertIn("loom:recurring", [label["name"] if isinstance(label, dict) else label
                                          for label in issue["labels"]])
        self.assertEqual(sum("cleared" in c.get("body", "") for c in self.gh.comments[(REPO, number)]), 0)

    def test_a_quiet_issue_closed_and_broken_again_says_why_it_closes_the_second_time(self):
        # loom#651 closed a second time with no word: the first close's note
        # counted as the explanation (2026-10-20).
        self.file(self.breach(at=T0 - 7 * 3600), now=T0 - 7 * 3600)
        number = next(iter(self.gh.issues))
        issue = self.gh.issues[number]
        issue.update(created_at=stamp(T0 - 3 * 86400), labels=[{"name": x} for x in invariants.LABELS])
        self.gh.comments[(REPO, number)] = [
            {"body": "The invariant cleared by then; its last breach was observed earlier.",
             "created_at": stamp(T0 - 2 * 86400)},
            {"body": "Broken again after this was closed: evidence", "created_at": stamp(T0 - 7 * 3600)},
        ]
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0, self.said.append).run([])
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(sum("cleared" in c.get("body", "") for c in self.gh.comments[(REPO, number)]), 2)

    def test_quiet_triage_issue_closes_once(self):
        self.file(self.breach(at=T0 - 7 * 3600), now=T0 - 7 * 3600)
        number = next(iter(self.gh.issues))
        issue = self.gh.issues[number]
        issue.update(created_at=stamp(T0 - 7 * 3600), labels=[{"name": x} for x in invariants.LABELS])
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0, self.said.append).run([])
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue.get("state_reason"), "not_planned")
        self.assertEqual(sum("cleared" in c.get("body", "") for c in self.gh.comments[(REPO, number)]), 1)
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0 + invariants.CHECK_EVERY,
                         self.said.append).run([])
        self.assertEqual(sum("cleared" in c.get("body", "") for c in self.gh.comments[(REPO, number)]), 1)

    def test_a_closed_issue_in_the_listing_is_never_read_again(self):
        # 131 of 146 invariant issues were closed, and each was read every pass.
        self.file(self.breach(at=T0 - 7 * 3600), now=T0 - 7 * 3600)
        number = next(iter(self.gh.issues))
        self.gh.issues[number].update(state="closed")
        self.gh.calls.clear()
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0 + invariants.CHECK_EVERY,
                         self.said.append).run([])
        self.assertNotIn(("GET", "/repos/%s/issues/%d" % (REPO, number)), self.gh.calls)

    def test_a_rewritten_issue_closes_after_the_clear_wait_like_a_triage_one(self):
        self.file(self.breach(at=T0 - 7 * 3600), now=T0 - 7 * 3600)
        number = next(iter(self.gh.issues))
        issue = self.gh.issues[number]
        issue.update(created_at=stamp(T0 - 7 * 3600),
                     labels=[{"name": invariants.LABEL}, {"name": "loom:rewritten"}])
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0 - 2 * 3600,
                         self.said.append).run([])
        self.assertEqual(issue["state"], "open")
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0, self.said.append).run([])
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue.get("state_reason"), "not_planned")
        self.assertEqual(sum("cleared" in c.get("body", "") for c in self.gh.comments[(REPO, number)]), 1)


class InvariantTest(unittest.TestCase):
    goal = Ref("%s#%d" % (REPO, GOAL), "#%d" % GOAL)

    def j(self, at, holder="muse", steps=("status failed",)):
        return Journal(at, self.goal, holder, stamp(at), steps)

    def test_repeated_failure_opens_after_an_hour_and_success_suppresses_it(self):
        events = [Event(T0 - 3600 + i * 60, "rewrite_skipped", "no command")
                  for i in range(61)]
        (breach,) = invariants.repeated_failure(Facts(T0, events=events))
        self.assertEqual(breach.invariant, "repeated-failure")
        events.append(Event(T0 - 30, "rewrite_succeeded", "no command"))
        self.assertEqual(invariants.repeated_failure(Facts(T0, events=events)), [])

    def test_repeated_failure_success_is_per_kind_not_per_reason(self):
        events = [Event(T0 - 3600 + i * 60, kind, "no command")
                  for i in range(61) for kind in ("rewrite_skipped", "rewrite_failed")]
        events.append(Event(T0 - 1800, "rewrite_succeeded", "rewrote the body"))
        self.assertEqual(invariants.repeated_failure(Facts(T0, events=events)), [])

    def test_repeated_failure_that_stopped_an_hour_ago_is_not_a_breach(self):
        events = [Event(T0 - 7000 + i * 290, "model_wait", "no lane") for i in range(12)]
        self.assertEqual(invariants.repeated_failure(Facts(T0, events=events)), [])

    def test_repeated_failure_needs_full_hour(self):
        events = [Event(T0 - 3599, "rewrite_skipped", "no command"),
                  Event(T0, "rewrite_skipped", "no command")]
        self.assertEqual(invariants.repeated_failure(Facts(T0, events=events)), [])

    def test_one_failed_tick_between_good_ones_is_not_a_breach(self):
        f = Facts(T0, journals=[self.j(T0 - 3000), self.j(T0 - 1800, steps=()), self.j(T0 - 600)])
        self.assertEqual(invariants.brain_failing(f), [])

    def test_one_tick_journaling_on_two_goals_counts_once(self):
        other = Ref("%s#31" % REPO, "#31")
        f = Facts(T0, journals=[self.j(T0 - 600), Journal(T0 - 590, other, "muse", stamp(T0 - 600),
                                                           ("merge degraded",))])
        self.assertEqual(invariants.brain_failing(f), [])

    def test_failed_steps_reads_the_step_lines_only(self):
        body = journal(T0) + "\n- Waiting: #12 failed on lane codex\n- Brief step: degraded, cap reached"
        self.assertEqual(invariants.failed_steps(body),
                         ("status failed: (GitHub answered 502)", "brief degraded: cap reached"))

    def test_the_brain_is_silent_after_ninety_minutes_and_not_before_one_was_seen(self):
        self.assertEqual(invariants.brain_silent(Facts(T0, journals=[])), [])
        self.assertEqual(invariants.brain_silent(Facts(T0, journals=[self.j(T0 - 80 * 60)])), [])
        (b,) = invariants.brain_silent(Facts(T0, journals=[], brain_seen=T0 - 100 * 60))
        self.assertIn("100 minutes", b.title)

    def test_two_reports_within_ninety_minutes_on_one_goal(self):
        other = Ref("%s#31" % REPO, "#31")
        posts = [Posted(self.goal, "report", T0 - 9000, ""), Posted(other, "report", T0 - 8800, ""),
                 Posted(self.goal, "report", T0 - 3600, "")]
        self.assertEqual(invariants.marker_cadence(Facts(T0, posts=posts)), [])
        posts.append(Posted(self.goal, "report", T0 - 600, ""))
        (b,) = invariants.marker_cadence(Facts(T0, posts=posts))
        self.assertEqual((b.invariant, b.subject), ("report-cadence", self.goal.key))

    def test_a_done_run_or_too_few_runs_is_not_a_breach(self):
        runs = [Run("zen", False, T0 - i * 60, "x") for i in range(4)]
        self.assertEqual(invariants.lane_no_success(Facts(T0, runs=runs)), [])
        runs.append(Run("zen", True, T0, ""))
        runs.append(Run("zen", False, T0, "x"))
        self.assertEqual(invariants.lane_no_success(Facts(T0, runs=runs)), [])

    def test_runs_that_ended_at_a_limit_an_expired_key_or_a_factory_incident_are_no_breach(self):
        # Every run agy made for a day ended at its plan's quota; the lane was
        # out of quota, which lane_held reports, not broken (loom#651, 2026-10-20).
        for reason in ("[limit] limit: all models limited: gemini until tomorrow",
                       "[auth] API key expired", "[factory] setup failed: base check exited 1"):
            with self.subTest(reason=reason):
                runs = [Run("agy", False, T0 - i * 600, reason) for i in range(34)]
                self.assertEqual(invariants.lane_no_success(Facts(T0, runs=runs)), [])
        runs = [Run("agy", False, T0 - i * 600, "[limit] provider limit") for i in range(30)]
        runs += [Run("agy", False, T0 - i * 600 - 300, "[work] agent made no commit") for i in range(5)]
        (b,) = invariants.lane_no_success(Facts(T0, runs=runs))
        self.assertEqual(b.title, "Lane agy: 5 runs in 24 hours and none done")

    def test_briefs_parked_unrun_are_no_breach(self):
        # A fix brief whose pull request merged or closed, or whose branch
        # moved, is parked before the agent runs: the lane did nothing, so
        # nothing about the lane is shown (loom#736, 2026-10-20).
        for reason in ("[work] pull request #1228 is no longer open on loom/736-x (MERGED loom/736-x)",
                       "[work] pull request #9 is no longer open on loom/9-y (CLOSED loom/9-y)",
                       "[work] branch moved: loom/5-z is at 0123456789ab on the remote, not ba9876543210"):
            with self.subTest(reason=reason):
                runs = [Run("zen", False, T0 - i * 600, reason) for i in range(6)]
                self.assertEqual(invariants.lane_no_success(Facts(T0, runs=runs)), [])
                runs.insert(0, Run("zen", True, T0 - 9 * 3600, ""))
                self.assertEqual(invariants.lane_failing_streak(Facts(T0, runs=runs)), [])

    def test_a_lane_failing_its_last_runs_after_an_earlier_success_is_a_breach(self):
        runs = [Run("zen", True, T0 - 9 * 3600, "")]
        runs += [Run("zen", False, T0 - i * 600, "[transient] agent exited 1") for i in range(5)]
        (b,) = invariants.lane_failing_streak(Facts(T0, runs=runs))
        self.assertEqual((b.invariant, b.subject), ("lane-failing-streak", invariants.LANES_SUBJECT))
        self.assertEqual(b.title, "1 lane failed its last 5 runs")
        self.assertTrue(any(line.startswith("zen: 5 runs in a row failed") for line in b.evidence))
        self.assertEqual(invariants.lane_no_success(Facts(T0, runs=runs)), [])

    def test_limits_expired_keys_a_recent_success_or_no_success_make_no_streak(self):
        early = Run("zen", True, T0 - 9 * 3600, "")
        fails = [Run("zen", False, T0 - i * 600, "[transient] agent exited 1") for i in range(4)]
        limited = [Run("zen", False, T0 - 5 * 600, "[limit] provider limit"),
                   Run("zen", False, T0 - 6 * 600, "[auth] API key expired"),
                   Run("zen", False, T0 - 7 * 600, "[factory] setup failed: base check exited 1")]
        self.assertEqual(invariants.lane_failing_streak(
            Facts(T0, runs=[early] + fails + limited)), [])
        recent = [Run("zen", True, T0 - 50, "")]
        self.assertEqual(invariants.lane_failing_streak(
            Facts(T0, runs=[early] + recent + fails + [fails[0]._replace(at=T0 - 3000)])), [])
        # Never a success all day: lane_no_success's breach, not this one.
        self.assertEqual(invariants.lane_failing_streak(
            Facts(T0, runs=fails + [fails[0]._replace(at=T0 - 3000)])), [])

    def test_a_lane_out_of_credit_on_a_no_topup_account_is_not_held(self):
        ticket = Ref("%s#5" % REPO, "#5")
        f = Facts(T0, held={"anthropic": T0 - 3 * 3600, "anthropic-2": T0 - 3 * 3600},
                  queued=[(ticket, ("anthropic", "anthropic-2"))], no_topup=("anthropic-2",))
        (b,) = invariants.lane_held(f)
        self.assertEqual([line.split(":")[0] for line in b.evidence[1:]], ["anthropic"])

    def test_a_lane_held_over_two_hours_while_its_tier_waits(self):
        ticket = Ref("%s#5" % REPO, "#5")
        f = Facts(T0, held={"agy": T0 - 3 * 3600, "zen": T0 - 600},
                  queued=[(ticket, ("agy", "zen"))])
        (b,) = invariants.lane_held(f)
        self.assertEqual([line.split(":")[0] for line in b.evidence[1:]], ["agy"])
        self.assertIn("#5", " ".join(b.evidence))

    def test_the_four_epic_shapes(self):
        t7, e40 = Ref("%s#7" % REPO, "#7"), Ref("%s#40" % REPO, "#40")
        ok = invariants.GoalShape(self.goal, 6, (invariants.EpicShape(e40, True, 15, (t7,)),), ())
        f = Facts(T0, shapes=[ok])
        for check in (invariants.ticket_outside_epic, invariants.epic_over_cap,
                      invariants.goal_epics_over_cap, invariants.closed_epic_open_tickets):
            self.assertEqual(check(f), [])
            self.assertEqual(check(Facts(T0)), [])  # not read: nothing found
        bad = invariants.GoalShape(self.goal, 1, (
            invariants.EpicShape(e40, True, 16, ()),
            invariants.EpicShape(Ref("%s#41" % REPO, "#41"), True, 1, ()),
            invariants.EpicShape(Ref("%s#42" % REPO, "#42"), False, 2, (t7,))), (t7,))
        f = Facts(T0, shapes=[bad])
        (outside,) = invariants.ticket_outside_epic(f)
        self.assertEqual(outside.subject, invariants.OUTSIDE_EPIC_SUBJECT)
        self.assertEqual([b.subject for b in invariants.epic_over_cap(f)], [e40.key])
        self.assertEqual([b.subject for b in invariants.goal_epics_over_cap(f)], [self.goal.key])
        self.assertEqual([b.subject for b in invariants.closed_epic_open_tickets(f)],
                         ["%s#42" % REPO])

    def test_tickets_outside_an_epic_in_many_goals_are_one_rolling_breach(self):
        """One rolling breach for every goal home, not one issue per goal:
        the sweep found 18 issues for this one cause (infra#5613)."""
        other = Ref("%s#12" % OTHER, "Goal home issue 12 of a private repo")
        shapes = [invariants.GoalShape(self.goal, 6, (), (Ref("%s#7" % REPO, "#7"),)),
                  invariants.GoalShape(other, 6, (), tuple(
                      Ref("%s#%d" % (OTHER, n), "a private ticket %d" % n) for n in (1, 2)))]
        (b,) = invariants.ticket_outside_epic(Facts(T0, shapes=shapes))
        self.assertTrue(b.rolling)
        self.assertEqual(b.subject, invariants.OUTSIDE_EPIC_SUBJECT)
        self.assertIn("3 open tickets", b.title)
        self.assertIn("2 goal homes", b.title)
        self.assertTrue(any(self.goal.label in line and "#7" in line for line in b.evidence))
        self.assertTrue(any(other.label in line and "2 open" in line for line in b.evidence))
        # The same key whatever the goals: a new goal joins the one issue.
        (alone,) = invariants.ticket_outside_epic(Facts(T0, shapes=shapes[:1]))
        self.assertEqual(alone.key, b.key)

    def test_a_plan_check_that_passed_or_failed_recently_is_not_a_breach(self):
        pr = Ref("%s#7" % REPO, "pull request #7")
        checks = [Check(pr, "Chief", "success", T0 - 3600, ""), Check(pr, "Chief", "failure", T0 - 60, "")]
        self.assertEqual(invariants.plan_check_failed(Facts(T0, checks=checks)), [])

    def test_a_remote_lane_past_its_beat_max(self):
        limit = invariants.REMOTE_LANES["muse"]["beat_max"]
        self.assertEqual(invariants.remote_silent(Facts(T0, beats={"muse": None})), [])
        self.assertEqual(invariants.remote_silent(Facts(T0, beats={"muse": T0 - limit + 60})), [])
        self.assertEqual(len(invariants.remote_silent(Facts(T0, beats={"muse": T0 - limit - 60}))), 1)

    def test_a_lane_that_held_a_running_ticket_while_silent_is_named(self):
        run = SilentRun("codex", 31 * 60, (Ref("%s#1" % REPO, "#1"),), T0 - 60)
        (b,) = invariants.silent_lane(Facts(T0, silent_runs=[run]))
        self.assertEqual(b.title, "1 lane held a running ticket while silent")
        self.assertTrue(any(line.startswith("codex: silent for 0.5 hours") for line in b.evidence))
        self.assertIn("#1", " ".join(b.evidence))
        self.assertEqual(invariants.silent_lane(Facts(T0)), [])
        old = run._replace(at=T0 - invariants.SILENT_KEEP - 1)
        self.assertEqual(invariants.silent_lane(Facts(T0, silent_runs=[old])), [])

    def test_tickets_waiting_on_the_same_reason_for_hours_are_one_line(self):
        # The factory logged "waits: no free lane in its rung" for six fix
        # rounds every pass for nine hours, and nothing raised it (2026-10-21).
        limit = invariants.WAIT_LIMIT
        rung = "on lane agy, at a limit: no free lane in its rung"
        waits = [Wait(Ref("%s#%d" % (REPO, n), "#%d" % n), rung, T0 - limit - n * 600) for n in (1, 2)]
        waits.append(Wait(Ref("%s#3" % REPO, "#3"), "no free lane in tier medium", T0 - limit - 60))
        waits.append(Wait(Ref("%s#4" % REPO, "#4"), "no free lane in tier hard", T0 - limit + 600))
        (b,) = invariants.ticket_waiting(Facts(T0, waits=waits))
        self.assertEqual((b.invariant, b.subject, b.rolling),
                         ("ticket-waiting", invariants.TICKETS_SUBJECT, True))
        self.assertEqual(b.title, "3 tickets waited over 6 hours for the same reason")
        rung_line = next(x for x in b.evidence if x.startswith(rung))
        self.assertIn("2 tickets, longest", rung_line)
        self.assertIn("#1", rung_line)
        self.assertIn("#2", rung_line)
        self.assertFalse(any("#4" in x for x in b.evidence))

    def test_no_long_wait_is_no_breach(self):
        self.assertEqual(invariants.ticket_waiting(Facts(T0)), [])
        short = [Wait(Ref("%s#1" % REPO, "#1"), "no free lane in tier medium", T0 - 60)]
        self.assertEqual(invariants.ticket_waiting(Facts(T0, waits=short)), [])

    def test_unread_facts_break_nothing_and_a_raising_invariant_is_logged(self):
        said = []

        def broken(facts):
            raise KeyError("boom")
        self.assertEqual(invariants.check(Facts(T0), said.append), [])
        self.assertEqual(invariants.check(Facts(T0), said.append, (broken,)), [])
        self.assertIn("broken raised KeyError", said[0])

    def test_a_goal_the_gate_refused_for_59_minutes_is_not_a_breach(self):
        hold = GateHold(Ref("%s#%d" % (REPO, GOAL), "#30"), T0 - 59 * 60,
                        27, 15, (Ref("%s#1" % REPO, "#1"),), 2)
        self.assertEqual(invariants.gate_hold(Facts(T0, gate_held={"k": hold})), [])
        self.assertEqual(invariants.gate_hold(Facts(T0)), [])

    def test_a_goal_the_gate_refused_for_61_minutes_names_goal_count_limit_held_and_idle(self):
        hold = GateHold(Ref("%s#%d" % (REPO, GOAL), "#30"), T0 - 61 * 60,
                        27, 15, (Ref("%s#1" % REPO, "#1"), Ref("%s#2" % REPO, "#2")), 4)
        (b,) = invariants.gate_hold(Facts(T0, gate_held={"k": hold}))
        text = b.title + " " + " ".join(b.evidence)
        self.assertIn("#30", text)
        self.assertIn("27", text)
        self.assertIn("15", text)
        self.assertIn("#1", text)
        self.assertIn("#2", text)
        self.assertIn("4", text)

    def test_a_goal_no_longer_held_clears_the_invariant(self):
        hold = GateHold(Ref("%s#%d" % (REPO, GOAL), "#30"), T0 - 61 * 60,
                        27, 15, (Ref("%s#1" % REPO, "#1"),), 2)
        self.assertEqual(len(invariants.gate_hold(Facts(T0, gate_held={"k": hold}))), 1)
        # A claim for the goal succeeded, so the station no longer holds it.
        self.assertEqual(invariants.gate_hold(Facts(T0, gate_held={})), [])

    def test_a_login_refused_three_times_in_the_hour_is_not_a_breach(self):
        self.assertEqual(invariants.untrusted_labeler(Facts(T0, label_refusals={"x[bot]": 3})), [])
        self.assertEqual(invariants.untrusted_labeler(Facts(T0)), [])

    def test_a_login_refused_four_times_in_the_hour_names_the_login(self):
        (b,) = invariants.untrusted_labeler(Facts(T0, label_refusals={"loom-app[bot]": 4}))
        self.assertIn("loom-app[bot]", b.title)
        self.assertIn("LOOM_BRIEF_AUTHORS", " ".join(b.evidence))

    def pull(self, at, number=534, ticket=481, labels=("loom:ticket", "loom:pr"),
             drives=True, threads=0, fix_pending=False):
        """One open lane pull request as the station reads it."""
        return invariants.LanePull(
            Ref("%s#%d" % (REPO, number), "pull request #%d" % number), at,
            Ref("%s#%d" % (REPO, ticket), "#%d" % ticket) if ticket is not None else None,
            tuple(labels), drives, threads, fix_pending)

    def test_a_lane_pull_open_five_hours_and_unread_pull_requests_find_nothing(self):
        self.assertEqual(invariants.lane_pr_stalled(Facts(T0)), [])
        self.assertEqual(invariants.lane_pr_stalled(
            Facts(T0, lane_pulls=[self.pull(T0 - 5 * 3600, threads=2)])), [])

    def test_each_stall_names_its_own_reason(self):
        # (short reason in the title, full reason in the evidence)
        cases = {
            ("no open loom:ticket carries it", "no open loom:ticket carries it"):
                dict(ticket=None, drives=False, labels=()),
            ("ticket past the model ladder", "past the last rung of the model ladder"):
                dict(labels=("loom:ticket", "loom:failed")),
            ("ticket waits on the brain", "its ticket waits on the brain"):
                dict(labels=("loom:ticket", "loom:pr", "needs-brain")),
            ("1 review thread, no fix round", "1 open review thread and no fix round queued"):
                dict(threads=1),
        }
        for (short, reason), shape in cases.items():
            with self.subTest(reason):
                (b,) = invariants.lane_pr_stalled(
                    Facts(T0, lane_pulls=[self.pull(T0 - 7 * 3600, **shape)]))
                self.assertEqual((b.invariant, b.subject), ("lane-pr-stalled", "%s#534" % REPO))
                self.assertIn(short, b.title)
                self.assertLessEqual(len(b.title), 72)
                self.assertIn(reason, " ".join(b.evidence))
                self.assertIn("Unstick it:", " ".join(b.evidence))

    def test_the_ladder_and_the_open_threads_name_the_one_command_that_unsticks_them(self):
        (ladder,) = invariants.lane_pr_stalled(
            Facts(T0, lane_pulls=[self.pull(T0 - 7 * 3600, labels=("loom:ticket", "loom:failed"))]))
        self.assertIn("bus.brain brief", ladder.evidence[-1])
        (threads,) = invariants.lane_pr_stalled(
            Facts(T0, lane_pulls=[self.pull(T0 - 7 * 3600, threads=2)]))
        self.assertIn("bus.brain fix", threads.evidence[-1])

    def test_a_ticket_the_factory_can_move_drives_the_pull_request_it_carries(self):
        for shape in ({}, dict(threads=2, fix_pending=True),
                      dict(labels=("loom:ticket", "loom:ready"))):
            with self.subTest(shape):
                self.assertEqual(invariants.lane_pr_stalled(
                    Facts(T0, lane_pulls=[self.pull(T0 - 7 * 3600, **shape)])), [])


if __name__ == "__main__":
    unittest.main()


def lane_facts(lanes, now=T0):
    """Facts in which every lane of `lanes` breaks all three per-lane rolling
    invariants at once: held by a limit for three hours while a ticket of its
    tier waits, its last runs failed after a success, and it held a running
    ticket while silent."""
    ticket = Ref("%s#5" % REPO, "#5")
    runs, silent = [], []
    for n, lane in enumerate(lanes):
        runs.append(Run(lane, True, now - 9 * 3600, ""))
        runs += [Run(lane, False, now - i * 600 - n, "[transient] agent exited 1")
                 for i in range(invariants.FAILING_STREAK)]
        silent.append(SilentRun(lane, 31 * 60, (Ref("%s#%d" % (REPO, 10 + n), "#%d" % (10 + n)),),
                                now - 60 - n))
    return Facts(now, held={lane: now - 3 * 3600 for lane in lanes},
                 queued=[(ticket, tuple(lanes))], runs=runs, silent_runs=silent)


ROLLING_LANES = (("lane-held", "lane_held"), ("lane-failing-streak", "lane_failing_streak"),
                 ("silent-lane", "silent_lane"))


class RollingLaneTest(unittest.TestCase):
    """lane-held, lane-failing-streak and silent-lane alarm in bursts with one
    shared cause: one provider limit opened 9 lane-held issues in 6 minutes
    (2026-10-15), and 6 failing-streak issues opened in 27 minutes
    (2026-10-20). Each is one rolling issue, a line per lane."""

    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        self.state = {}

    def file(self, breaches, now=T0):
        invariants.Filer(self.gh, REPO, GOAL, self.state, now, self.said.append).run(breaches)

    def test_three_lanes_are_one_rolling_breach_with_a_line_per_lane(self):
        lanes = ("agy", "zen", "codex")
        for invariant, name in ROLLING_LANES:
            with self.subTest(invariant=invariant):
                (b,) = getattr(invariants, name)(lane_facts(lanes))
                self.assertEqual(b.invariant, invariant)
                self.assertTrue(b.rolling)
                self.assertEqual(b.subject, invariants.LANES_SUBJECT)
                self.assertIn("3 lanes", b.title)
                self.assertLessEqual(len(b.title), 72)
                for lane in lanes:
                    self.assertEqual(len([line for line in b.evidence
                                          if line.startswith(lane + ":")]), 1, b.evidence)
                # The same key whatever the lanes: a new lane joins the one issue.
                (alone,) = getattr(invariants, name)(lane_facts(lanes[:1]))
                self.assertEqual(alone.key, b.key)
                self.assertIn("1 lane ", alone.title)

    def test_a_fourth_lane_rewrites_the_one_issue_and_files_no_second(self):
        for invariant, name in ROLLING_LANES:
            with self.subTest(invariant=invariant):
                self.setUp()
                self.file(getattr(invariants, name)(lane_facts(("agy", "zen", "codex"))))
                (number,) = self.gh.issues
                later = T0 + 600
                (b,) = getattr(invariants, name)(
                    lane_facts(("agy", "zen", "codex", "spark"), now=later))
                self.file([b], now=later)
                self.assertEqual(list(self.gh.issues), [number])
                issue = self.gh.issues[number]
                self.assertIn("4 lanes", issue["title"])
                self.assertIn("- spark:", issue["body"])
                self.assertIn("Last seen by the station: %s" % invariants.utc(b.at),
                              issue["body"])

    def test_an_old_per_lane_issue_folds_into_the_rolling_one_unless_it_asks_the_operator(self):
        old = invariants.Breach("lane-held", "zen", "Lane zen held", ("x",), T0)
        self.file([old])
        (old_number,) = self.gh.issues
        self.gh.issues[old_number].update(labels=[{"name": x} for x in invariants.LABELS])
        asks = invariants.Breach("lane-held", "anthropic-4", "Lane anthropic-4 held", ("x",), T0)
        self.file([asks])
        asks_number = max(self.gh.issues)
        self.gh.issues[asks_number].update(labels=[{"name": x} for x in invariants.LABELS]
                                           + [{"name": "needs:example-operator"}])
        later = T0 + 300
        self.file(invariants.lane_held(lane_facts(("zen", "agy"), now=later)), now=later)
        rolling = self.state[invariants.Breach("lane-held", invariants.LANES_SUBJECT, "t", (),
                                               T0).key]
        self.assertEqual(self.gh.issues[old_number]["state"], "closed")
        (note,) = self.gh.comments[(REPO, old_number)]
        self.assertIn("Folded into #%d" % rolling, note["body"])
        self.assertEqual(self.gh.issues[rolling]["state"], "open")
        # An operator ask is not closed out from under the operator.
        self.assertEqual(self.gh.issues[asks_number]["state"], "open")
        self.assertNotIn((REPO, asks_number), self.gh.comments)

    def test_a_rewritten_issue_is_folded_into_the_rolling_one_unless_it_asks_the_operator(self):
        old = invariants.Breach("lane-held", "zen", "Lane zen held", ("x",), T0)
        self.file([old])
        (old_number,) = self.gh.issues
        rewritten = [{"name": invariants.LABEL}, {"name": "loom:rewritten"}]
        self.gh.issues[old_number].update(labels=rewritten)
        asks = invariants.Breach("lane-held", "anthropic-4", "Lane anthropic-4 held", ("x",), T0)
        self.file([asks])
        asks_number = max(self.gh.issues)
        self.gh.issues[asks_number].update(labels=rewritten + [{"name": "needs:example-operator"}])
        later = T0 + 300
        self.file(invariants.lane_held(lane_facts(("zen", "agy"), now=later)), now=later)
        rolling = self.state[invariants.Breach("lane-held", invariants.LANES_SUBJECT, "t", (),
                                               T0).key]
        self.assertEqual(self.gh.issues[old_number]["state"], "closed")
        (note,) = self.gh.comments[(REPO, old_number)]
        self.assertIn("Folded into #%d" % rolling, note["body"])
        self.assertEqual(self.gh.issues[asks_number]["state"], "open")

    def test_lane_no_success_stays_one_breach_per_lane(self):
        runs = [Run(lane, False, T0 - i * 60, "x") for lane in ("zen", "zai") for i in range(5)]
        found = invariants.lane_no_success(Facts(T0, runs=runs))
        self.assertEqual(sorted(b.key for b in found), sorted(
            "lane-no-success/%s" % hashlib.sha256(lane.encode()).hexdigest()[:12]
            for lane in ("zen", "zai")))
        self.assertFalse(any(b.rolling for b in found))


class StandingLaneHeldTest(unittest.TestCase):
    """loom#845: the lane-held issue is a standing one. A hold that clears
    updates it in place to say so, and the next hold edits it again; it is
    never closed and reopened (every reopen sent a notification email)."""

    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        self.state = {}

    def file(self, breaches, now=T0):
        invariants.Filer(self.gh, REPO, GOAL, self.state, now, self.said.append).run(breaches)

    def held(self, lanes, now):
        return invariants.lane_held(lane_facts(lanes, now=now))

    def test_a_second_hold_edits_the_standing_issue_instead_of_reopening_a_closed_one(self):
        self.file(self.held(("zen",), T0), now=T0)
        (number,) = self.gh.issues
        cleared = T0 + 7 * 3600
        self.file([], now=cleared)
        again = T0 + 2 * DAY_S
        writes = len(self.gh.writes)
        self.file(self.held(("zen",), again), now=again)
        self.assertEqual(list(self.gh.issues), [number])
        issue = self.gh.issues[number]
        self.assertEqual(issue["state"], "open")
        self.assertIn("1 lane held by a limit", issue["title"])
        self.assertIn("- zen: held by a limit since %s" % invariants.utc(again - 3 * 3600),
                      issue["body"])
        # An edit, not a reopen: no state change and no comment (a comment mails people).
        for method, _path, body in self.gh.writes[writes:]:
            self.assertEqual(method, "PATCH")
            self.assertNotIn("state", body)
        self.assertNotIn((REPO, number), self.gh.comments)

    def test_a_cleared_hold_is_marked_clear_in_place_and_not_closed(self):
        self.file(self.held(("zen",), T0), now=T0)
        (number,) = self.gh.issues
        self.gh.issues[number].update(labels=[{"name": x} for x in invariants.LABELS])
        cleared = T0 + 7 * 3600
        self.file([], now=cleared)
        issue = self.gh.issues[number]
        self.assertEqual(issue["state"], "open")
        self.assertIn("No lane is held by a limit now", issue["title"])
        self.assertIn("Clear since %s." % invariants.utc(cleared), issue["body"])
        self.assertRegex(issue["body"], invariants.MARKER_RE)
        self.assertNotIn((REPO, number), self.gh.comments)
        # The hold goes to the log in the body, not to a new issue.
        self.assertIn("- zen: held since %s, cleared %s"
                      % (invariants.utc(T0 - 3 * 3600), invariants.utc(cleared)), issue["body"])
        # Another clear pass writes nothing.
        writes = len(self.gh.writes)
        self.file([], now=cleared + 600)
        self.assertEqual(len(self.gh.writes), writes)

    def test_the_hold_log_survives_the_next_hold(self):
        self.file(self.held(("zen",), T0), now=T0)
        (number,) = self.gh.issues
        self.gh.issues[number].update(labels=[{"name": x} for x in invariants.LABELS])
        cleared = T0 + 7 * 3600
        self.file([], now=cleared)
        again = T0 + 2 * DAY_S
        self.file(self.held(("agy",), again), now=again)
        body = self.gh.issues[number]["body"]
        self.assertIn("- agy: held by a limit since", body)
        self.assertIn("- zen: held since %s, cleared %s"
                      % (invariants.utc(T0 - 3 * 3600), invariants.utc(cleared)), body)
        # A lane that drops out while another stays held is logged too.
        later = again + 3600
        self.file(self.held(("agy", "codex"), later), now=later)
        self.file(self.held(("codex",), later + 600), now=later + 600)
        body = self.gh.issues[number]["body"]
        self.assertIn("- agy: held since", body)
        self.assertEqual(self.gh.issues[number]["state"], "open")

    def test_an_old_per_lane_issue_is_still_closed_when_cleared(self):
        old = invariants.Breach("lane-held", "zen", "Lane zen held", ("x",), T0)
        self.file([old], now=T0)
        (number,) = self.gh.issues
        self.gh.issues[number].update(labels=[{"name": x} for x in invariants.LABELS])
        self.file([], now=T0 + 7 * 3600)
        self.assertEqual(self.gh.issues[number]["state"], "closed")


class StandingEpicTest(unittest.TestCase):
    def test_the_repairs_epic_is_exempt_from_both_caps(self):
        goal = Ref("%s#%d" % (REPO, GOAL), "#30")
        epics = tuple(invariants.EpicShape(Ref("%s#%d" % (REPO, n), "#%d" % n), True, 1, ())
                      for n in range(40, 46))
        repairs = invariants.EpicShape(Ref("%s#50" % REPO, "#50"), True, 40, (), True)
        f = Facts(T0, shapes=[invariants.GoalShape(goal, 6, epics + (repairs,), ())])
        self.assertEqual(invariants.epic_over_cap(f), [])
        self.assertEqual(invariants.goal_epics_over_cap(f), [])


class RollPinOffTest(StationBase):
    """A goal home whose ship rules name a roll, with no roll ship document
    naming it: its merged module changes get no source-pin pull request, and
    the station said nothing about it (loom#528)."""

    def roll_goal(self, repo=REPO, number=GOAL):
        self.gh.goal_issues[repo] = [{
            "number": number, "title": "A server goal", "labels": [{"name": "loom:goal"}],
            "body": "Ship rules: at most one server roll per hour."}]

    def test_the_invariant_names_each_roll_goal_in_one_rolling_breach(self):
        goals = [Ref("%s#%d" % (REPO, GOAL), "#%d" % GOAL),
                 Ref("%s#3" % OTHER, "issue 3 of a private repo")]
        self.assertEqual(invariants.roll_pin_off(Facts(T0)), [])  # not read
        self.assertEqual(invariants.roll_pin_off(Facts(T0, roll_goals=[])), [])
        (b,) = invariants.roll_pin_off(Facts(T0, roll_goals=goals))
        self.assertTrue(b.rolling)
        self.assertEqual(b.subject, invariants.ROLL_SUBJECT)
        self.assertIn("2 goal homes", b.title)
        self.assertTrue(any("LOOM_ROLL_SHIP_DOCS is empty" in line for line in b.evidence))
        self.assertTrue(any("issue 3 of a private repo" in line for line in b.evidence))
        (named,) = invariants.roll_pin_off(Facts(T0, roll_goals=goals[:1], roll_docs=1))
        self.assertEqual(named.key, b.key)
        self.assertFalse(any("is empty" in line for line in named.evidence))

    def test_a_roll_goal_with_the_pass_configured_off_files_a_finding(self):
        self.roll_goal()
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("roll-pin-off", filed["body"])
        self.assertIn("#%d" % GOAL, filed["body"])
        self.assertIn("LOOM_ROLL_SHIP_DOCS", filed["body"])

    def test_a_roll_goal_its_ship_document_names_files_nothing(self):
        self.roll_goal()
        self.settings.roll_ship_docs = [{"goal": "%s#%d" % (REPO, GOAL), "repo": REPO,
                                         "path": "docs/ship.md"}]
        self.run_at(T0)
        self.assertEqual(self.breaches(), [])

    def test_a_goal_that_ships_without_a_roll_files_nothing(self):
        self.gh.goal_issues[REPO][0]["body"] = "Ship rules: rolling deploys, with rollback."
        self.run_at(T0)
        self.assertEqual(self.breaches(), [])
