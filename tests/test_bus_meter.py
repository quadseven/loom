"""bus/meter.py: the free lanes' provider meters, read from recorded answers.
No test reaches a provider: every read goes through a fake fetch."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from bus import meter, usage

# 2026-10-18T12:00:00Z
NOW = 1792324800.0
MIDNIGHT = "2026-10-19T00:00:00+00:00"

# A GET /api/v1/key answer, in the shape OpenRouter documents (data.*).
OPENROUTER_KEY = {"data": {
    "label": "sk-or-v1-abc...xyz", "limit": 5.0, "limit_remaining": 3.75, "limit_reset": None,
    "usage": 1.25, "usage_daily": 0.0, "usage_weekly": 0.5, "usage_monthly": 1.25,
    "is_free_tier": False, "include_byok_in_limit": False,
    "free_model_daily_requests": {"used": 120, "limit": 1000, "remaining": 880}}}

# A GraphQL Analytics answer for aiInferenceAdaptiveGroups.
NEURONS = {"data": {"viewer": {"accounts": [
    {"aiInferenceAdaptiveGroups": [{"sum": {"totalNeurons": 7400.5}}]}]}}, "errors": None}

NOT_AUTHORIZED = {"data": None, "errors": [
    {"message": "not authorized for that account", "path": ["viewer", "accounts"],
     "extensions": {"code": "authz", "timestamp": "2026-10-18T12:00:00Z"}}]}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeFetch:
    """Answers each request with BODY (a dict, raw bytes, or an exception to
    raise) and records the requests."""

    def __init__(self, body):
        self.body, self.requests = body, []

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.body, Exception):
            raise self.body
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return FakeResponse(raw)


class MeterReadTests(unittest.TestCase):
    def test_openrouter_key_answer_gives_free_requests_today_and_credit(self):
        fetch = FakeFetch(OPENROUTER_KEY)
        record = meter.read("openrouter", "sk-or-test", "", NOW, {}, fetch=fetch)
        self.assertIsNone(record["error"])
        self.assertEqual(fetch.requests[0].full_url, meter.OPENROUTER_URL)
        self.assertEqual(fetch.requests[0].get_method(), "GET")
        self.assertEqual(fetch.requests[0].get_header("Authorization"), "Bearer sk-or-test")
        daily, credit = record["windows"]
        self.assertEqual((daily["window"], daily["used"], daily["limit"], daily["remaining"]),
                         ("daily", 120.0, 1000.0, 880.0))
        self.assertEqual((daily["used_pct"], daily["unit"], daily["resets_at"]), (12.0, "requests", MIDNIGHT))
        self.assertEqual((credit["window"], credit["used"], credit["remaining"], credit["used_pct"]),
                         ("credit", 1.25, 3.75, 25.0))

    def test_openrouter_key_without_a_credit_limit_gives_only_the_daily_requests(self):
        body = {"data": dict(OPENROUTER_KEY["data"], limit=None, limit_remaining=None)}
        record = meter.read("openrouter", "k", "", NOW, {}, fetch=FakeFetch(body))
        self.assertEqual([w["window"] for w in record["windows"]], ["daily"])

    def test_openrouter_answer_without_a_meter_says_why(self):
        body = {"data": {"label": "x", "limit": None}}
        record = meter.read("openrouter", "k", "", NOW, {}, fetch=FakeFetch(body))
        self.assertEqual(record["windows"], [])
        self.assertIn("no free-model request count", record["error"])

    def test_a_refused_key_says_so(self):
        refused = urllib.error.HTTPError(meter.OPENROUTER_URL, 401, "Unauthorized", {}, None)
        record = meter.read("openrouter", "k", "", NOW, {}, fetch=FakeFetch(refused))
        self.assertEqual(record["error"], "the provider refused the key (HTTP 401)")

    def test_no_answer_and_non_json_are_reported(self):
        record = meter.read("openrouter", "k", "", NOW, {}, fetch=FakeFetch(urllib.error.URLError("down")))
        self.assertEqual(record["error"], "no answer from the provider (URLError)")
        record = meter.read("openrouter", "k", "", NOW, {}, fetch=FakeFetch(b"<html>"))
        self.assertEqual(record["error"], "the provider answered with something other than JSON")

    def test_workersai_neurons_today_against_the_free_allocation(self):
        fetch = FakeFetch(NEURONS)
        record = meter.read("workersai", "cf-token", "acct0123", NOW, {}, fetch=fetch)
        self.assertIsNone(record["error"])
        request = fetch.requests[0]
        self.assertEqual(request.full_url, meter.CLOUDFLARE_GRAPHQL_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer cf-token")
        sent = json.loads(request.data)
        self.assertIn("aiInferenceAdaptiveGroups", sent["query"])
        self.assertIn("totalNeurons", sent["query"])
        self.assertEqual(sent["variables"], {"account": "acct0123", "date": "2026-10-18"})
        [daily] = record["windows"]
        self.assertEqual((daily["used"], daily["limit"], daily["remaining"], daily["unit"], daily["resets_at"]),
                         (7400.5, 10000.0, 2599.5, "neurons", MIDNIGHT))
        self.assertAlmostEqual(daily["used_pct"], 74.005)

    def test_workersai_allowance_can_be_configured(self):
        record = meter.read("workersai", "t", "acct", NOW, {"LOOM_WORKERSAI_DAILY_NEURONS": "20000"},
                            fetch=FakeFetch(NEURONS))
        self.assertEqual(record["windows"][0]["limit"], 20000.0)

    def test_workersai_with_no_inference_today_reads_zero(self):
        body = {"data": {"viewer": {"accounts": [{"aiInferenceAdaptiveGroups": []}]}}, "errors": None}
        record = meter.read("workersai", "t", "acct", NOW, {}, fetch=FakeFetch(body))
        self.assertEqual((record["windows"][0]["used"], record["windows"][0]["used_pct"]), (0.0, 0.0))

    def test_workersai_token_without_analytics_read_says_which_permission(self):
        record = meter.read("workersai", "t", "acct", NOW, {}, fetch=FakeFetch(NOT_AUTHORIZED))
        self.assertEqual(record["windows"], [])
        self.assertIn("Account Analytics read", record["error"])

    def test_workersai_without_an_account_is_not_sent(self):
        fetch = FakeFetch(NEURONS)
        record = meter.read("workersai", "t", "", NOW, {}, fetch=fetch)
        self.assertEqual(fetch.requests, [])
        self.assertEqual(record["error"], "the lane has no Cloudflare account id")

    def test_the_key_never_reaches_the_record(self):
        body = {"data": None, "errors": [{"message": "bad token sk-secret-1"}]}
        record = meter.read("workersai", "sk-secret-1", "acct", NOW, {}, fetch=FakeFetch(body))
        self.assertNotIn("sk-secret-1", json.dumps(record))


class MeterMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = str(Path(self.tmp.name) / "state" / "usage" / "openrouter.meter.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_comes_from_stdin_and_the_file_is_written(self):
        fetch = FakeFetch(OPENROUTER_KEY)
        code = meter.main(["openrouter", self.out], stdin=io.StringIO("sk-or-stdin\n\n"), now=NOW, fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(fetch.requests[0].get_header("Authorization"), "Bearer sk-or-stdin")
        written = json.loads(Path(self.out).read_text())
        self.assertEqual(written["at"], NOW)
        self.assertNotIn("sk-or-stdin", Path(self.out).read_text())

    def test_a_url_argument_replaces_the_provider_endpoint(self):
        fetch = FakeFetch(OPENROUTER_KEY)
        meter.main(["openrouter", self.out, "http://127.0.0.1:1/api/v1/key"], stdin=io.StringIO("k\n"),
                   now=NOW, fetch=fetch)
        self.assertEqual(fetch.requests[0].full_url, "http://127.0.0.1:1/api/v1/key")

    def test_a_fresh_reading_is_not_read_again(self):
        meter.main(["openrouter", self.out], stdin=io.StringIO("k\n"), now=NOW, fetch=FakeFetch(OPENROUTER_KEY))
        again = FakeFetch(OPENROUTER_KEY)
        meter.main(["openrouter", self.out], stdin=io.StringIO("k\n"), now=NOW + 60, fetch=again)
        self.assertEqual(again.requests, [])
        meter.main(["openrouter", self.out], stdin=io.StringIO("k\n"), now=NOW + meter.METER_EVERY, fetch=again)
        self.assertEqual(len(again.requests), 1)


class UsageMeterTests(unittest.TestCase):
    """bus/usage.py turns a lane's meter file into Usage readings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.usage_dir = Path(self.home) / "state" / "usage"
        self.usage_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_meter(self, lane, record):
        meter.write(str(self.usage_dir / (lane + ".meter.json")), record)

    def test_openrouter_meter_becomes_a_daily_and_a_credit_reading(self):
        self.write_meter("openrouter", meter.read("openrouter", "k", "", NOW, {}, fetch=FakeFetch(OPENROUTER_KEY)))
        rows = usage.collect(self.home, now=NOW + 30, lane_names=["openrouter"])
        by_window = {row["window"]: row for row in rows}
        self.assertEqual(sorted(by_window), ["credit", "daily"])
        daily = by_window["daily"]
        self.assertEqual((daily["used_pct"], daily["remaining"], daily["unit"], daily["resets_at"]),
                         (12.0, 880.0, "requests", MIDNIGHT))
        self.assertEqual(daily["window_minutes"], 1440)
        self.assertEqual(daily["observed_at"], "2026-10-18T12:00:00+00:00")
        self.assertEqual(daily["source"], "OpenRouter GET /api/v1/key")
        self.assertEqual(daily["note"], "120 of 1000 requests used")
        self.assertEqual((by_window["credit"]["remaining"], by_window["credit"]["unit"]), (3.75, "USD"))

    def test_workersai_meter_becomes_a_daily_neurons_reading(self):
        self.write_meter("workersai", meter.read("workersai", "t", "acct", NOW, {}, fetch=FakeFetch(NEURONS)))
        [row] = usage.collect(self.home, now=NOW, lane_names=["workersai"])
        self.assertEqual((row["window"], row["unit"], row["resets_at"]), ("daily", "neurons", MIDNIGHT))
        self.assertAlmostEqual(row["used_pct"], 74.005)
        self.assertEqual(row["note"], "7400.50 of 10000 neurons used")

    def test_a_failed_meter_read_says_why(self):
        self.write_meter("workersai", meter.read("workersai", "t", "acct", NOW, {},
                                                 fetch=FakeFetch(NOT_AUTHORIZED)))
        [row] = usage.collect(self.home, now=NOW, lane_names=["workersai"])
        self.assertIsNone(row["used_pct"])
        self.assertTrue(row["note"].startswith("meter read failed: the token cannot read account analytics"))

    def test_a_metered_lane_with_no_reading_yet_says_so(self):
        [row] = usage.collect(self.home, now=NOW, lane_names=["openrouter"])
        self.assertIsNone(row["used_pct"])
        self.assertEqual(row["note"], "no meter reading yet: the lane reads it with its key on its next pass")

    def test_providers_with_no_meter_say_the_provider_exposes_none(self):
        rows = usage.collect(self.home, now=NOW, lane_names=["zai", "zen"])
        for row in rows:
            self.assertIsNone(row["used_pct"])
            self.assertTrue(row["note"].startswith("no meter: provider exposes no usage"), row["note"])
        notes = {row["provider"]: row["note"] for row in rows}
        self.assertIn("GLM Coding Plan", notes["zai"])
        self.assertIn("without a key", notes["zen"])


if __name__ == "__main__":
    unittest.main()
