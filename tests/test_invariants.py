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
from bus.invariants import Check, Event, Facts, GateHold, Journal, Posted, Ref, Run  # noqa: E402

REPO = "example-owner/factory"
OTHER = "example-owner/hidden"
GOAL = 30
AUTHOR = "trusted-example"
T0 = 1_800_000_000.0


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
            return []
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
        if method == "POST" and path.endswith("/comments"):
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", path)
            if m:
                self.comments.setdefault((m.group(1), int(m.group(2))), []).append(
                    {"body": body["body"], "created_at": stamp(T0 + 10),
                     "user": {"login": AUTHOR}})
            return Response(201, {"id": 1})
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
        self.assertIn("Lane pull request #657 open 7.0 hours", filed["title"])
        self.assertIn("past the last rung of the model ladder", filed["title"])
        self.assertIn("#506", filed["body"])
        self.assertNotIn("#658", filed["title"] + filed["body"])

    def test_a_lane_pull_with_open_review_threads_and_no_fix_round_files_a_breach(self):
        self.lane_ticket(602)
        self.lane_pull(660, 602, T0 - 7 * 3600)
        self.bot_threads(2)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("2 open review threads and no fix round queued", filed["title"])
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
                                    "has 1 open tickets under no epic"),
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
        self.assertIn("Invariants broken: #%d Goal home #30 has 1 open tickets under no epic."
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

    def test_quiet_triage_issue_closes_once_and_rewritten_issue_is_untouched(self):
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

        issue.update(state="open", labels=[{"name": invariants.LABEL}, {"name": "loom:rewritten"}])
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0 + 2 * invariants.CHECK_EVERY,
                         self.said.append).run([])
        self.assertEqual(issue["state"], "open")


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

    def test_a_lane_held_over_two_hours_while_its_tier_waits(self):
        ticket = Ref("%s#5" % REPO, "#5")
        f = Facts(T0, held={"agy": T0 - 3 * 3600, "zen": T0 - 600},
                  queued=[(ticket, ("agy", "zen"))])
        (b,) = invariants.lane_held(f)
        self.assertEqual(b.subject, "agy")
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
        self.assertEqual([b.subject for b in invariants.ticket_outside_epic(f)], [self.goal.key])
        self.assertEqual([b.subject for b in invariants.epic_over_cap(f)], [e40.key])
        self.assertEqual([b.subject for b in invariants.goal_epics_over_cap(f)], [self.goal.key])
        self.assertEqual([b.subject for b in invariants.closed_epic_open_tickets(f)],
                         ["%s#42" % REPO])

    def test_a_plan_check_that_passed_or_failed_recently_is_not_a_breach(self):
        pr = Ref("%s#7" % REPO, "pull request #7")
        checks = [Check(pr, "Chief", "success", T0 - 3600, ""), Check(pr, "Chief", "failure", T0 - 60, "")]
        self.assertEqual(invariants.plan_check_failed(Facts(T0, checks=checks)), [])

    def test_a_remote_lane_past_its_beat_max(self):
        limit = invariants.REMOTE_LANES["muse"]["beat_max"]
        self.assertEqual(invariants.remote_silent(Facts(T0, beats={"muse": None})), [])
        self.assertEqual(invariants.remote_silent(Facts(T0, beats={"muse": T0 - limit + 60})), [])
        self.assertEqual(len(invariants.remote_silent(Facts(T0, beats={"muse": T0 - limit - 60}))), 1)

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
        cases = {
            "no open loom:ticket carries it": dict(ticket=None, drives=False, labels=()),
            "past the last rung of the model ladder": dict(labels=("loom:ticket", "loom:failed")),
            "its ticket waits on the brain": dict(labels=("loom:ticket", "loom:pr", "needs-brain")),
            "1 open review thread and no fix round queued": dict(threads=1),
        }
        for reason, shape in cases.items():
            with self.subTest(reason):
                (b,) = invariants.lane_pr_stalled(
                    Facts(T0, lane_pulls=[self.pull(T0 - 7 * 3600, **shape)]))
                self.assertEqual((b.invariant, b.subject), ("lane-pr-stalled", "%s#534" % REPO))
                self.assertIn(reason, b.title)
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


class StandingEpicTest(unittest.TestCase):
    def test_the_repairs_epic_is_exempt_from_both_caps(self):
        goal = Ref("%s#%d" % (REPO, GOAL), "#30")
        epics = tuple(invariants.EpicShape(Ref("%s#%d" % (REPO, n), "#%d" % n), True, 1, ())
                      for n in range(40, 46))
        repairs = invariants.EpicShape(Ref("%s#50" % REPO, "#50"), True, 40, (), True)
        f = Facts(T0, shapes=[invariants.GoalShape(goal, 6, epics + (repairs,), ())])
        self.assertEqual(invariants.epic_over_cap(f), [])
        self.assertEqual(invariants.goal_epics_over_cap(f), [])
