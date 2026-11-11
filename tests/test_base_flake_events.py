"""Regression coverage for retry events, without external side effects."""
import json
from pathlib import Path
import tempfile
import unittest

from bus import base_flakes, runners
from lanes.k8s import lane_job
import test_base_retry_evidence as retry_fixture


class BaseFlakeEventTests(unittest.TestCase):
    run_baseline = retry_fixture.BaseRetryEvidenceTests.run_baseline

    def test_real_flake_writes_one_structured_event(self):
        run = self.run_baseline()
        self.assertEqual(len(run["events"]), 1)
        event = run["events"][0]
        self.assertEqual(event["repo"], "example/project")
        self.assertEqual(event["base_commit"], "source-head")
        self.assertEqual(event["first_exit"], 1)
        self.assertIn("passing && flaky && later", event["command"])
        self.assertEqual(len(event["excerpt"].splitlines()), 20)
        self.assertNotIn("second-run output", event["excerpt"])
        self.assertNotIn("earlier setup output", event["excerpt"])
        self.assertGreater(event["at"], 0)
        self.assertEqual(len(event["id"]), 32)

    def test_long_first_run_line_is_bounded_by_bytes(self):
        run = self.run_baseline("long-line")
        self.assertEqual(len(run["events"]), 1)
        excerpt = run["events"][0]["excerpt"]
        self.assertLessEqual(len(excerpt.encode("utf-8")), 4096)
        self.assertLessEqual(len(excerpt.splitlines()), 20)
        self.assertIn("FAIL: test_flaky", excerpt)

    def test_only_real_flakes_write_events(self):
        for mode in ("green", "permanent", "mutated", "interrupted", "loaded", "unavailable"):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_baseline(mode)["events"], [])

    def test_failed_event_write_is_logged_and_does_not_fail_lane(self):
        run = self.run_baseline("write-failed")
        self.assertEqual(run["result"], "0")
        self.assertEqual(run["events"], [])
        self.assertIn("could not write base flake event:", run["log"])
        self.assertIn("IsADirectoryError", run["log"])
        self.assertEqual(run["incidents"], [])


class FlakeSidecarTests(unittest.TestCase):
    def test_utf8_excerpt_is_bounded_without_broken_characters(self):
        text = "earlier\n" + "\N{SNOWMAN}" * 4000 + "\nlast line"
        excerpt = base_flakes.bounded_excerpt(text)
        self.assertLessEqual(len(excerpt.encode("utf-8")), 4096)
        self.assertLessEqual(len(excerpt.splitlines()), 20)
        self.assertTrue(excerpt.endswith("last line"))
        self.assertNotIn("\N{REPLACEMENT CHARACTER}", excerpt)

    def test_repeated_runs_append_distinct_events(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ticket.base-flakes.jsonl"
            for status in (1, 2):
                base_flakes.write_event(path, "example/project", "a" * 40,
                                        "python3 check.py", status, "failed")
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([e["first_exit"] for e in events], [1, 2])
            self.assertNotEqual(events[0]["id"], events[1]["id"])

    def test_existing_report_transports_sidecar_to_station_home(self):
        with tempfile.TemporaryDirectory() as temp:
            local, remote = Path(temp) / "station", Path(temp) / "runner"
            relative = "logs/claude/ticket-1.base-flakes.jsonl"
            path = remote / relative
            path.parent.mkdir(parents=True)
            base_flakes.write_event(path, "example/project", "a" * 40,
                                    "python3 check.py", 1, "first attempt failed")
            names = lane_job.report_files(str(remote), "claude", "ticket-1")
            self.assertEqual(names, [relative])
            station = runners.Runners(None, str(local), "example/factory", [], {},
                                      lambda repo: False, lambda text: None)
            station.apply("claude", "ticket-1", {relative: path.read_bytes()})
            self.assertEqual((local / relative).read_bytes(), path.read_bytes())
