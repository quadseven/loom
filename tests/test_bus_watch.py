from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bus import watch


class GitHub:
    def __init__(self):
        self.calls = []
        self.open_issues = []

    def paginate(self, path):
        return self.open_issues

    def rest(self, method, path, body):
        self.calls.append((method, path, body))
        return type("Response", (), {"data": {"number": 12}})()


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "logs").mkdir()
        self.log = self.home / "logs" / "station.log"
        self.state = self.home / "state" / "station"
        self.gh = GitHub()

    def scan(self, now=100000):
        watch.watch(self.gh, str(self.home), str(self.state), "example-owner/example-repo",
                    now, say=lambda _: None)

    def append(self, line):
        with self.log.open("a") as fh:
            fh.write(line + "\n")

    def test_signature_normalizes_private_data_and_repo_numbers(self):
        first = "HTTP 403 failed for example/repo#12 at /Users/example-user/run token=abcdef"
        second = "HTTP 403 failed for sample/other#99 at /home/sample-user/run token=123456"
        self.assertEqual(watch.signature(first), watch.signature(second))
        self.assertNotIn("example-user", watch.signature(first))
        self.assertIn("403", watch.signature(first))
        self.assertEqual(watch.signature("403 failed on example-a/api"),
                         watch.signature("403 failed on example-b/worker"))

    def test_three_hits_file_once_and_fourth_does_not_comment(self):
        for _ in range(3):
            self.append("HTTP 403 failed for example/repo#12 at /Users/example-user/run")
        self.scan()
        self.assertEqual(len(self.gh.calls), 1)
        body = self.gh.calls[0][2]
        self.assertEqual(body["labels"], ["loom:triage"])
        self.assertNotIn("example-user", json.dumps(body))
        self.append("HTTP 403 failed for sample/repo#99 at /home/sample-user/run")
        self.scan(now=100060)
        self.assertEqual(len(self.gh.calls), 1)
        self.scan(now=103700)
        self.assertEqual(len(self.gh.calls), 2)

    def test_signature_seen_earlier_in_day_files_nothing(self):
        self.append("connection refused")
        self.scan(now=100000)
        for _ in range(3):
            self.append("connection refused")
        self.scan(now=103000)
        self.assertEqual(self.gh.calls, [])

    def test_rotated_log_restarts_offset(self):
        self.append("HTTP 403 failed")
        self.scan()
        self.log.rename(self.log.with_suffix(".old"))
        self.append("HTTP 503 failed")
        self.scan(now=100060)
        state = json.loads((self.state / "watch.json").read_text())
        self.assertEqual(len(state["signatures"]), 2)

    def test_comment_after_hour_when_count_grows(self):
        for _ in range(3):
            self.append("HTTP 503 failed")
        self.scan()
        self.append("HTTP 503 failed")
        self.scan(now=103601)
        self.assertEqual(len(self.gh.calls), 2)
        self.assertIn("4 total occurrences", self.gh.calls[1][2]["body"])

    def test_existing_open_issue_is_reused(self):
        sig = watch.signature("HTTP 403 failed")
        self.gh.open_issues = [{"number": 19, "title": "station: " + sig}]
        for _ in range(3):
            self.append("HTTP 403 failed")
        self.scan()
        self.assertEqual(self.gh.calls, [])
        state = json.loads((self.state / "watch.json").read_text())
        self.assertEqual(next(iter(state["signatures"].values()))["issue"], 19)


if __name__ == "__main__":
    unittest.main()
