from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import station, tickets  # noqa: E402

REPO = "example-owner/example-repo"
AUTHORS = ["example-bot[bot]"]
HEAD = "a" * 40


class TimedOutRoundNotPersistedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = station.Settings(self.temp.name, [REPO], AUTHORS, ["example-owner"])
        self.station = station.Station(mock.Mock(), settings)

    def fix_comment(self, head=HEAD):
        body = (tickets.FIX_MARKER + "\nPull request: %s#99, current\n"
                "### Check: lint\nfix the lint failure\n"
                "<!-- loom:fix-head %s -->") % (REPO, head)
        return {"body": body, "created_at": "2026-11-17T10:00:00Z",
                "user": {"login": AUTHORS[0]}}

    def timeout_comment(self, branch="loom/12-fix-it-timedout-1234"):
        return {"body": "Lane codex failed: [transient] agent exited 124; "
                         "the edits are on branch " + branch,
                "created_at": "2026-11-17T10:05:00Z",
                "user": {"login": AUTHORS[0]}}

    def round_inputs(self, head=HEAD):
        return ([self.fix_comment(), self.timeout_comment()],
                {"head": {"sha": head, "ref": "loom/12-fix-it"}},
                [("lint", "still failing")])

    def test_kept_timeout_on_unchanged_head_retries_same_lane_without_escalation(self):
        comments, detail, failed = self.round_inputs()
        posted = []
        moved = []
        with mock.patch.object(self.station, "_post_fix_comment",
                               side_effect=lambda *args, **kwargs: posted.append(args)), \
             mock.patch.object(self.station, "_move_fix_to_lane",
                               side_effect=lambda *args: moved.append(args)), \
             mock.patch.object(self.station, "_stop_fix_rounds") as stop, \
             mock.patch.object(self.station, "_climb_stuck",
                               side_effect=lambda *args: stop()) as climb, \
             mock.patch.object(self.station, "_issue", return_value={"labels": []}), \
             mock.patch("bus.station._say") as say:
            self.station._post_fix_or_escalate(
                REPO, 12, 99, "codex", detail, HEAD, comments, ["loom:pr", "lane:codex"],
                failed, [], 3)

        self.assertEqual(len(posted), 1)
        self.assertEqual(moved[0][2], "codex")
        self.assertFalse(stop.called)
        self.assertFalse(climb.called)
        self.assertTrue(any("not counted as persisted" in call.args[0]
                            and "loom/12-fix-it-timedout-1234" in call.args[0]
                            for call in say.call_args_list))

    def test_round_that_pushed_new_head_still_escalates_persistent_failure(self):
        comments, detail, failed = self.round_inputs(head=HEAD)
        detail["head"]["sha"] = "b" * 40
        with mock.patch.object(self.station, "_climb_stuck", return_value=None) as climb, \
             mock.patch.object(self.station, "_stop_fix_rounds") as stop, \
             mock.patch.object(self.station, "_post_fix_comment") as post:
            self.station._post_fix_or_escalate(
                REPO, 12, 99, "codex", detail, "b" * 40, comments,
                ["loom:pr", "lane:codex"], failed, [], 3)

        climb.assert_called_once()
        self.assertFalse(stop.called)
        self.assertFalse(post.called)

    def test_unpushed_timeout_still_counts_toward_the_rung_round_cap(self):
        comments, detail, failed = self.round_inputs()
        with mock.patch.object(self.station, "_stop_fix_rounds") as stop, \
             mock.patch.object(self.station, "_climb_stuck") as climb, \
             mock.patch.object(self.station, "_post_fix_comment") as post:
            self.station._post_fix_or_escalate(
                REPO, 12, 99, "codex", detail, HEAD, comments,
                ["loom:pr", "lane:codex"], failed, [], 1)

        stop.assert_called_once()
        self.assertFalse(climb.called)
        self.assertFalse(post.called)


if __name__ == "__main__":
    unittest.main()
