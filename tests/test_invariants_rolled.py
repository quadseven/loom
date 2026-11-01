from __future__ import annotations

import hashlib
import os
import re
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import invariants, station  # noqa: E402
from bus.invariants import Facts, Ref  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401 - shadow off: these tests close
from bus.github import GitHubError  # noqa: E402
from test_invariants import (  # noqa: E402
    AUTHOR, GOAL, OTHER, REPO, T0, FakeGH, StationBase, epic, stamp)


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

    def pull(self, number, repo=REPO, opened=T0 - 25 * 3600, threads=1):
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
                          "(since %s)" % (number, number - 100, stamp(T0 - 25 * 3600)),
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

    def legacy(self, number, invariant="lane-pr-stalled", labels=None, subject=None):
        key = invariants.Breach(invariant, subject or "%s#%d" % (REPO, number), "t", (), T0).key
        self.gh.issues[number] = {
            "number": number, "id": 9000 + number, "state": "open", "closed_at": None,
            "title": "Lane pull request #%d open 25.0 hours" % number,
            "body": "%s\nOld.\n\nPart of #%d" % (invariants.MARKER % key, GOAL),
            "labels": [{"name": n} for n in (labels or invariants.LABELS)]}
        return self.gh.issues[number]

    def test_an_old_issue_folds_only_into_its_own_repos_tracking_issue(self):
        # With no kept subject, the old issue's text is all migration has to
        # tell the repo by: a private repo's names no reference that hashes
        # back to its key.
        private = self.legacy(61, subject="%s#2440" % OTHER)
        private["title"] = "Lane pull request 2440 of a private repo open 32.9 hours: x"
        private["created_at"] = stamp(T0 - 7 * 3600)
        public = self.legacy(62, subject="%s#1527" % self.PUBLIC)
        public["title"] = "Lane pull request 1527 of %s open 13.3 hours: x" % self.PUBLIC
        # Only the factory repo has a stalled pull request this pass.
        self.file(self.stalled([self.pull(201)]))
        (rolled,) = self.rolled()
        # The private one closes naming no tracking issue, not the factory's.
        self.assertEqual(private["state"], "closed")
        self.assertIn("no longer files one issue per", private["body"])
        self.assertNotIn("#%d lists" % rolled, private["body"])
        # The public repo has no tracking issue open: it waits out the clear.
        self.assertEqual(public["state"], "open")
        # Its repo breaks too: it closes into that repo's issue, not the other.
        later = T0 + 300
        self.file(self.stalled([self.pull(201),
                                self.pull(301, self.PUBLIC)], now=later), now=later)
        mine = [n for n, i in self.rolled().items() if i["title"].endswith(self.PUBLIC)]
        self.assertEqual(len(mine), 1)
        self.assertEqual(public["state"], "closed")
        self.assertIn("#%d lists this kind" % mine[0], public["body"])

    def test_migration_folds_a_few_a_pass_silently_with_one_edited_summary(self):
        old = [self.legacy(n) for n in (51, 52, 53)]
        ticketed = self.legacy(54, labels=(invariants.LABEL, "loom:ticket", "loom:briefed"))
        asks = self.legacy(56, labels=invariants.LABELS + ["needs:someone"])
        other = self.legacy(55, invariant="lane-no-success", subject="zen")
        self.file(self.stalled([self.pull(201)]))
        (rolled,) = self.rolled()
        closed = [i for i in old if i["state"] == "closed"]
        self.assertEqual(len(closed), invariants.FOLD_MAX, self.said)
        for issue in closed:
            self.assertEqual(issue["state_reason"], "not_planned")
            self.assertIn("#%d lists this kind" % rolled, issue["body"])
        self.assertEqual(sum(": folded " in line for line in self.said), invariants.FOLD_MAX)
        (note,) = self.gh.comments[(REPO, rolled)]
        self.assertIn("2 older per-violation issues", note["body"])
        self.file(self.stalled([self.pull(201)], now=T0 + 300), now=T0 + 300)
        self.assertEqual([i["state"] for i in old], ["closed"] * 3)
        # One comment in all, on the tracking issue, edited as the count grew.
        self.assertEqual([k for k in self.gh.comments], [(REPO, rolled)])
        (note,) = self.gh.comments[(REPO, rolled)]
        self.assertIn("3 older per-violation issues", note["body"])
        # A ticket keeps its factory link, an ask its operator, and another
        # kind its own clear rule.
        self.assertEqual(ticketed["state"], "open")
        self.assertEqual(asks["state"], "open")
        self.assertEqual(other["state"], "open")
        self.assertEqual(len(self.gh.issues), 7)

    def test_epic_title_shared_is_one_tracking_issue_with_a_line(self):
        shape = invariants.GoalShape(Ref("%s#%d" % (REPO, GOAL), "#%d" % GOAL), 6, (
            invariants.EpicShape(Ref("%s#40" % REPO, "#40"), True, 3, (), title="Intake"),
            invariants.EpicShape(Ref("%s#41" % REPO, "#41"), True, 3, (), title="Intake")), ())
        self.file(invariants.epic_title_shared(Facts(T0, shapes=[shape])))
        (issue,) = self.gh.issues.values()
        self.assertEqual(issue["title"], "Invariant: epic-title-shared in %s" % REPO)
        self.assertIn("- Open epics with one title: #40, #41 (since %s)" % stamp(T0),
                      issue["body"])

    def test_a_pull_request_stalls_past_a_day_not_past_six_hours(self):
        self.assertEqual(invariants.PR_STALLED, 24 * 3600)
        self.assertEqual(self.stalled([self.pull(201, opened=T0 - 23 * 3600)]), [])
        (b,) = self.stalled([self.pull(201, opened=T0 - 25 * 3600)])
        self.assertEqual(b.subject, "%s#201" % REPO)


