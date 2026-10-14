from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import invariants, metrics, station  # noqa: E402
from bus.github import GitHubError, Response  # noqa: E402
from bus.invariants import Check, Facts, Journal, Posted, Ref, Run  # noqa: E402

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
        if method == "POST" and path.endswith("/sub_issues"):
            return Response(201, {})
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)", path)
        if m and method == "GET":
            issue = self.issues.get(int(m.group(1)))
            if issue is None:
                raise GitHubError("Not Found", 404)
            return Response(200, issue)
        if m and method == "PATCH":
            self.issues[int(m.group(1))].update(body)
            return Response(200, {})
        if method == "POST" and path.endswith("/comments"):
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


class AcceptanceTest(StationBase):
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
        self.gh.goal_issues[OTHER] = [{"number": 3, "title": "Hidden", "labels": [],
                                       "sub_issues_summary": {"total": 95}}]
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

    def test_breaches_wait_while_the_github_budget_is_low(self):
        self.gh.goal_issues[REPO][0]["sub_issues_summary"]["total"] = 100
        self.gh.rate = {"core": (10, 5000, int(T0) + 3600)}
        self.run_at(T0)
        self.assertEqual(self.gh.filed(), [])
        self.gh.rate = {}
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(len(self.gh.filed()), 1)

    def test_the_station_standup_on_the_invariant_goal_lists_open_breaches(self):
        self.gh.goal_issues[REPO][0]["sub_issues_summary"]["total"] = 100
        st = self.run_at(T0)
        number = self.gh.filed()[0]["number"]
        body = st._overdue_standup_body([], None, st._open_breaches(REPO, GOAL))
        self.assertIn("Invariants broken: #%d Goal home #30 has 100 sub-issues." % number, body)
        self.assertNotIn("Invariants broken", st._overdue_standup_body([], None))


class FilerTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        self.state = {}

    def file(self, breach, now=T0, write=True):
        invariants.Filer(self.gh, REPO, GOAL, self.state, now, self.said.append, write).run([breach])

    def breach(self, at=T0, evidence=("one line",)):
        return invariants.Breach("goal-full", "x/y#1", "Goal home #1 has 95 sub-issues",
                                 tuple(evidence), at)

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


class InvariantTest(unittest.TestCase):
    goal = Ref("%s#%d" % (REPO, GOAL), "#%d" % GOAL)

    def j(self, at, holder="muse", steps=("status failed",)):
        return Journal(at, self.goal, holder, stamp(at), steps)

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

    def test_a_goal_home_at_ninety_sub_issues(self):
        self.assertEqual(invariants.goal_full(Facts(T0, goals=[(self.goal, 89)])), [])
        self.assertEqual(len(invariants.goal_full(Facts(T0, goals=[(self.goal, 90)]))), 1)

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


if __name__ == "__main__":
    unittest.main()
