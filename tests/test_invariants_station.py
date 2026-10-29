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
        self.assertEqual("Invariant: plan-check-failed in %s" % REPO, filed[0]["title"])
        self.assertIn("- Lane pull request #7: %s failure; it says: Size section missing "
                      "(since %s)" % (invariants.DEFAULT_PLAN_CHECK, stamp(T0 - 3600)),
                      filed[0]["body"])
        self.assertNotIn("#8", filed[0]["body"])

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
        self.assertEqual("Invariant: lane-pr-stalled in %s" % REPO, filed["title"])
        self.assertIn("- Lane pull request #534: no open loom:ticket carries it; ticket: #481, "
                      "which is not an open loom:ticket (since %s)" % stamp(T0 - 7 * 3600),
                      filed["body"])
        self.assertIn("`lane-pr-stalled`", filed["body"])

    def test_a_lane_pull_whose_ticket_was_closed_is_a_breach_for_the_same_reason(self):
        self.lane_ticket(481, state="closed")
        self.lane_pull(534, 481, T0 - 7 * 3600)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("#534: no open loom:ticket carries it", filed["body"])
        self.assertIn("#481, which is not an open loom:ticket", filed["body"])

    def test_a_lane_pull_past_the_ladder_files_a_breach_and_five_hours_old_does_not(self):
        self.lane_ticket(506, labels=("loom:ticket", "loom:failed"))
        self.lane_ticket(507, labels=("loom:ticket", "loom:failed"))
        self.lane_pull(657, 506, T0 - 7 * 3600)
        self.lane_pull(658, 507, T0 - 5 * 3600)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("- Lane pull request #657: ticket past the model ladder; ticket: #506 "
                      "(loom:failed)", filed["body"])
        self.assertIn("ticket past the model ladder: brief the work again", filed["body"])
        self.assertNotIn("#658", filed["title"] + filed["body"])

    def test_a_lane_pull_closed_past_the_ladder_clears_its_finding_on_the_next_pass(self):
        # loom#2027: the factory closes such a pull request; the stalled
        # finding is gone on the next pass and its issue closes once quiet.
        self.lane_ticket(506, labels=("loom:ticket", "loom:failed", "handoff:claude"))
        self.lane_pull(657, 506, T0 - 7 * 3600)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertEqual(filed["state"], "open")
        self.gh.pulls[REPO] = []  # closed by the factory: no longer listed open
        st = self.run_at(T0 + invariants.CHECK_EVERY)
        facts = st._invariant_facts({})
        self.assertEqual(invariants.lane_pr_stalled(facts), [])
        self.assertEqual(len(self.breaches()), 1)
        self.run_at(T0 + invariants.CLEAR_AFTER + 3 * invariants.CHECK_EVERY)
        self.assertEqual(filed["state"], "closed", self.said)

    def test_a_lane_pull_with_open_review_threads_and_no_fix_round_files_a_breach(self):
        self.lane_ticket(602)
        self.lane_pull(660, 602, T0 - 7 * 3600)
        self.bot_threads(2)
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("#660: 2 review threads, no fix round", filed["body"])
        self.assertLessEqual(len(filed["title"]), 72)
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
                "Epic #40 of goal home #30: 16 tickets (cap 15)"),
            "goal-epics-over-cap": (lambda: self.gh.subs.update({
                (REPO, GOAL): [epic(n) for n in range(40, 47)]}),
                "Goal home #30: 7 open epics (cap 6)"),
            "closed-epic-open-tickets": (lambda: self.gh.subs.update({
                (REPO, GOAL): [epic(40, state="closed")], (REPO, 40): [ticket(7)]}),
                "Closed epic #40: 1 open tickets: #7"),
            # loom#2030: two open epics in one repo with one title.
            "epic-title-shared": (lambda: self.gh.subs.update({
                (REPO, GOAL): [dict(epic(40), title="Epic: one name"),
                               dict(epic(41), title="Epic: one  name"), epic(42)]}),
                "2 open epics share the title of epic #40"),
        }
        for name, (shape, title) in cases.items():
            with self.subTest(name):
                self.setUp()
                shape()
                self.run_at(T0)
                filed = self.gh.filed()
                self.assertEqual(len(filed), 1, self.said)
                if name in invariants.ROLLED:
                    self.assertEqual("Invariant: %s in %s" % (name, REPO), filed[0]["title"])
                    self.assertIn(title, filed[0]["body"])
                else:
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

    def test_epic_over_cap_counts_sub_issues_and_part_of_links_once(self):
        """loom#2023: 13 sub-issues plus 4 issues linked only by a Part of
        line (one of them closed) are 17 tickets, not 16."""
        part_of = lambda n, state="open": dict(ticket(n, state), body="Why.\n\nPart of #40\n")  # noqa: E731
        subs = [ticket(n) for n in range(50, 63)]
        self.gh.subs[(REPO, GOAL)] = [epic(40)]
        self.gh.subs[(REPO, 40)] = subs
        self.gh.tickets[REPO] = subs + [part_of(70), part_of(71), part_of(72)]
        # The search finds every Part of #40 issue, the closed one too, and a
        # sub-issue that also says so is still counted once.
        self.gh.search_items = [part_of(70), part_of(71), part_of(72),
                                part_of(73, "closed"), part_of(50)]
        self.run_at(T0)
        (filed,) = self.gh.filed()
        self.assertIn("Epic #40 of goal home #30: 17 tickets (cap 15)", filed["body"])

    def test_epic_over_cap_flags_the_standing_repairs_epic_at_the_sub_issue_limit(self):
        repairs = dict(epic(50), title=brain.REPAIRS_TITLE,
                       sub_issues_summary={"total": 100, "completed": 98})
        self.gh.subs[(REPO, GOAL)] = [repairs]
        self.gh.subs[(REPO, 50)] = [ticket(n, state="closed") for n in range(200, 300)]
        self.run_at(T0)
        (filed,) = self.gh.filed()
        self.assertIn("Epic #50 of goal home #30: 100 sub-issues (GitHub's limit 100)",
                      filed["body"])
        self.assertIn("`epic-over-cap`", filed["body"])

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