class PullErrorsGH(FakeGH):
    """FakeGH whose open pull request listing fails for the repos in errors."""

    def __init__(self):
        super().__init__()
        self.errors = set()

    def rest(self, method, path, body=None):
        for repo in self.errors:
            if path.startswith("/repos/%s/pulls?" % repo):
                raise GitHubError("Bad Gateway", 502)
        return super().rest(method, path, body)


class UnreadRepoTest(StationBase):
    def setUp(self):
        super().setUp()
        self.gh = PullErrorsGH()

    def notes(self, filed):
        return [c["body"] for c in self.gh.comments.get((REPO, filed["number"]), [])]

    def test_a_repo_whose_pull_requests_cannot_be_read_keeps_its_tracking_issue(self):
        """One repo's open pull requests answer an error: its tracking issue
        is not emptied or closed, and the other repo's still updates."""
        self.lane_ticket(602)
        self.lane_pull(660, 602, T0 - 25 * 3600)
        self.lane_ticket(603)
        self.gh.pulls[OTHER] = [{"number": 770, "created_at": stamp(T0 - 25 * 3600),
                                 "head": {"ref": "loom/603-thing", "sha": "abc770"},
                                 "labels": [], "user": {"login": AUTHOR}}]
        self.bot_threads(2)
        self.run_at(T0)
        rolled = {i["title"]: i for i in self.breaches()}
        self.assertEqual(len(rolled), 2, sorted(rolled))
        (own,) = [i for t, i in rolled.items() if t.endswith(REPO)]
        (hidden,) = [i for t, i in rolled.items() if "private repo" in t]
        body = hidden["body"]
        self.assertIn("pull request 770 of a private repo", body)
        # The private repo's listing fails from now on; #660 merges.
        self.gh.errors.add(OTHER)
        self.gh.pulls[REPO] = []
        self.gh.pull_state[(REPO, 660)] = {"state": "closed", "merged_at": stamp(T0 + 60)}
        for n in range(1, 5):
            self.run_at(T0 + n * invariants.CLEAR_AFTER)
        self.assertEqual(hidden["body"], body)
        self.assertEqual(hidden["state"], "open")
        self.assertNotIn((REPO, hidden["number"]), self.gh.comments)
        self.assertIn(invariants.ROLL_NONE, own["body"])
        self.assertEqual(own["state"], "closed", self.said)


