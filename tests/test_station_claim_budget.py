"""Tests for bounding the claim pass reads to leave rotation room in every pass
and running time-due steps when due ahead of the rotation order (loom#3014)."""
from __future__ import annotations

import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bus import scorecard, station
from bus.github import GitHub
from tests.test_bus_station import Base, REPO, labeled


class StationClaimBudgetTest(Base):
    """The claim pass bounds its reads to leave room for the step rotation,
    and due steps run ahead of the rotation order."""

    def test_claim_step_leaves_rotation_room_when_it_would_spend_past_target(self):
        # A briefed ticket whose comments would cost well over the pass target (500 requests).
        self.heartbeat("codex")
        self.fake.add(12, lane="codex", state="briefed")
        self.fake.events[12] = [
            labeled("loom:briefed", "alice-example"), labeled("lane:codex", "bob-example")
        ]
        # 60,000 comments is 600 pages (at 100/page), which would cost 600 requests.
        self.fake.comments[12] = [
            {"id": i, "body": "comment %d" % i, "user": {"login": "alice-example"}}
            for i in range(60000)
        ]

        # Spy on the rotation steps (starting with roll_source_pins).
        ran_steps = []
        original_roll = station.Station.roll_source_pins

        def spy_roll(st):
            ran_steps.append("roll_source_pins")
            return original_roll(st)

        err = io.StringIO()
        with mock.patch.object(station.Station, "roll_source_pins", spy_roll), \
                redirect_stderr(err):
            self.run_pass()

        # The claim step must have stopped at its share of the target, leaving
        # room for rotation steps in the same pass.
        self.assertIn("roll_source_pins", ran_steps,
                      "at least one rotation step must run after the claim step in the same pass; "
                      "output was: %s" % err.getvalue())

    def test_due_score_lanes_runs_within_one_pass_when_rotation_stands_far_from_it(self):
        # Seed scorecard so it fell due over an hour ago.
        self.settings.factory_on = True
        now = 10000.0
        state_dir = os.path.join(self.home, "state", "station")
        os.makedirs(state_dir, exist_ok=True)
        card = scorecard.Scorecard(state_dir, now=now)
        card.data["refreshed"] = now - scorecard.REFRESH - 100
        card.save()
        self.assertTrue(scorecard.Scorecard(state_dir, now=now).due())

        # The rotation stands at step 12 (forward_reports), far from score_lanes (step 36).
        rot_path = os.path.join(state_dir, station.STEP_ROTATION_FILE)
        station._write_json(rot_path, {"next": 12, "carried": None})

        # When the rotation reaches forward_reports (step 12), simulate it carrying over
        # so the rotation loop stops at step 12 without reaching step 36.
        original_timed = station.Station._timed
        step_calls = []

        def spy_timed(st, step):
            step_calls.append(step.__name__)
            if step.__name__ == "forward_reports":
                # forward_reports carries over, stopping the rotation loop
                return False
            return original_timed(st, step)

        score_calls = []
        original_score = station.Station.score_lanes

        def score_lanes(st):
            score_calls.append("score_lanes")
            return original_score(st)

        err = io.StringIO()
        with mock.patch.object(station.Station, "_timed", spy_timed), \
                mock.patch.object(station.Station, "score_lanes", score_lanes), \
                redirect_stderr(err):
            gh = GitHub("token", transport=self.fake)
            code = station.run(self.settings, gh, now=now)
            self.assertEqual(code, 0)

        # score_lanes must have run in this pass because it was due, even though
        # the rotation was at step 12 and stopped there.
        self.assertIn("score_lanes", score_calls,
                      "due score_lanes must run within one pass of falling due; "
                      "called steps: %s; err: %s" % (step_calls, err.getvalue()))
        # Scorecard should have refreshed.
        refreshed_card = scorecard.Scorecard(state_dir, now=now)
        self.assertFalse(refreshed_card.due())
