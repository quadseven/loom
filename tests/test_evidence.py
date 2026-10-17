"""Read-only evidence endpoint behavior."""
import json
import logging
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lanes.evidence.serve import _capped_payload, _safe_items, make_handler
from tests import sandbox_caps


class EvidenceTextSafetyTest(unittest.TestCase):
    def test_private_host_and_credential_lines_are_dropped_and_counted(self):
        safe, dropped = _safe_items([
            "ordinary evidence", "service.internal failed", "Bearer abcdefghijklmnop"])
        self.assertEqual(safe, ["ordinary evidence"])
        self.assertEqual(dropped, 2)

    def test_item_cap_counts_omitted_lines(self):
        payload = _capped_payload("lines", ["event"] * 201, 0)
        self.assertEqual(len(payload["lines"]), 200)
        self.assertEqual(payload["dropped"], 1)


@unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
class EvidenceEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.log = root / "service.log"
        self.log.write_text("ready\nfailed safely\n", encoding="utf-8")
        self.db = root / "evidence.sqlite"
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE runs (id INTEGER, state TEXT)")
            conn.execute("INSERT INTO runs VALUES (1, 'stalled')")
        self.config = {
            "logs": {"service": {"command": [
                sys.executable, "-c",
                "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text(), end='')",
                str(self.log), "{since_hours}",
            ]}},
            "queries": {"runs": {
                "database": str(self.db),
                "sql": "SELECT id, state FROM runs WHERE state = :state",
                "params": {"state": "str"},
            }},
        }
        handler = make_handler(self.config, "test-bearer", logging.getLogger("evidence-test"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    # The default is a test-only fixture matching the handler's bearer value.
    def get_json(self, path, token="test-bearer"):
        request = Request(self.base + path, headers={"Authorization": "Bearer " + token})  # noqa: S310 - URL uses this test's local server.
        with urlopen(request) as response:  # noqa: S310 - URL uses this test's local server.
            return response.status, json.loads(response.read())

    def test_named_log_source_and_query_return_capped_evidence(self):
        _, logs = self.get_json("/evidence/logs?source=service&match=failed&since=1")
        self.assertEqual(logs["lines"], ["failed safely"])
        _, rows = self.get_json("/evidence/query?name=runs&state=stalled")
        self.assertEqual(rows["rows"], [[1, "stalled"]])
        self.assertLessEqual(len(json.dumps(rows).encode()), 65536)

    def test_answers_are_limited_to_200_items_and_64_kibibytes(self):
        self.log.write_text("event\n" * 250, encoding="utf-8")
        _, result = self.get_json("/evidence/logs?source=service&match=event&since=1")
        self.assertEqual(len(result["lines"]), 200)
        self.assertEqual(result["dropped"], 50)
        self.assertLessEqual(len(json.dumps(result).encode()), 65536)
        self.log.write_text(("x" * 10000 + "\n") * 10, encoding="utf-8")
        _, result = self.get_json("/evidence/logs?source=service&match=x&since=1")
        self.assertLessEqual(len(json.dumps(result).encode()), 65536)
        self.assertGreater(result["dropped"], 0)

    def test_unnamed_sources_and_queries_are_refused(self):
        for path in ("/evidence/logs?source=unknown&match=x&since=1",
                     "/evidence/query?name=unknown"):
            with self.assertRaises(HTTPError) as caught:
                self.get_json(path)
            self.assertEqual(caught.exception.code, 404)

    def test_metacharacters_and_credential_lines_are_rejected_or_dropped(self):
        with self.assertRaises(HTTPError) as caught:
            self.get_json("/evidence/query?name=runs&state=x%3B%20DROP%20TABLE%20runs")
        self.assertEqual(caught.exception.code, 400)
        self.log.write_text("Bearer abcdefghijklmnop evidence\nordinary evidence\n", encoding="utf-8")
        _, result = self.get_json("/evidence/logs?source=service&match=evidence&since=1")
        self.assertEqual(result["lines"], ["ordinary evidence"])
        self.assertEqual(result["dropped"], 1)

    def test_wrong_token_unknown_route_and_bad_parameters_are_refused(self):
        cases = (("/evidence/logs?source=service&match=x&since=1", "wrong-bearer", 401),
                 ("/evidence/other", "test-bearer", 404),
                 ("/evidence/logs?source=service&match=x&since=999", "test-bearer", 400),
                 ("/evidence/query?name=runs", "test-bearer", 400))
        for path, token, code in cases:
            with self.assertRaises(HTTPError) as caught:
                self.get_json(path, token)
            self.assertEqual(caught.exception.code, code, path)

    def test_bearer_is_required_and_each_call_is_logged(self):
        calls = []
        handler = make_handler(self.config, "test-bearer", logging.getLogger("evidence-audit"),
                               audit_logger=lambda *args: calls.append(args))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/evidence/query?name=runs&state=stalled" % server.server_port
            with self.assertRaises(HTTPError) as caught:
                urlopen(url)  # noqa: S310 - URL uses this test's local server.
            self.assertEqual(caught.exception.code, 401)
            request = Request(url, headers={"Authorization": "Bearer test-bearer"})  # noqa: S310 - URL uses this test's local server.
            with urlopen(request) as response:  # noqa: S310 - URL uses this test's local server.
                response.read()
            self.assertEqual(len(calls), 2)
            self.assertEqual([call[0] for call in calls], ["unknown", "query"])
            self.assertEqual(calls[0][1], "<invalid>")
            self.assertEqual(calls[0][2], {})
            self.assertEqual(calls[1][1], "runs")
            self.assertEqual(calls[1][2], {"state": "stalled"})
            self.assertGreater(calls[0][3], 0)
            self.assertGreater(calls[1][3], 0)
        finally:
            server.shutdown()
            server.server_close()
