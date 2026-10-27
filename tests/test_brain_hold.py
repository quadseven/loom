"""needs-brain holds nothing resolves (loom#1872, bus/brain_hold.py and the
factory's brain_holds): a hold older than a day that Loom set is resolved, a
person's hold never is, and a run resolves at most a few."""

import os
import tempfile
import unittest
from unittest import mock

from bus import brain, brain_hold, factory, hunt_plan, labels, station, status
from bus.github import GitHubError
from tests.test_bus_factory import (BODY_FAIL, CHIEF, ME, NOW, PLAN_BODY, PLAN_TICKET, REPO,
                                    SIZE_FAIL, FakeGitHub, chief_summary, iso)

LOOM_APP = "loom-example[bot]"
OPERATOR = "alice-example"
TRUSTED = ["Alice-Example", LOOM_APP]
DAY = 24 * 3600


def held_by(gh, number, login, seconds_ago=30 * 3600):
    """Ticket `number` carries needs-brain, added by `login` that long ago."""
    gh.issues[number]["labels"].append({"name": brain_hold.LABEL})
    gh.events[number] = [
        {"event": "labeled", "label": {"name": "loom:ticket"}, "actor": {"login": LOOM_APP},
         "created_at": iso(seconds_ago + 600)},
        {"event": "labeled", "label": {"name": brain_hold.LABEL},
         "actor": {"login": login} if login else None, "created_at": iso(seconds_ago)}]


class BrainHoldsTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()

    def run_holds(self, limit=brain_hold.LIFT_PER_RUN, write=True):
        f = factory.Factory(self.gh, [REPO], NOW, trusted=TRUSTED)
        return f.brain_holds(limit, write)

    def names(self, number):
        return factory._labels(self.gh.issues[number])

    def events(self, number):
        return [c["body"] for c in status.expand(self.gh.comments[number], [ME["login"]])]

    def test_a_failed_tickets_old_loom_hold_goes_to_the_hand_off_pool_with_one_note(self):
        self.gh.ticket(5, "failed")
        held_by(self.gh, 5, LOOM_APP)
        self.gh.comments[5].append({"id": 70, "user": ME, "created_at": iso(30 * 3600 - 5),
                                    "body": "<!-- loom:x -->\nPast the model ladder (tried a model)."})
        report = self.run_holds()
        names = self.names(5)
        self.assertNotIn(brain_hold.LABEL, names)
        self.assertIn(labels.HANDOFF, names)
        self.assertIn("loom:failed", names)
        self.assertFalse(brain._in_flight(self.gh.issues[5]))
        notes = [e for e in self.events(5) if brain_hold.LIFT_MARKER in e]
        self.assertEqual(len(notes), 1)
        self.assertIn("set by %s: Past the model ladder (tried a model)." % LOOM_APP, notes[0])
        # One status comment beside the note that set the hold: no new email per pass.
        self.assertEqual(len(self.gh.comments[5]), 2)
        self.assertEqual(report[REPO]["lifted"], 1)
        self.run_holds()
        self.assertEqual(len([e for e in self.events(5) if brain_hold.LIFT_MARKER in e]), 1)

    def test_a_queued_ticket_goes_back_to_the_queue_with_no_lane(self):
        self.gh.ticket(6, "briefed", lane="codex")
        held_by(self.gh, 6, LOOM_APP)
        self.run_holds()
        names = self.names(6)
        self.assertIn("loom:queued", names)
        self.assertEqual([x for x in names if x.startswith("lane:")], [])
        self.assertEqual(names.count(labels.HANDOFF), 1)
        self.assertFalse(brain._in_flight(self.gh.issues[6]))
        self.assertIn("no note names it; it was held at loom:briefed",
                      [e for e in self.events(6) if brain_hold.LIFT_MARKER in e][0])

    def plan_held(self, extra_runs=()):
        self.gh.ticket(5, "pr", body=PLAN_TICKET)
        self.gh.pull(50, 5, green=False, body=PLAN_BODY)
        self.gh.runs["sha50"] = [
            {"name": CHIEF, "status": "completed", "conclusion": "failure",
             "output": {"title": "Hunt Plan hold", "summary": chief_summary([BODY_FAIL, SIZE_FAIL])}},
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed",
             "conclusion": "success"}] + list(extra_runs)
        held_by(self.gh, 5, LOOM_APP)

    def test_a_held_pull_request_failing_only_the_plan_check_is_repaired_and_the_hold_lifted(self):
        self.plan_held()
        report = self.run_holds()
        self.assertEqual(hunt_plan.problems(self.gh.pulls[50]["body"]), [])
        names = self.names(5)
        self.assertNotIn(brain_hold.LABEL, names)
        self.assertNotIn(labels.HANDOFF, names)
        self.assertIn("loom:pr", names)
        events = self.events(5)
        self.assertEqual(len(self.gh.comments[5]), 1)
        self.assertTrue(any(factory.PLAN_REPAIR_MARKER % "sha50" in e
                            and brain_hold.LIFT_MARKER in e for e in events), events)
        self.assertEqual(report[REPO]["lifted"], 1)

    def test_a_held_pull_request_with_another_failing_check_is_handed_off(self):
        self.plan_held(extra_runs=[{"name": "unittest", "status": "completed",
                                    "conclusion": "failure"}])
        self.run_holds()
        self.assertEqual(self.gh.pulls[50]["body"], PLAN_BODY)
        names = self.names(5)
        self.assertNotIn(brain_hold.LABEL, names)
        self.assertNotIn("loom:pr", names)
        self.assertIn("loom:failed", names)
        self.assertIn(labels.HANDOFF, names)
        self.assertFalse(brain._in_flight(self.gh.issues[5]))

    def test_a_body_rebuilt_on_this_head_that_now_passes_lifts_the_hold(self):
        self.plan_held()
        self.gh.runs["sha50"][0]["conclusion"] = "success"
        self.gh.comments[5].append({"id": 71, "user": ME, "created_at": iso(3 * 3600),
                                    "body": factory.PLAN_REPAIR_MARKER % "sha50" + "\nRebuilt."})
        self.run_holds()
        self.assertNotIn(brain_hold.LABEL, self.names(5))
        self.assertNotIn(labels.HANDOFF, self.names(5))
        self.assertEqual(self.gh.pulls[50]["body"], PLAN_BODY)

    def test_a_rebuilt_body_the_plan_check_still_fails_past_the_grace_is_handed_off(self):
        self.plan_held()
        self.gh.comments[5].append({"id": 71, "user": ME, "created_at": iso(3 * 3600),
                                    "body": factory.PLAN_REPAIR_MARKER % "sha50" + "\nRebuilt."})
        self.run_holds()
        self.assertIn(labels.HANDOFF, self.names(5))
        self.assertNotIn(brain_hold.LABEL, self.names(5))

    def test_a_held_pull_request_whose_checks_run_waits(self):
        self.plan_held(extra_runs=[{"name": "unittest", "status": "in_progress"}])
        report = self.run_holds()
        self.assertIn(brain_hold.LABEL, self.names(5))
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(report[REPO]["lifted"], 0)

    def test_a_hold_a_person_set_is_never_lifted_and_is_counted(self):
        self.gh.ticket(5, "failed")
        held_by(self.gh, 5, OPERATOR)
        report = self.run_holds()
        self.assertIn(brain_hold.LABEL, self.names(5))
        self.assertEqual(self.gh.writes, [])
        self.assertEqual((report[REPO]["old"], report[REPO]["person"]), (1, 1))

    def test_a_hold_with_no_readable_setter_is_left_and_counted(self):
        self.gh.ticket(5, "failed")
        self.gh.ticket(6, "failed")
        held_by(self.gh, 5, None)
        self.gh.issues[6]["labels"].append({"name": brain_hold.LABEL})  # no event at all
        report = self.run_holds()
        self.assertIn(brain_hold.LABEL, self.names(5))
        self.assertIn(brain_hold.LABEL, self.names(6))
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(report[REPO]["unknown"], 2)

    def test_a_hold_younger_than_a_day_is_left_alone(self):
        self.gh.ticket(5, "failed")
        held_by(self.gh, 5, LOOM_APP, seconds_ago=DAY - 60)
        report = self.run_holds()
        self.assertIn(brain_hold.LABEL, self.names(5))
        self.assertEqual(report[REPO]["old"], 0)

    def test_a_run_resolves_at_most_its_limit_oldest_first_and_an_error_uses_a_turn(self):
        for n in range(1, 8):
            self.gh.ticket(n, "failed")
            held_by(self.gh, n, LOOM_APP, seconds_ago=DAY + n * 3600)
        real = self.gh.rest

        def rest(method, path, *args, **kwargs):
            if method == "PUT" and path.endswith("/issues/7/labels"):
                raise GitHubError("boom", 500)
            return real(method, path, *args, **kwargs)
        self.gh.rest = rest
        report = self.run_holds(limit=3)
        still = [n for n in range(1, 8) if brain_hold.LABEL in self.names(n)]
        # #7 (the oldest) failed and used its turn; #6 and #5 were resolved.
        self.assertEqual(still, [1, 2, 3, 4, 7])
        self.assertEqual(report[REPO]["lifted"], 2)
        self.assertEqual(report[REPO][brain_hold.LOOM], 7)

    def test_a_dry_run_writes_nothing_and_counts_what_a_run_could_resolve(self):
        for n in (1, 2):
            self.gh.ticket(n, "failed")
            held_by(self.gh, n, LOOM_APP)
        report = self.run_holds(write=False)
        self.assertEqual(self.gh.writes, [])
        self.assertEqual((report[REPO][brain_hold.LOOM], report[REPO]["lifted"]), (2, 0))


