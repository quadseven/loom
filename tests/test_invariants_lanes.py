from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, invariants, metrics, station  # noqa: E402
from bus.github import GitHubError, Response  # noqa: E402
from bus.invariants import Check, Event, Facts, GateHold, Journal, Posted, Ref, Run, SilentRun, Wait  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401 - shadow off: these tests close

REPO = "example-owner/factory"
OTHER = "example-owner/hidden"
GOAL = 30
AUTHOR = "trusted-example"
T0 = 1_800_000_000.0
DAY_S = 24 * 3600
from invariants_support import *  # noqa: E402,F401,F403


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


class RolledKindTest(unittest.TestCase):
    """loom#2099: the station opened 185 invariant tickets in 7 days
    (lane-pr-stalled 109, epic-over-cap 36, closed-epic-open-tickets 15,
    plan-check-failed 13), each restating state the pull request, the epic or
    the dashboard already shows. Each ROLLED kind is one tracking issue per
    repo, its list edited in place."""

    PUBLIC = "example-owner/open-thing"

    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        self.state = {}
        self.seen = {}
        patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": AUTHOR})
        patcher.start()
        self.addCleanup(patcher.stop)

    def file(self, breaches, now=T0):
        invariants.Filer(self.gh, REPO, GOAL, self.state, now, self.said.append,
                         seen=self.seen).run(breaches)

    def pull(self, number, repo=REPO, opened=T0 - 7 * 3600, threads=1):
        label = ("pull request #%d" % number if repo == REPO else
                 "pull request %d of %s" % (number, "a private repo" if repo == OTHER else repo))
        return invariants.LanePull(Ref("%s#%d" % (repo, number), label), opened,
                                   Ref("%s#%d" % (repo, number - 100), "#%d" % (number - 100)),
                                   ("loom:ticket", "loom:pr"), True, threads, False)

    def stalled(self, pulls, now=T0):
        return invariants.lane_pr_stalled(Facts(now, lane_pulls=pulls))

    def rolled(self):
        return {n: i for n, i in self.gh.issues.items()
                if invariants.ROLL_MARKER in str(i.get("body", ""))}

    def test_three_stalled_pulls_in_one_repo_are_one_issue_with_three_lines(self):
        self.file(self.stalled([self.pull(201), self.pull(202), self.pull(203)]))
        (issue,) = self.gh.issues.values()
        self.assertEqual(issue["title"], "Invariant: lane-pr-stalled in %s" % REPO)
        self.assertEqual(issue["labels"], [invariants.LABEL])
        lines = [line for line in issue["body"].split("\n") if line.startswith("- Lane ")]
        self.assertEqual(len(lines), 3, issue["body"])
        for number in (201, 202, 203):
            self.assertIn("- Lane pull request #%d: 1 review thread, no fix round; ticket: #%d "
                          "(since %s)" % (number, number - 100, stamp(T0 - 7 * 3600)),
                          issue["body"])
        self.assertIn("Current violations (3):", issue["body"])
        self.assertIn("Last checked: %s" % stamp(T0), issue["body"])
        self.assertRegex(issue["body"], invariants.MARKER_RE)

    def test_an_unchanged_list_writes_nothing_a_pass_later(self):
        self.file(self.stalled([self.pull(201), self.pull(202)]))
        writes = len(self.gh.writes)
        later = T0 + 3600
        self.file(self.stalled([self.pull(201), self.pull(202)], now=later), now=later)
        self.assertEqual(len(self.gh.writes), writes, self.gh.writes[writes:])
        self.assertEqual(len(self.gh.issues), 1)

    def test_a_merged_pull_drops_its_line_by_an_edit_with_no_comment_and_no_issue(self):
        self.file(self.stalled([self.pull(201), self.pull(202)]))
        (number,) = self.gh.issues
        later = T0 + 600
        self.file(self.stalled([self.pull(202)], now=later), now=later)
        self.assertEqual(list(self.gh.issues), [number])
        body = self.gh.issues[number]["body"]
        self.assertNotIn("#201", body)
        self.assertIn("#202", body)
        self.assertIn("Current violations (1):", body)
        self.assertEqual(self.gh.comments, {})
        self.assertEqual(self.gh.issues[number]["state"], "open")

    def test_a_cleared_kind_says_none_then_closes_after_the_clear_wait_and_reopens(self):
        self.clear_and_return()

    def clear_and_return(self):
        """Clear, wait, close, return: the tracking issue's number and when."""
        self.file(self.stalled([self.pull(201)]))
        (number,) = self.gh.issues
        issue = self.gh.issues[number]
        self.file([], now=T0 + 300)
        self.assertIn(invariants.ROLL_NONE, issue["body"])
        self.assertIn("Clear since %s." % stamp(T0 + 300), issue["body"])
        self.assertNotIn("#201", issue["body"])
        self.assertEqual(self.gh.comments, {})
        # Empty for passes, but short of CLEAR_AFTER: open, nothing written.
        writes = len(self.gh.writes)
        for at in (T0 + 600, T0 + 300 + invariants.CLEAR_AFTER - 60):
            self.file([], now=at)
        self.assertEqual(issue["state"], "open")
        self.assertEqual(len(self.gh.writes), writes)
        # Clear for CLEAR_AFTER: it closes with one line.
        closed_at = T0 + 300 + invariants.CLEAR_AFTER
        self.file([], now=closed_at)
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue["state_reason"], "not_planned")
        (note,) = self.gh.comments[(REPO, number)]
        self.assertIn(invariants.CLEARED_TEXT + stamp(closed_at), note["body"])
        # Still clear: nothing more is written.
        writes = len(self.gh.writes)
        self.file([], now=closed_at + 300)
        self.assertEqual(len(self.gh.writes), writes)
        # A violation returns: the same issue reopens with the new list.
        issue["closed_at"] = stamp(closed_at)
        back = closed_at + 600
        self.file(self.stalled([self.pull(205)], now=back), now=back)
        self.assertEqual(list(self.gh.issues), [number])
        self.assertEqual(issue["state"], "open")
        self.assertIn("#205", issue["body"])
        self.assertNotIn(invariants.ROLL_NONE, issue["body"])
        return number, back

    def test_a_second_clear_edits_the_one_cleared_note_and_it_reopens_again(self):
        number, back = self.clear_and_return()
        issue = self.gh.issues[number]
        self.file([], now=back + 300)
        again = back + 300 + invariants.CLEAR_AFTER
        self.file([], now=again)
        self.assertEqual(issue["state"], "closed")
        notes = [c["body"] for c in self.gh.comments[(REPO, number)]
                 if invariants.CLEARED_TEXT in c["body"]]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn(invariants.CLEARED_TEXT + stamp(again), notes[0])
        # The edited note is newer than the reopen note: a third return
        # reopens the issue again.
        issue["closed_at"] = stamp(again)
        self.file(self.stalled([self.pull(206)], now=again + 600), now=again + 600)
        self.assertEqual(issue["state"], "open", self.said)
        self.assertIn("#206", issue["body"])

    def test_a_pass_that_could_not_read_the_facts_leaves_the_list_alone(self):
        self.file(self.stalled([self.pull(201)]))
        (issue,) = self.gh.issues.values()
        body, writes = issue["body"], len(self.gh.writes)
        facts = Facts(T0 + 300)  # lane_pulls unread: None
        self.assertEqual(invariants.read_kinds(facts), ())
        invariants.Filer(self.gh, REPO, GOAL, self.state, T0 + 300, self.said.append,
                         seen=self.seen, read=invariants.read_kinds(facts)).run([])
        self.assertEqual(issue["body"], body)
        self.assertEqual(len(self.gh.writes), writes)

    def test_other_kinds_and_repos_stay_separate(self):
        epics = invariants.epic_over_cap(Facts(T0, shapes=[invariants.GoalShape(
            Ref("%s#%d" % (REPO, GOAL), "#%d" % GOAL), 6,
            (invariants.EpicShape(Ref("%s#40" % REPO, "#40"), True, 16, ()),), ())]))
        lane = invariants.Breach("lane-no-success", "zen", "Lane zen: 5 runs in 24 hours and "
                                 "none done", ("x",), T0)
        self.file(self.stalled([self.pull(201), self.pull(202), self.pull(301, self.PUBLIC),
                                self.pull(401, OTHER)]) + epics + [lane])
        titles = sorted(i["title"] for i in self.gh.issues.values())
        private = "a private repo (%s)" % hashlib.sha256(OTHER.encode()).hexdigest()[:6]
        self.assertEqual(titles, sorted([
            "Invariant: lane-pr-stalled in %s" % REPO,
            "Invariant: lane-pr-stalled in %s" % self.PUBLIC,
            "Invariant: lane-pr-stalled in %s" % private,
            "Invariant: epic-over-cap in %s" % REPO,
            "Lane zen: 5 runs in 24 hours and none done"]))
        by_title = {i["title"]: i for i in self.gh.issues.values()}
        self.assertIn("pull request 301 of %s" % self.PUBLIC,
                      by_title["Invariant: lane-pr-stalled in %s" % self.PUBLIC]["body"])
        self.assertNotIn("301", by_title["Invariant: lane-pr-stalled in %s" % REPO]["body"])
        self.assertNotIn("hidden", "".join(i["title"] + i["body"]
                                           for i in self.gh.issues.values()))
        # The kind not ROLLED keeps its own issue, with the triage labels.
        self.assertEqual(by_title["Lane zen: 5 runs in 24 hours and none done"]["labels"],
                         invariants.LABELS)

    def test_an_epic_line_keeps_when_it_was_first_seen(self):
        def shape(tickets, now):
            return invariants.epic_over_cap(Facts(now, shapes=[invariants.GoalShape(
                Ref("%s#%d" % (REPO, GOAL), "#%d" % GOAL), 6,
                (invariants.EpicShape(Ref("%s#40" % REPO, "#40"), True, tickets, ()),), ())]))
        self.file(shape(16, T0))
        writes = len(self.gh.writes)
        self.file(shape(16, T0 + 3600), now=T0 + 3600)
        self.assertEqual(len(self.gh.writes), writes)
        (issue,) = self.gh.issues.values()
        self.assertIn("16 tickets (cap 15) (since %s)" % stamp(T0), issue["body"])
        # Gone and back: it starts again.
        self.file([], now=T0 + 4000)
        self.file(shape(17, T0 + 5000), now=T0 + 5000)
        self.assertIn("17 tickets (cap 15) (since %s)" % stamp(T0 + 5000), issue["body"])

    def test_old_per_violation_issues_are_left_as_they_are(self):
        """While its kind is broken, an older per-violation issue of a ROLLED
        kind is never closed as cleared: its breach now lives on the tracking
        issue, so its own key going quiet says nothing."""
        key = invariants.Breach("lane-pr-stalled", "%s#51" % REPO, "t", (), T0).key
        old = self.gh.issues[51] = {
            "number": 51, "id": 9051, "state": "open", "closed_at": None,
            "title": "Lane pull request #51 open 7.0 hours",
            "body": "%s\nOld.\n\nPart of #%d" % (invariants.MARKER % key, GOAL),
            "labels": [{"name": n} for n in invariants.LABELS]}
        self.file(self.stalled([self.pull(201)]))
        self.file(self.stalled([self.pull(201)], now=T0 + 2 * invariants.CLEAR_AFTER),
                  now=T0 + 2 * invariants.CLEAR_AFTER)
        self.assertEqual(old["state"], "open", self.said)
        self.assertNotIn((REPO, 51), self.gh.comments)


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


