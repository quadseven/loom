"""Station findings from an audit (loom#2583).

1. A dead-lane runaway whose hold stayed for want of a free tier lane is
   released on a later pass once a lane of its tier is free.
2. A done brief whose pull request is never found stops retrying after
   DONE_NO_PR_WAIT and takes the failure path.
3. A half-done claim met with the pass's request target reached waits for
   the next pass instead of being refused as untrusted.
4. reported.json drops the keys of briefs no longer on disk.
5. An EVERY step that ends on a RateLimited raised by its first request is
   not stamped, so the pass after the reset runs it.

The contributor-linked loop in _reconcile_repo is removed without a test of
its own: it never changed a label (the first loop reconciles every closed
pull request), so no test can fail on main. The reconcile tests in
tests/test_bus_station.py cover the remaining path.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from bus import claim_referee, claimed, label_pass, stale_claims, station, tickets
from bus.github import GitHub, RateLimited
from tests import test_bus_station as base
from shadow_env import setUpModule, tearDownModule  # noqa: F401

REPO = base.REPO
DEAD = base.DeadLaneRunawayTest


class DeadLaneReleaseLaterTest(base.Base):
    MEDIUM = DEAD.MEDIUM
    bodies = DEAD.bodies
    key = DEAD.key
    file_invariant = DEAD.file_invariant
    hold = DEAD.hold

    def setUp(self):
        super().setUp()
        self.fake.add_goal(40)
        self.settings.invariant_goal = "%s#40" % REPO

    def marked(self):
        return [b for b in self.bodies() if b.startswith("<!-- loom:runaway-dead-lane v1")]

    def test_a_hold_that_stayed_is_released_once_a_tier_lane_is_free(self):
        self.file_invariant("codex-2")
        self.hold(lane="codex-2")
        self.heartbeat("codex", available=False)
        self.run_pass()
        self.assertIn("loom:runaway", self.fake.names(12))
        self.assertTrue(any("the hold stays" in b for b in self.marked()))
        # A later pass: lane codex, of the same tier, is free again.
        self.heartbeat("codex")
        self.run_pass()
        names = self.fake.names(12)
        self.assertNotIn("loom:runaway", names)
        self.assertIn("lane:codex", names)
        self.assertTrue(any("Runaway hold released" in b for b in self.marked()))
        # Released once: a third pass posts nothing more.
        count = len(self.marked())
        self.run_pass()
        self.assertEqual(len(self.marked()), count)


class DoneWithoutPullRequestTest(base.Base):
    def finished(self, age):
        self.fake.add(12, state="running")
        name = tickets.ticket_name(REPO, 12, 1)
        path = self.inbox("codex", "done", name + ".md")
        self.write(path, tickets.render_brief(REPO, self.fake.issues[12], "codex", 1))
        # The brief landed in done/ `age` seconds ago. A file's ctime cannot
        # be set back, so the landing time is read through _landed_at.
        at = time.time() - age
        patcher = mock.patch.object(station, "_landed_at", return_value=at)
        patcher.start()
        self.addCleanup(patcher.stop)
        return name

    def reported(self):
        path = os.path.join(self.home, "state", "station", "reported.json")
        try:
            with open(path) as fh:
                return json.load(fh)
        except OSError:
            return {}

    def test_a_fresh_done_brief_with_no_pull_request_is_retried(self):
        self.finished(age=60)
        self.run_pass()
        self.assertIn("loom:running", self.fake.names(12))
        self.assertEqual(self.reported(), {})

    def test_an_old_done_brief_with_no_pull_request_takes_the_failure_path(self):
        name = self.finished(age=station.DONE_NO_PR_WAIT + 60)
        self.run_pass()
        names = self.fake.names(12)
        self.assertNotIn("loom:running", names)
        self.assertIn("loom:failed", names)
        self.assertIn("codex/%s" % name, self.reported())


class LandedAtTest(unittest.TestCase):
    def test_a_brief_moved_by_rename_counts_from_its_move(self):
        with tempfile.TemporaryDirectory() as folder:
            first = os.path.join(folder, "brief.md")
            with open(first, "w") as fh:
                fh.write("brief")
            written = time.time() - 3 * 3600
            os.utime(first, (written, written))
            moved = os.path.join(folder, "done.md")
            os.replace(first, moved)
            self.assertGreater(station._landed_at(moved, 0.0), written + 3600)
            self.assertEqual(station._landed_at(os.path.join(folder, "gone"), 5.0), 5.0)


class TargetReachedClaimTest(base.Base):
    def test_a_half_done_claim_waits_for_the_next_pass_when_the_target_is_reached(self):
        self.fake.add(12, state="briefed", lane="codex")
        name = tickets.ticket_name(REPO, 12, 1)
        claiming = self.inbox("codex", name + ".claiming")
        self.write(claiming, tickets.render_brief(REPO, self.fake.issues[12], "codex", 1))
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        # The trust check's own events read is the one that reaches the
        # pass's request target.
        with mock.patch.object(runner, "_target_reached", return_value=True):
            runner.finish_claims()
        self.assertTrue(os.path.exists(claiming))
        untrusted = os.path.join(runner.state, "untrusted.json")
        seen = {}
        if os.path.exists(untrusted):
            with open(untrusted) as fh:
                seen = json.load(fh)
        self.assertNotIn("%s#12" % REPO, seen)


class ReportedLedgerTest(base.Base):
    def test_keys_of_briefs_no_longer_on_disk_are_dropped(self):
        self.fake.add(12, state="running")
        name = tickets.ticket_name(REPO, 12, 1)
        self.write(self.inbox("codex", "done", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex", 1))
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        path = os.path.join(runner.state, "reported.json")
        self.write(path, json.dumps({"codex/%s" % name: "done",
                                     "codex/gone-brief": "done",
                                     "no-such-lane/other": "failed"}))
        runner.report_outcomes()
        # A lane the station does not run keeps its keys.
        with open(path) as fh:
            self.assertEqual(json.load(fh), {"codex/%s" % name: "done",
                                             "no-such-lane/other": "failed"})


class EveryStampTest(base.Base):
    STEPS = (
        ("settle_claimed_tickets", claimed.STATE_FILE),
        ("release_stale_claims", stale_claims.STATE_FILE),
        ("clean_labels", label_pass.STATE_FILE),
        ("referee_claims", claim_referee.STATE_FILE),
    )

    def setUp(self):
        super().setUp()
        self.settings.settle_claimed = "on"
        self.settings.claim_idle_hours = 2
        self.settings.claim_referee = "on"

    def run_step(self, step, read_first):
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)

        def refused():
            if read_first:
                runner.gh.rest("GET", "/repos/%s/issues/12" % REPO)
            # The refused request is sent and counted, as GitHub._send does.
            runner.gh.requests += 1
            raise RateLimited("GET: budget exhausted", 403, "", int(time.time()) + 600)

        with mock.patch.object(runner, "_verify_repos", side_effect=refused):
            with self.assertRaises(RateLimited):
                getattr(runner, step)()
        return runner

    def stamped(self, runner, state_file):
        path = os.path.join(runner.state, state_file)
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return json.load(fh).get("_at")

    def test_a_step_refused_on_its_first_request_is_not_stamped(self):
        for step, state_file in self.STEPS:
            with self.subTest(step=step):
                runner = self.run_step(step, read_first=False)
                self.assertIsNone(self.stamped(runner, state_file))

    def test_a_step_refused_after_a_read_is_stamped(self):
        self.fake.add(12, state="queued")
        for step, state_file in self.STEPS:
            with self.subTest(step=step):
                runner = self.run_step(step, read_first=True)
                self.assertEqual(self.stamped(runner, state_file), runner.now)


if __name__ == "__main__":
    unittest.main()