class SettledSubjectTest(StationBase):
    """loom#2022: an invariant issue whose subject settled for good (its lane
    pull request merged or closed, its epic back at the cap) closes on the
    next pass with one note naming how, not after the clear wait. Since
    loom#2099 these kinds file no per-violation issue, so this covers the
    ones filed before it; a live subject is a line of its tracking issue."""

    def legacy(self, invariant, subject, number=90, labels=None):
        """A per-violation issue as the station filed it before loom#2099,
        with the station state that knows its subject."""
        key = invariants.Breach(invariant, subject, "t", (), T0).key
        self.gh.issues[number] = {
            "number": number, "id": 9000 + number, "state": "open", "closed_at": None,
            "title": "Old %s issue" % invariant,
            "body": "%s\nOld text.\n\nPart of #%d" % (invariants.MARKER % key, GOAL),
            "labels": [{"name": n} for n in (labels or invariants.LABELS)]}
        self.write(os.path.join(self.home, "state", "station", "invariants.json"),
                   json.dumps({"issues": {key: number}, "subjects": {key: subject}}))
        return self.gh.issues[number]

    def leave_open_set(self, **pull):
        self.gh.pulls[REPO] = []
        self.gh.pull_state[(REPO, 660)] = dict({"state": "closed"}, **pull)

    def notes(self, filed):
        return [c["body"] for c in self.gh.comments.get((REPO, filed["number"]), [])]

    def test_a_merged_lane_pull_closes_its_old_issue_with_one_note_naming_the_merge(self):
        filed = self.legacy("lane-pr-stalled", "%s#660" % REPO)
        self.leave_open_set(merged_at=stamp(T0 + 100), closed_at=stamp(T0 + 100))
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(filed["state"], "closed", self.said)
        self.assertEqual(filed["state_reason"], "not_planned")
        (note,) = self.notes(filed)
        self.assertIn("pull request #660 merged at %s" % stamp(T0 + 100), note)
        self.assertIn(invariants.CLEARED_TEXT, note)
        # A later pass writes nothing more, and no tracking issue is filed.
        self.run_at(T0 + 2 * invariants.CHECK_EVERY)
        self.assertEqual(len(self.notes(filed)), 1)
        self.assertEqual(self.breaches(), [])

    def test_a_lane_pull_closed_unmerged_closes_its_old_issue_too(self):
        filed = self.legacy("lane-pr-stalled", "%s#660" % REPO)
        self.leave_open_set(merged_at=None, closed_at=stamp(T0 + 100))
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(filed["state"], "closed", self.said)
        (note,) = self.notes(filed)
        self.assertIn("pull request #660 was closed unmerged at %s" % stamp(T0 + 100), note)

    def test_it_closes_whatever_labels_the_old_issue_carries(self):
        filed = self.legacy("lane-pr-stalled", "%s#660" % REPO,
                            labels=[invariants.LABEL, "needs-info"])
        self.leave_open_set(merged_at=stamp(T0 + 100), closed_at=stamp(T0 + 100))
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(filed["state"], "closed", self.said)

    def test_an_issue_whose_finding_is_still_live_stays_open(self):
        """A live subject is never closed as settled: its line stays on the
        open tracking issue, and no settle note is written anywhere."""
        self.lane_ticket(602)
        self.lane_pull(660, 602, T0 - 7 * 3600)
        self.bot_threads(2)
        self.run_at(T0)
        self.run_at(T0 + invariants.CHECK_EVERY)
        (rolled,) = self.breaches()
        self.assertEqual(rolled["state"], "open")
        self.assertIn("#660", rolled["body"])
        self.assertEqual(self.notes(rolled), [])

    def test_a_gone_breach_whose_pull_request_is_still_open_waits(self):
        filed = self.legacy("lane-pr-stalled", "%s#660" % REPO)
        self.lane_ticket(602)
        # Open, but not stalled now: the breach is gone, the pull request is not.
        self.lane_pull(660, 602, T0 - 3600)
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(filed["state"], "open", self.said)
        self.assertNotIn(("GET", "/repos/%s/pulls/660" % REPO), self.gh.calls)

    def test_a_repo_whose_pull_requests_cannot_be_read_keeps_its_tracking_issue(self):
        """One repo's open pull requests answer an error: its tracking issue
        is not emptied or closed, and the other repo's still updates."""
        self.lane_ticket(602)
        self.lane_pull(660, 602, T0 - 7 * 3600)
        self.lane_ticket(603)
        self.gh.pulls[OTHER] = [{"number": 770, "created_at": stamp(T0 - 7 * 3600),
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
        self.gh.pull_errors.add(OTHER)
        self.gh.pulls[REPO] = []
        self.gh.pull_state[(REPO, 660)] = {"state": "closed", "merged_at": stamp(T0 + 60)}
        for n in range(1, 5):
            self.run_at(T0 + n * invariants.CLEAR_AFTER)
        self.assertEqual(hidden["body"], body)
        self.assertEqual(hidden["state"], "open")
        self.assertNotIn((REPO, hidden["number"]), self.gh.comments)
        self.assertIn(invariants.ROLL_NONE, own["body"])
        self.assertEqual(own["state"], "closed", self.said)

    def test_an_epic_back_at_its_cap_closes_its_issue(self):
        """Since loom#2099 the epic is a line of its repo's tracking issue:
        back at the cap, the line goes, and the empty list closes once it
        has been clear for CLEAR_AFTER."""
        self.gh.subs[(REPO, GOAL)] = [epic(40)]
        self.gh.subs[(REPO, 40)] = [ticket(n, state="closed") for n in range(50, 66)]
        self.run_at(T0)
        (filed,) = self.breaches()
        self.assertIn("Epic #40 of goal home #30: 16 tickets (cap 15)", filed["body"])
        self.gh.subs[(REPO, 40)] = self.gh.subs[(REPO, 40)][:15]
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertIn(invariants.ROLL_NONE, filed["body"])
        self.assertEqual(filed["state"], "open")
        self.run_at(T0 + invariants.CHECK_EVERY + invariants.CLEAR_AFTER)
        self.assertEqual(filed["state"], "closed", self.said)
        (note,) = self.notes(filed)
        self.assertIn(invariants.CLEARED_TEXT, note)

    def test_an_epic_back_at_its_cap_closes_its_old_issue(self):
        filed = self.legacy("epic-over-cap", "%s#40" % REPO)
        self.gh.subs[(REPO, GOAL)] = [epic(40)]
        self.gh.subs[(REPO, 40)] = [ticket(n, state="closed") for n in range(50, 65)]
        self.run_at(T0 + invariants.CHECK_EVERY)
        self.assertEqual(filed["state"], "closed", self.said)
        (note,) = self.notes(filed)
        self.assertIn("epic #40 holds 15 tickets, within the cap of 15", note)


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