class EpicTitleSharedTest(unittest.TestCase):
    """loom#2030: open epics of one repo that share a title are a breach;
    closed ones, other repos and distinct titles are not."""

    def shape(self, goal, *epics):
        return invariants.GoalShape(goal, 6, tuple(epics), ())

    def test_two_open_epics_of_one_repo_with_one_title_are_reported(self):
        goal, other = Ref("%s#%d" % (REPO, GOAL), "#30"), Ref("%s#9" % REPO, "#9")
        same = "Epic: work that triage, rewrites and health intake find"
        e = lambda n, title, open_=True, repo=REPO: invariants.EpicShape(  # noqa: E731
            Ref("%s#%d" % (repo, n), "#%d" % n), open_, 1, (), False, title=title)
        f = Facts(T0, shapes=[
            self.shape(goal, e(45, same), e(41, "Epic: other work"), e(39, same, False)),
            self.shape(other, e(43, same), e(44, same, repo="example-owner/elsewhere"))])
        (b,) = invariants.epic_title_shared(f)
        self.assertEqual(b.subject, "%s#43" % REPO)
        self.assertIn("2 open epics share the title of epic #43", b.title)
        self.assertIn("#43, #45", b.evidence[0])
        self.assertNotIn(same, " ".join((b.title,) + b.evidence))  # labels only
        self.assertIn(invariants.epic_title_shared, invariants.INVARIANTS)

    def test_distinct_titles_and_unread_shapes_are_healthy(self):
        goal = Ref("%s#%d" % (REPO, GOAL), "#30")
        epics = [invariants.EpicShape(Ref("%s#%d" % (REPO, n), "#%d" % n), True, 1, (), False,
                                      title="Epic: work, batch %d" % n) for n in (40, 41)]
        self.assertEqual(invariants.epic_title_shared(Facts(T0, shapes=[self.shape(goal, *epics)])), [])
        self.assertEqual(invariants.epic_title_shared(Facts(T0)), [])


