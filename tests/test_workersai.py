"""The request sizes that the Workers AI lane can serve."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from bus import workersai


class RequestSizeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.model = "cloudflare-workers-ai/@cf/example/model"

    def test_check_requires_a_served_coding_prompt_on_the_configured_model(self):
        for served in (66, 65536):
            response = {"choices": [{"message": {"content": "OK"}}],
                        "usage": {"prompt_tokens": served}}
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(
                    json.dumps(response).encode())) as fetch:
                rc = workersai.main(["check", str(self.root), self.model], io.StringIO("test\nexample\n"))
            payload = json.loads(fetch.call_args.args[0].data)
            self.assertEqual(payload["model"], "@cf/example/model")
            self.assertGreaterEqual(len(payload["messages"][0]["content"].split()), 65536)
            self.assertEqual(rc == 0, served == 65536)
            self.assertEqual(workersai.code_ready(self.root, self.model), served == 65536)
            self.assertFalse(workersai.code_ready(self.root, self.model + "-other"))

    def test_unreadable_refusal_record_holds_only_coding(self):
        usage = self.root / "state" / "usage"
        usage.mkdir(parents=True)
        (usage / "workersai-code.json").write_text(json.dumps({
            "model": self.model, "at": time.time(), "prompt_tokens": 65536}))
        (usage / "workersai.code-refused.json").write_text("{")
        self.assertFalse(workersai.code_ready(self.root, self.model))

    def test_first_request_429_with_unspent_daily_meter(self):
        events = self.root / "events"
        usage = self.root / "state" / "usage"
        usage.mkdir(parents=True)
        now = time.time()
        for pct, age, progress, expected in (
                (49.99, 0, "", True), (50, 0, "", False),
                (0, 86400, "", False), (0, 0, '{"type":"step_finish"}', False)):
            events.write_text(progress)
            (usage / "workersai.meter.json").write_text(json.dumps({
                "lane": "workersai", "at": now - age, "windows": [
                    {"window": "daily", "unit": "neurons", "used_pct": pct}]}))
            rc = workersai.main(["refused", str(self.root), str(events), "coding"],
                                io.StringIO("AI_APICallError: Too Many Requests"))
            self.assertEqual(rc == 0, expected)

    def test_short_verify_sends_only_the_bounded_prompt_and_returns_json(self):
        brief = self.root / "brief.md"
        result = self.root / "result.json"
        brief.write_text("Check the enclosed evidence.")
        response = {"choices": [{"message": {"content": '{"missing":"log line"}'}}]}
        usage = self.root / "state" / "usage"
        usage.mkdir(parents=True)
        (usage / "workersai.meter.json").write_text(json.dumps({
            "lane": "workersai", "at": time.time(), "windows": [
                {"window": "daily", "unit": "neurons", "used_pct": 10}]}))
        args = ["verify", str(brief), self.model, str(result), str(self.root)]
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(
                json.dumps(response).encode())) as fetch:
            self.assertEqual(workersai.main(args, io.StringIO("test\nexample\n")), 0)
            payload = json.loads(fetch.call_args.args[0].data)
            self.assertEqual(payload["max_tokens"], 1024)
            self.assertIn(brief.read_text(), payload["messages"][0]["content"])
            self.assertEqual(json.loads(result.read_text()), {"missing": "log line"})
            brief.write_text("x" * 8192)
            self.assertNotEqual(workersai.main(args, io.StringIO("test\nexample\n")), 0)
            self.assertEqual(fetch.call_count, 1)
