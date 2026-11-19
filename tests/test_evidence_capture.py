"""Rolling log captures through the station and evidence readers."""
import json
import logging
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from bus import evidence, station
from bus.github import GitHub
from lanes.evidence.serve import make_handler
from tests import sandbox_caps
from tests.local_http import LocalHTTPServer


class CaptureTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.capture = self.home / "logs" / "capture" / "service.log"
        self.settings = station.Settings.from_env({
            "LOOM_HOME": str(self.home),
            "LOOM_LOG_CAPTURE": json.dumps([
                {"source": "service", "namespace": "example", "selector": "app=service"},
            ]),
            "LOOM_LOG_CAPTURE_RETENTION_HOURS": "2",
            "LOOM_EVIDENCE_CMDS": '{"example-owner/example-repo":"capture:service"}',
        })
        self.uid = "pod-1"
        self.log = []
        self.log_calls = []
        self.fail_logs = False

    def kubectl(self, command, **kwargs):
        self.assertEqual(command[:3], ["kubectl", "-n", "example"])
        if "get" in command:
            self.assertIn("app=service", command)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"items": [{
                "metadata": {"name": "service-0", "uid": self.uid},
                "spec": {"containers": [{"name": "service"}]},
            }]}))
        self.assertIn("pod/service-0", command)
        self.assertIn("--timestamps", command)
        self.log_calls.append(command)
        kwargs["stdout"].write("".join(line + "\n" for line in self.log))
        if self.fail_logs:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    def tick(self, stamp):
        now = evidence.line_time(stamp)
        gh = GitHub("test-token")
        gh.limited_until = int(now) + 3600
        with patch("subprocess.run", side_effect=self.kubectl):
            station.Station(gh, self.settings, now=now).run_pass()

    def test_two_station_passes_deduplicate_and_trim(self):
        old = "2030-01-02T01:30:00Z event old"
        anchor = "2030-01-02T02:59:00.123456789Z event retained"
        fresh = "2030-01-02T02:59:00.123456789Z event added at the same timestamp"
        newest = "2030-01-02T03:59:00Z event newest"
        self.log = [old, anchor, anchor]
        self.tick("2030-01-02T03:00:00Z")
        self.log = [anchor, anchor, fresh, newest]
        self.tick("2030-01-02T04:00:00Z")
        self.assertEqual(len(self.log_calls), 2)
        self.assertIn("--since-time=2030-01-02T02:59:00.123456789Z", self.log_calls[1])
        self.assertEqual([line for line in self.capture.read_text().splitlines()
                          if " event " in line], [anchor, anchor, fresh, newest])

    @unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
    def test_http_reads_older_matches_from_capture(self):
        anchor = "2030-01-02T02:00:00Z starting"
        older = "2030-01-02T02:20:00Z route ready"
        self.log = [anchor, older]
        self.tick("2030-01-02T02:25:00Z")
        self.log = ["2030-01-02T03:55:00Z route ready", "2030-01-02T03:56:00Z other"]
        self.tick("2030-01-02T04:00:00Z")
        config = {"logs": {"service": {"capture": "service"}}, "queries": {}}
        handler = make_handler(config, "test-bearer", logging.getLogger("capture-test"))
        with LocalHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:%d/evidence/logs?source=service&match=route&since=" % server.server_port
                now = evidence.line_time("2030-01-02T04:00:00Z")
                with patch.dict("os.environ", {"LOOM_HOME": str(self.home)}), patch("time.time", return_value=now):
                    answers = []
                    for hours in (2, 1):
                        request = Request(base + str(hours), headers={"Authorization": "Bearer test-bearer"})
                        with urlopen(request, timeout=10) as response:
                            answers.append(json.load(response))
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(answers[0]["lines"], [older, self.log[0]])
        self.assertEqual(answers[1]["lines"], [self.log[0]])
        self.assertFalse(answers[0]["covered"])

    def test_replacement_records_one_gap_and_absence_requires_coverage(self):
        before = "2030-01-02T02:15:00Z"
        after = "2030-01-02T03:20:00Z"
        self.log = [before + " last old record"]
        self.tick("2030-01-02T02:16:00Z")
        self.uid = "pod-2"
        self.log = [after + " first new record"]
        self.tick("2030-01-02T03:21:00Z")
        self.tick("2030-01-02T03:22:00Z")

        lines = self.capture.read_text().splitlines()
        self.assertEqual([line for line in lines if line.startswith("loom-log-gap:")],
                         ["loom-log-gap: " + before + " " + after])
        self.assertIn(before + " last old record", lines)
        self.assertIn(after + " first new record", lines)
        self.assertFalse(any(arg.startswith("--since-time") for arg in self.log_calls[1]))
        now = evidence.line_time("2030-01-02T03:22:00Z")
        self.assertIsNone(evidence.lines_since(self.capture, "refusal", evidence.line_time(before), now, 2))
        self.assertEqual(evidence.lines_since(self.capture, "refusal", evidence.line_time(after), now, 2), [])
        self.assertIsNone(evidence.lines_since(self.capture, "refusal", evidence.line_time(after), now + 1, 2))
        source = station.Station(GitHub("test-token"), self.settings, now=now)
        proof_log = source._proof_sources("example-owner/example-repo").log
        self.assertIsNone(proof_log("refusal", evidence.line_time(before)))
        _, seen, hours = evidence.gather_window(
            self.settings.evidence_cmds, "example-owner/example-repo",
            "No `read refused` for 1 hour.", now)
        self.assertFalse(seen)
        self.assertAlmostEqual(hours, 2 / 60)

    def test_rotation_at_cursor_timestamp_does_not_hide_the_new_record(self):
        stamp = "2030-01-02T02:15:00.000000001Z"
        old = stamp + " final old record"
        new = stamp + " first surviving record"
        self.log = [old]
        self.tick("2030-01-02T02:16:00Z")
        self.log = [new]
        self.tick("2030-01-02T02:19:00Z")
        self.log = [new, "2030-01-02T02:19:30Z later record"]
        self.tick("2030-01-02T02:20:00Z")
        lines = self.capture.read_text().splitlines()
        self.assertEqual(lines.count("loom-log-gap: " + stamp + " " + stamp), 1)
        self.assertEqual(lines.count(new), 1)

    def test_failed_read_preserves_capture_and_coverage(self):
        self.log = ["2030-01-02T02:15:00Z original record"]
        self.tick("2030-01-02T02:16:00Z")
        previous = self.capture.read_bytes()
        self.log = ["2030-01-02T02:17:00Z partial response"]
        self.fail_logs = True
        self.tick("2030-01-02T02:18:00Z")
        self.assertEqual(self.capture.read_bytes(), previous)
        self.assertIsNone(evidence.lines_since(
            self.capture, "refusal", evidence.line_time("2030-01-02T02:15:00Z"),
            evidence.line_time("2030-01-02T02:18:00Z"), 2))