class StandingEpicTest(unittest.TestCase):
    def test_the_repairs_epic_is_exempt_from_both_caps(self):
        goal = Ref("%s#%d" % (REPO, GOAL), "#30")
        epics = tuple(invariants.EpicShape(Ref("%s#%d" % (REPO, n), "#%d" % n), True, 1, ())
                      for n in range(40, 46))
        repairs = invariants.EpicShape(Ref("%s#50" % REPO, "#50"), True, 40, (), True)
        f = Facts(T0, shapes=[invariants.GoalShape(goal, 6, epics + (repairs,), ())])
        self.assertEqual(invariants.epic_over_cap(f), [])
        self.assertEqual(invariants.goal_epics_over_cap(f), [])

    def test_any_open_epic_at_the_sub_issue_limit_is_flagged_standing_or_not(self):
        goal = Ref("%s#%d" % (REPO, GOAL), "#30")
        repairs = invariants.EpicShape(Ref("%s#50" % REPO, "#50"), True, 100, (), True, 100)
        batch = invariants.EpicShape(Ref("%s#51" % REPO, "#51"), True, 100, (), False, 100)
        done = invariants.EpicShape(Ref("%s#52" % REPO, "#52"), False, 15, (), True, 100)
        f = Facts(T0, shapes=[invariants.GoalShape(goal, 6, (repairs, batch, done), ())])
        found = invariants.epic_over_cap(f)
        self.assertEqual([b.subject for b in found], ["%s#50" % REPO, "%s#51" % REPO])
        self.assertIn("100 sub-issues", found[0].title)