class OldIssueSubjectTest(unittest.TestCase):
    """loom#2260 passed the 12-hex half of a breach key to settled(), and
    _invariant_settled reads only owner/name#N, so nothing could ever close.
    The fake here is the real _invariant_settled over a pull request table."""

    PUBLIC = "example-owner/open-thing"

    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        self.asked = []
        patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": AUTHOR})
        patcher.start()
        self.addCleanup(patcher.stop)

    def settled(self, invariant, subject):
        def pull(repo, number):
            self.asked.append("%s#%d" % (repo, number))
            return "lane pull request %d merged" % number
        return station.Station._invariant_settled(
            types.SimpleNamespace(_lane_pull_settled=pull), Facts(T0), invariant, subject)

    def old(self, subject, title, marker=None, number=71):
        key = invariants.Breach("lane-pr-stalled", subject, "t", (), T0).key
        self.gh.issues[number] = {
            "number": number, "id": 9000 + number, "state": "open", "closed_at": None,
            "title": title, "labels": [{"name": n} for n in invariants.LABELS],
            "created_at": stamp(T0), "body": "%s\nOld.\n\nPart of #%d" % (
                marker or invariants.MARKER % key, GOAL)}
        return self.gh.issues[number]

    def run_filer(self):
        invariants.Filer(self.gh, REPO, GOAL, {}, T0, self.said.append,
                         settled=self.settled).run([])

    def test_an_issue_of_this_repo_settles_from_its_title(self):
        issue = self.old("%s#660" % REPO, "Lane pull request #660 open 7.0 hours: x")
        self.run_filer()
        self.assertEqual(self.asked, ["%s#660" % REPO])
        self.assertEqual(issue["state"], "closed", self.said)
        self.assertNotIn((REPO, 71), self.gh.comments)

    def test_an_issue_of_another_public_repo_settles_from_its_title(self):
        issue = self.old("%s#77" % self.PUBLIC,
                         "Lane pull request 77 of %s open 7.0 hours: x" % self.PUBLIC)
        self.run_filer()
        self.assertEqual(self.asked, ["%s#77" % self.PUBLIC])
        self.assertEqual(issue["state"], "closed", self.said)

    def test_the_marker_carries_the_subject_past_a_rewritten_title(self):
        subject = "%s#77" % self.PUBLIC
        key = invariants.Breach("lane-pr-stalled", subject, "t", (), T0).key
        issue = self.old(subject, "Look at this", invariants.MARKER_SUBJECT % (key, subject))
        self.run_filer()
        self.assertEqual(self.asked, [subject])
        self.assertEqual(issue["state"], "closed", self.said)

    def test_a_subject_that_does_not_hash_to_the_key_is_not_believed(self):
        key = invariants.Breach("lane-pr-stalled", "%s#660" % REPO, "t", (), T0).key
        issue = self.old("%s#660" % REPO, "Look at this",
                         invariants.MARKER_SUBJECT % (key, "%s#661" % REPO))
        self.run_filer()
        self.assertEqual(self.asked, [])
        self.assertEqual(issue["state"], "open")

    def test_a_private_subject_stays_unread_and_never_reaches_a_marker(self):
        issue = self.old("%s#5" % OTHER, "Lane pull request 5 of a private repo open 7.0 hours: x")
        self.run_filer()
        # Its repo cannot be told, so it folds, and settled() is never asked.
        self.assertEqual(self.asked, [])
        self.assertIn("no longer files one issue per", issue["body"])
        filer = invariants.Filer(self.gh, REPO, GOAL, {}, T0, self.said.append)
        hidden = invariants.Breach("ticket-outside-epic", "%s#5" % OTHER,
                                   "Ticket 5 of a private repo", ("x",), T0)
        self.assertEqual(filer._marker(hidden), invariants.MARKER % hidden.key)

    def test_a_subject_the_breach_names_goes_in_the_marker(self):
        filer = invariants.Filer(self.gh, REPO, GOAL, {}, T0, self.said.append)
        for subject, title in (("%s#5" % REPO, "Ticket #5"),
                               ("%s#5" % self.PUBLIC, "Ticket 5 of %s" % self.PUBLIC)):
            b = invariants.Breach("ticket-outside-epic", subject, title, ("x",), T0)
            found = invariants.MARKER_RE.search(filer._marker(b))
            self.assertEqual(found.groups(), (b.key, subject))

    def test_a_rolling_breach_never_writes_its_subject(self):
        filer = invariants.Filer(self.gh, REPO, GOAL, {}, T0, self.said.append)
        b = invariants.Breach("lane-held", "%s#5" % REPO, "Ticket #5", ("x",), T0, rolling=True)
        self.assertEqual(filer._marker(b), invariants.MARKER % b.key)


