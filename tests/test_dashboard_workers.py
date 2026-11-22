"""The /workers view and POST /api/workers: cluster workers and goal bots from
a posted feed, shared by every dashboard replica through the snapshot store."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard import station_state, workers_view  # noqa: E402
from dashboard.shell import VIEWS  # noqa: E402
from test_dashboard_station_state import State, call  # noqa: E402

ROW = {"kind": "worker", "name": "sample-worker", "state": "running", "started": 1000.0,
       "last_action": "ran the tests", "url": "https://example.com/runs/1"}


class WorkersBodyTest(unittest.TestCase):
    def test_a_good_body_is_kept_with_its_receive_time(self):
        self.assertEqual(workers_view.workers({"rows": [ROW]}, 5.0), {"rows": [ROW], "received_at": 5.0})

    def test_bad_bodies_are_refused(self):
        for body in (None, {"rows": "x"}, {"rows": [1]},
                     {"rows": [dict(ROW, kind="lane")]}, {"rows": [dict(ROW, state="paused")]},
                     {"rows": [dict(ROW, name="a b")]}, {"rows": [dict(ROW, started="soon")]},
                     {"rows": [dict(ROW, started=True)]},
                     {"rows": [dict(ROW, url="javascript:alert(1)")]},
                     {"rows": [ROW] * (workers_view.WORKERS_MAX + 1)}):
            with self.assertRaises(ValueError, msg=repr(body)):
                workers_view.workers(body, 1.0)

    def test_last_action_is_one_trimmed_line(self):
        row = workers_view.workers({"rows": [dict(ROW, last_action="a\n" + "b" * 500)]}, 1.0)["rows"][0]
        self.assertNotIn("\n", row["last_action"])
        self.assertEqual(len(row["last_action"]), workers_view.TEXT_MAX)


class WorkersPageTest(unittest.TestCase):
    def test_no_feed_says_so(self):
        self.assertIn("No cluster worker", workers_view.page_html(None, 1.0))

    def test_rows_show_running_first_and_escape_text(self):
        done = dict(ROW, name="old-one", state="done", started=5000.0)
        evil = dict(ROW, last_action="<script>x</script>")
        page = workers_view.page_html(workers_view.workers({"rows": [done, evil]}, 6000.0), 6000.0)
        self.assertLess(page.index("sample-worker"), page.index("old-one"))
        self.assertNotIn("<script>", page)
        self.assertIn('href="https://example.com/runs/1"', page)

    def test_an_old_feed_is_marked_stale(self):
        stored = workers_view.workers({"rows": [ROW]}, 0.0)
        self.assertIn("out of date", workers_view.page_html(stored, workers_view.MAX_AGE + 1))
        self.assertNotIn("out of date", workers_view.page_html(stored, 10.0))

    def test_the_view_is_in_the_navigation(self):
        self.assertIn("/workers", [href for _, _, href in VIEWS])


class WorkersPostTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": "YOUR_UPLOAD_TOKEN_HERE"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_post_to_one_replica_shows_on_the_other(self):
        backend = station_state.MemoryBackend()
        first, second = State(station_state.Store(backend)), State(station_state.Store(backend))
        self.assertEqual(call(first, "POST", "/api/workers", {"rows": [ROW]}), (201, {"rows": 1}))
        station_state.sync(second, force=True)
        self.assertEqual(second.workers["rows"][0]["name"], "sample-worker")

    def test_a_bad_post_is_400(self):
        state = State(station_state.Store(station_state.MemoryBackend()))
        self.assertEqual(call(state, "POST", "/api/workers", {"rows": [{"kind": "x"}]})[0], 400)


if __name__ == "__main__":
    unittest.main()