class SetterTest(unittest.TestCase):
    def test_the_newest_label_event_names_the_setter(self):
        events = [{"event": "labeled", "label": {"name": "needs-brain"},
                   "actor": {"login": LOOM_APP}, "created_at": "2030-01-01T00:00:00Z"},
                  {"event": "unlabeled", "label": {"name": "needs-brain"},
                   "actor": {"login": OPERATOR}, "created_at": "2030-01-01T01:00:00Z"},
                  {"event": "labeled", "label": {"name": "needs-brain"},
                   "actor": {"login": OPERATOR}, "created_at": "2030-01-01T02:00:00Z"}]
        loom = brain_hold.loom_logins(TRUSTED)
        self.assertEqual(loom, {LOOM_APP})
        self.assertEqual(brain_hold.setter(events, loom).kind, brain_hold.PERSON)
        self.assertEqual(brain_hold.setter(events[:2], loom).kind, brain_hold.LOOM)
        self.assertEqual(brain_hold.setter([], loom).kind, brain_hold.UNKNOWN)


class StationStampTest(unittest.TestCase):
    """The run is stamped in a finally block: a failing run waits LIFT_EVERY
    like a finished one, instead of repeating every pass."""

    def test_a_failing_run_is_stamped_and_not_repeated_until_due(self):
        st = station.Station.__new__(station.Station)
        st.state = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(st.state, ignore_errors=True))
        st.now = NOW
        st.s = mock.Mock(factory_on=True, transcript_url="", transcript_token_ssm="")
        st._deferred = lambda: False
        runner = mock.Mock()
        runner.brain_holds.side_effect = GitHubError("boom", 500)
        st._factory = lambda: runner
        with self.assertRaises(GitHubError):
            st.lift_brain_holds()
        self.assertTrue(os.path.exists(os.path.join(st.state, station.BRAIN_HOLDS_FILE)))
        st.lift_brain_holds()
        self.assertEqual(runner.brain_holds.call_count, 1)
        st.now += brain_hold.LIFT_EVERY
        runner.brain_holds.side_effect = None
        runner.brain_holds.return_value = {}
        st.lift_brain_holds()
        self.assertEqual(runner.brain_holds.call_count, 2)


if __name__ == "__main__":
    unittest.main()