class SettleBeforeFilingTest(unittest.TestCase):
    """loom#2408: a lane pull request issue of the 2306 shape (marker key, a
    title naming #N, no subject map) never closed once its pull request ended,
    because filing other invariants spent the pass's writes before settling
    ran. The settled callback here is the real _invariant_settled over the
    real _lane_pull_settled."""

    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": AUTHOR})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = types.SimpleNamespace(
            _pulls={}, gh=self.gh,
            _invariant_ref=lambda repo, number, noun="issue": Ref(
                "%s#%d" % (repo, number), "%s #%d" % (noun, number)))
        self.host._lane_pull_settled = (
            lambda repo, number: station.Station._lane_pull_settled(self.host, repo, number))

    def settled(self, invariant, subject):
        return station.Station._invariant_settled(self.host, Facts(T0), invariant, subject)

    def old(self, number, pull):
        key = invariants.Breach("lane-pr-stalled", "%s#%d" % (REPO, pull), "t", (), T0).key
        self.gh.issues[number] = {
            "number": number, "id": 9000 + number, "state": "open", "closed_at": None,
            "title": "Lane pull request #%d open 13.5 hours: 1 review thread, no fix round" % pull,
            "labels": [{"name": n} for n in invariants.LABELS], "created_at": stamp(T0),
            "body": "%s\nBroken.\n\nTicket: #%d\n\nPart of #%d" % (
                invariants.MARKER % key, pull - 3, GOAL)}
        return self.gh.issues[number]

    def others(self, tag, count=invariants.WRITES_MAX):
        return [invariants.Breach("remote-silent", "%s-%d" % (tag, n), "Lane %s-%d silent" % (
            tag, n), ("x",), T0) for n in range(count)]

    def run_filer(self, breaches, now=T0):
        invariants.Filer(self.gh, REPO, GOAL, {}, now, self.said.append,
                         settled=self.settled).run(breaches)

    def test_a_merged_pull_closes_its_issue_while_other_breaches_spend_the_writes(self):
        issue = self.old(71, 660)
        self.gh.pull_state[(REPO, 660)] = {"state": "closed", "merged_at": stamp(T0 + 60)}
        self.run_filer(self.others("a"))
        self.assertEqual(issue["state"], "closed", self.said)
        self.assertTrue(any("closed settled" in s for s in self.said), self.said)

    def test_the_list_drains_while_every_pass_files_something_else(self):
        issues = [self.old(71 + n, 660 + n) for n in range(3)]
        for n in range(3):
            self.gh.pull_state[(REPO, 660 + n)] = {"state": "closed",
                                                   "merged_at": stamp(T0 + 60)}
        for n in range(2):
            before = sum(1 for i in issues if i["state"] == "closed")
            self.run_filer(self.others("p%d" % n), T0 + n * 60)
            self.assertGreater(sum(1 for i in issues if i["state"] == "closed"), before,
                               self.said)
        self.assertEqual(sum(1 for i in issues if i["state"] == "closed"), 3)

    def test_an_open_pull_keeps_its_issue_open(self):
        issue = self.old(71, 660)
        self.gh.pull_state[(REPO, 660)] = {"state": "open", "merged_at": None}
        self.run_filer(self.others("a"))
        self.assertEqual(issue["state"], "open")


if __name__ == "__main__":
    unittest.main()
