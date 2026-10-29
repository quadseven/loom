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


class FilerTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        self.state = {}
        # The Filer reads only trusted comments (loom#1956); the fake writes
        # every comment as AUTHOR.
        patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": AUTHOR})
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_a_refused_sub_issue_link_rolls_over_to_a_batch_epic_and_is_logged(self):
        """loom#2023: GitHub answered 422 for the repairs epic at its
        sub-issue limit, and the issue kept only a Part of line naming it."""
        self.gh.issues[GOAL] = {"number": GOAL, "id": 9000 + GOAL, "state": "open",
                                "title": "Goal", "labels": [{"name": "loom:goal"}], "body": ""}
        repairs = dict(epic(50), title=brain.REPAIRS_TITLE)
        batch = dict(epic(60), title="Epic: factory repairs the brain finds by hand, batch 2")
        self.gh.subs[(REPO, GOAL)] = [repairs, batch]
        self.gh.subs[(REPO, 50)] = [ticket(n, state="closed") for n in range(200, 299)]
        self.gh.subs[(REPO, 60)] = [ticket(7)]
        self.gh.refuse_subs.add(50)
        self.file(self.breach())
        (filed,) = [i for i in self.gh.issues.values() if "loom:invariant" in i.get("labels", [])]
        self.assertTrue(filed["body"].rstrip().endswith("Part of #60"), filed["body"])
        self.assertNotIn("Part of #50", filed["body"])
        self.assertIn(filed["number"], [c["number"] for c in self.gh.subs[(REPO, 60)]])
        self.assertTrue(any("HTTP 422" in line and "#50" in line for line in self.said),
                        self.said)
        # The next filing this pass goes straight to the batch.
        self.file(invariants.Breach("goal-full", "x/y#2", "Another", ("line",), T0))
        later = [i for i in self.gh.issues.values() if i.get("title") == "Another"][0]
        self.assertTrue(later["body"].rstrip().endswith("Part of #60"), later["body"])

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
             "created_at": stamp(T0 - 3 * 3600), "user": {"login": AUTHOR}})
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
            {"body": "Broken again after this was closed: evidence", "created_at": stamp(T0 - 2 * 86400),
             "user": {"login": AUTHOR}},
            {"body": "Broken again after this was closed: evidence", "created_at": stamp(T0 - 86400),
             "user": {"login": AUTHOR}},
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
