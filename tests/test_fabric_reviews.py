"""Expected Fabric outputs are checked without invoking a model (#1648)."""
import http.client
import inspect
import json
import socket
import tempfile
import unittest
import urllib.error
from datetime import datetime
from unittest import mock

from bus import fabric, fabric_reviews, station


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def config(*names):
    return json.dumps({"timezone": "America/New_York", "notes": [
        {"title_prefix": name, "start": "00:00", "by": "14:00"}
        for name in names]})


def page(*notes, **kwargs):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "resources": list(notes), "hasMore": False, "nextCursor": None,
        **kwargs}).encode()
    return response


def note(name, created="2026-11-10T18:00:00Z", kind="notepad"):
    return {"name": name, "createdAt": created, "kind": kind}


class FabricReviewsTest(unittest.TestCase):
    def setUp(self):
        self.gh = mock.Mock()
        self.gh.paginate.return_value = []
        self.state = {}
        self.saved = []
        # Patch the actual no-redirect transport. Fail any unintended call
        # through the default opener rather than reaching the network.
        self.api = self.enterContext(mock.patch("bus.fabric_reviews._OPENER.open"))
        self.enterContext(mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected transport")))
        self.enterContext(mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden in fake API tests")))
        self.enterContext(mock.patch("socket.socket.connect", side_effect=AssertionError("network forbidden in fake API tests")))
        self.secret = mock.Mock(return_value="fake-key")
        self.chat = self.enterContext(mock.patch.object(fabric, "ask"))
        self.enterContext(mock.patch("bus.fabric_reviews.shadow.active", return_value=False))

    def run_check(self, raw=None, now="2026-11-10T19:01:00Z"):
        fabric_reviews.run_pass(
            self.gh, "example-owner/project", raw or config("Morning review", "Done review"),
            "/example/fabric-key", "us-east-1", self.state, epoch(now),
            lambda: self.saved.append(json.loads(json.dumps(self.state))), self.secret)

    def body(self):
        return self.gh.rest.call_args.args[2]["body"]

    def test_missing_named_and_present_in_window_not_reported(self):
        self.api.side_effect = [page(), page(note("Done review 2026-11-10"))]
        self.run_check()
        self.assertIn("Morning review", self.body())
        self.assertNotIn("Done review", self.body())
        self.assertEqual(self.gh.rest.call_count, 1)
        self.assertIn("loom:factory", self.gh.rest.call_args.args[2]["labels"])

    def test_402_is_named_as_cause_not_proof_of_missing_output(self):
        self.api.side_effect = urllib.error.HTTPError(
            fabric_reviews.FILTER_URL, 402, "private upstream detail", {}, None)
        self.run_check()
        self.assertIn("402", self.body())
        self.assertIn("credit limit", self.body())
        self.assertIn("Morning review", self.body())
        self.assertIn("could not verify", self.body())
        self.assertNotIn("private upstream detail", self.body())

    def test_only_documented_metadata_read_no_chat_or_ai(self):
        self.api.return_value = page()
        self.run_check()
        self.assertEqual(self.api.call_count, 2)
        self.chat.assert_not_called()
        for call in self.api.call_args_list:
            request = call.args[0]
            self.assertEqual(request.full_url, "https://api.fabric.so/v2/resources/filter")
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.data)["kind"], ["notepad"])
            self.assertNotIn("fake-key", self.body())

    def test_before_latest_deadline_and_second_pass_make_no_search(self):
        self.api.return_value = page()
        self.run_check(now="2026-11-10T18:59:59Z")
        self.api.assert_not_called()
        self.run_check()
        self.run_check(now="2026-11-10T23:00:00Z")
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(self.gh.rest.call_count, 1)

    def test_stale_late_wrong_title_and_non_note_do_not_satisfy_window(self):
        self.api.return_value = page(
            note("Morning review old", "2026-11-10T04:59:59Z"),
            note("Morning review late", "2026-11-10T19:00:01Z"),
            note("Other Morning review"), note("Morning review folder", kind="folder"))
        self.run_check(config("Morning review"))
        self.assertIn("Missing", self.body())

    def test_follow_cursor_and_accept_deadline_boundary(self):
        self.api.side_effect = [page(hasMore=True, nextCursor="page-2"),
                                page(note("Morning review", "2026-11-10T19:00:00Z"))]
        self.run_check(config("Morning review"))
        self.gh.rest.assert_not_called()
        self.assertEqual(json.loads(self.api.call_args.args[0].data)["cursor"], "page-2")

    def test_incomplete_response_is_unknown_not_missing(self):
        self.api.return_value = page(hasMore=True)
        self.run_check(config("Morning review"))
        self.assertIn("could not verify", self.body())
        self.assertNotIn("Missing:", self.body())

    def test_github_retry_uses_saved_search_and_existing_daily_incident(self):
        self.api.return_value = page()
        self.gh.rest.side_effect = station.GitHubError("unavailable")
        with self.assertRaises(station.GitHubError):
            self.run_check()
        body = self.body()
        self.gh.paginate.return_value = [{"body": body}]
        self.gh.rest.reset_mock()
        self.run_check()
        self.assertEqual(self.api.call_count, 2)
        self.gh.rest.assert_not_called()

    def test_station_settings_and_step_integration(self):
        raw = config("Morning review")
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, ["example-owner/project"], [], [],
                                        self_repo="example-owner/project", fabric_key_ssm="/example/key",
                                        fabric_review_notes=raw)
            instance = station.Station(self.gh, settings, now=epoch("2026-11-10T19:01:00Z"),
                                       read_secret=self.secret)
            self.api.return_value = page()
            instance.check_fabric_reviews()
            self.assertIn("Morning review", self.body())
        with mock.patch.object(fabric_reviews, "run_pass") as run:
            instance.check_fabric_reviews()
            run.assert_called_once()

    def test_fake_api_tests_forbid_network_below_any_opener(self):
        with socket.socket() as connection:
            with self.assertRaisesRegex(AssertionError, "network forbidden"):
                connection.connect(("192.0.2.1", 443))

    def test_weekly_schedule_and_summer_eastern_deadline(self):
        raw = json.loads(config("Weekly review"))
        raw["notes"][0]["days"] = [1]  # Tuesday
        self.api.return_value = page()
        self.run_check(json.dumps(raw), now="2026-07-06T19:00:00Z")
        self.run_check(json.dumps(raw), now="2026-07-07T17:59:59Z")
        self.api.assert_not_called()
        self.run_check(json.dumps(raw), now="2026-07-07T18:00:00Z")
        payload = json.loads(self.api.call_args.args[0].data)
        self.assertEqual(payload["createdAfter"], "2026-07-07T03:59:59Z")
        self.assertEqual(payload["createdBefore"], "2026-07-07T18:00:01Z")

    def test_waits_for_all_windows_then_groups_every_missing_note(self):
        raw = json.loads(config("Morning review", "Evening review"))
        raw["notes"][1]["by"] = "18:00"
        self.api.return_value = page()
        self.run_check(json.dumps(raw))
        self.api.assert_not_called()
        self.secret.assert_not_called()
        self.assertEqual(self.state, {})
        self.run_check(json.dumps(raw), now="2026-11-10T23:00:00Z")
        self.assertIn("Morning review", self.body())
        self.assertIn("Evening review", self.body())
        self.assertEqual(self.gh.rest.call_count, 1)

    def test_spring_nonexistent_window_stops_before_any_request(self):
        raw = json.loads(config("Morning review"))
        raw["notes"][0].update(start="02:30", by="03:15")
        with self.assertRaisesRegex(ValueError, "nonexistent local time"):
            self.run_check(json.dumps(raw), now="2026-03-08T08:00:00Z")
        self.api.assert_not_called()
        self.secret.assert_not_called()
        self.gh.rest.assert_not_called()

    def test_autumn_repeated_hour_uses_first_occurrence(self):
        raw = json.loads(config("Morning review"))
        raw["notes"][0].update(start="00:00", by="01:30")
        self.api.return_value = page()
        self.run_check(json.dumps(raw), now="2026-11-01T06:15:00Z")
        payload = json.loads(self.api.call_args.args[0].data)
        self.assertEqual(payload["createdBefore"], "2026-11-01T05:30:01Z")

    def test_valid_spring_window_widens_bounds_in_utc(self):
        raw = json.loads(config("Morning review"))
        raw["notes"][0].update(start="03:00", by="04:00")
        self.api.return_value = page(note("Morning review", "2026-03-08T07:30:00Z"))
        self.run_check(json.dumps(raw), now="2026-03-08T08:00:00Z")
        payload = json.loads(self.api.call_args.args[0].data)
        self.assertEqual(payload["createdAfter"], "2026-03-08T06:59:59Z")
        self.assertEqual(payload["createdBefore"], "2026-03-08T08:00:01Z")
        self.gh.rest.assert_not_called()

    def test_shadow_holds_pending_incident_without_repeating_search(self):
        self.api.return_value = page()
        with mock.patch("bus.fabric_reviews.shadow.active", return_value=True):
            self.run_check()
            self.run_check()
        self.gh.rest.assert_not_called()
        self.run_check()
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(self.gh.rest.call_count, 1)

    def test_interrupted_check_does_not_repeat_fabric_requests(self):
        self.api.side_effect = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.run_check()
        self.run_check()
        self.assertEqual(self.api.call_count, 1)
        self.assertIn("interrupted", self.body())
        self.assertTrue(self.saved[0]["body"])

    def test_disabled_config_and_invalid_config_make_no_requests(self):
        fabric_reviews.run_pass(self.gh, "example-owner/project", "", "", "us-east-1",
                                self.state, 0, mock.Mock(), self.secret)
        for raw in ["{}", "[]", "invalid", config("Bad\nheading")]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.run_check(raw)
        self.api.assert_not_called()
        self.secret.assert_not_called()
        self.gh.rest.assert_not_called()

    def test_malformed_and_unbounded_pages_are_not_missing(self):
        for result in [page(note("Morning review", "bad-time")),
                       page(hasMore=True, nextCursor="loop")]:
            self.state.clear()
            self.api.return_value = result
            self.run_check(config("Morning review"))
            self.assertIn("could not verify", self.body())
            self.assertNotIn("Missing:", self.body())

    def test_missing_or_wrong_timestamp_types_are_unknown(self):
        for value in [None, 1, [], {}, "2026-11-10T12:00:00"]:
            with self.subTest(value=value):
                self.state.clear()
                self.api.return_value = page(note("Morning review", value))
                self.run_check(config("Morning review"))
                self.assertIn("could not verify", self.body())
                self.assertNotIn("Missing:", self.body())

    def test_truncated_http_response_is_unknown_and_does_not_escape(self):
        self.api.side_effect = http.client.IncompleteRead(b"private response fragment")
        self.run_check(config("Morning review"))
        self.assertIn("could not verify", self.body())
        self.assertNotIn("private response fragment", self.body())

    def test_settings_reads_config_and_run_steps_calls_check(self):
        raw = config("Morning review")
        settings = station.Settings.from_env({"LOOM_HOME": "/example/home",
                                              "LOOM_FABRIC_REVIEW_NOTES": raw})
        self.assertEqual(settings.fabric_review_notes, raw)
        self.assertIn("self.check_fabric_reviews", inspect.getsource(station.Station._run_steps))


if __name__ == "__main__":
    unittest.main()
