"""Read-only evidence capture-file source behavior."""
import json
import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lanes.evidence.serve import make_handler
from tests import sandbox_caps
from tests.local_http import LocalHTTPServer


class EvidenceCaptureFileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.capture_dir = root / "captures"
        self.capture_dir.mkdir(exist_ok=True)
        self.capture_file = self.capture_dir / "service.log"

        self.db = root / "evidence.sqlite"
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE runs (id INTEGER, state TEXT)")
            conn.execute("INSERT INTO runs VALUES (1, 'stalled')")

        self.config = {
            "logs": {},
            "queries": {"runs": {
                "database": str(self.db),
                "sql": "SELECT id, state FROM runs WHERE state = :state",
                "params": {"state": "str"},
            }},
            "captures": {
                "service": {
                    "command": [sys.executable, "-c", "print('empty')"],
                    "file": str(self.capture_file),
                }
            },
        }

        handler = make_handler(self.config, "test-bearer", logging.getLogger("evidence-capture-test"))
        self.server = LocalHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def get_json(self, path, token="test-bearer"):
        request = Request(self.base + path, headers={"Authorization": "Bearer " + token})
        with urlopen(request) as response:
            return response.status, json.loads(response.read())

    def test_capture_file_source_answers_match_and_since(self):
        self.capture_file.write_text("""[2025-01-01T10:00:00Z] event1
[2025-01-02T12:00:00Z] event2
[2025-01-03T14:00:00Z] event3
""", encoding="utf-8")

        _, result = self.get_json("/evidence/capture?command=%s&match=event2&since=24" % self.capture_file)

        self.assertEqual(result["lines"], ["[2025-01-02T12:00:00Z] event2"])
        self.assertEqual(result["dropped"], 0)

    def test_capture_file_source_ignores_lines_before_since(self):
        self.capture_file.write_text("""[2025-01-01T10:00:00Z] event1
[2025-01-02T12:00:00Z] event2
""", encoding="utf-8")

        _, result = self.get_json("/evidence/capture?command=%s&match=event&since=36" % self.capture_file)

        self.assertEqual(result["lines"], [])
        self.assertEqual(result["dropped"], 0)

    def test_capture_file_source_with_gap_marker(self):
        self.capture_file.write_text("""[2025-01-01T10:00:00Z] event1
[gap: 2025-01-02T12:00:00Z -> 2025-01-03T14:00:00Z]
[2025-01-03T14:00:00Z] event3
""", encoding="utf-8")

        _, result = self.get_json("/evidence/capture?command=%s&match=event&since=48" % self.capture_file)

        self.assertEqual(result["lines"], ["[2025-01-03T14:00:00Z] event3"])
        self.assertEqual(result["dropped"], 0)

    def test_capture_file_source_ignores_non_existent_source(self):
        with self.assertRaises(HTTPError) as caught:
            self.get_json("/evidence/capture?command=/nonexistent/file.log&match=test&since=1")

        self.assertEqual(caught.exception.code, 404)

    def test_capture_file_source_with_invalid_parameters(self):
        with self.assertRaises(HTTPError) as caught:
            self.get_json("/evidence/capture?command=%s&match=&since=1" % self.capture_file)

        self.assertEqual(caught.exception.code, 400)

    def test_capture_file_source_file_must_exist(self):
        with self.assertRaises(HTTPError) as caught:
            self.get_json("/evidence/capture?command=%s&match=test&since=1" % self.capture_file)

        self.assertEqual(caught.exception.code, 404)

    def test_capture_file_source_ignores_gap_lines(self):
        self.capture_file.write_text("""[gap: 2025-01-01T10:00:00Z -> 2025-01-01T12:00:00Z]
[2025-01-01T14:00:00Z] event1
""", encoding="utf-8")

        _, result = self.get_json("/evidence/capture?command=%s&match=event&since=24" % self.capture_file)

        self.assertEqual(result["lines"], [])
        self.assertEqual(result["dropped"], 0)

    def test_capture_file_source_filters_by_since_hours(self):
        self.capture_file.write_text("""[2025-01-01T10:00:00Z] event1
[2025-01-03T10:00:00Z] event2
""", encoding="utf-8")

        _, result1 = self.get_json("/evidence/capture?command=%s&match=event&since=24" % self.capture_file)
        self.assertEqual(len(result1["lines"]), 1)

        _, result2 = self.get_json("/evidence/capture?command=%s&match=event&since=72" % self.capture_file)
        self.assertEqual(len(result2["lines"]), 2)

    def test_capture_file_source_respects_since_hours_cap(self):
        self.capture_file.write_text("""[2025-01-01T10:00:00Z] event1
""", encoding="utf-8")

        _, result = self.get_json("/evidence/capture?command=%s&match=event&since=168" % self.capture_file)

        self.assertEqual(result["lines"], [])
        self.assertEqual(result["dropped"], 0)


if __name__ == "__main__":
    unittest.main()
