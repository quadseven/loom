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

from bus import fabric, fabric_reviews, station, status, tickets


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
        self.gh._status_logins.return_value = {"example-station[bot]"}
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
        self.gh.paginate.return_value = [
            {"body": body, "user": {"login": "example-station[bot]"}}]
        self.gh.rest.reset_mock()
        self.run_check()
        self.assertEqual(self.api.call_count, 2)
        self.gh.rest.assert_not_called()

    def existing_incident(self, day="2026-11-09", state="open"):
        issue = {"number": 7, "state": state,
                 "user": {"login": "example-station[bot]"},
                 "body": fabric_reviews.MARKER % day + "\nOriginal incident"}
        self.gh.status_in_body = True
        self.gh._status_logins.return_value = {"example-station[bot]"}
        self.gh.paginate.side_effect = lambda path: [] if "/comments" in path else [issue]

        def rest(method, path, body=None, **kwargs):
            if method == "PATCH":
                self.assertEqual(path, "/repos/example-owner/project/issues/7")
                issue.update(body)
            return mock.Mock(data=dict(issue))

        self.gh.rest.side_effect = rest
        return issue

    def test_untrusted_issue_marker_is_not_reused_or_reported(self):
        for day in ("2026-11-09", "2026-11-10"):
            with self.subTest(day=day):
                self.state.clear()
                self.gh.rest.reset_mock()
                issue = self.existing_incident(day=day)
                issue["user"] = {"login": "untrusted-user"}
                before = issue["body"]
                self.api.return_value = page()
                self.run_check()
                self.gh.rest.assert_called_once()
                self.assertEqual(self.gh.rest.call_args.args[:2],
                                 ("POST", "/repos/example-owner/project/issues"))
                self.assertEqual(issue["body"], before)
                self.assertIn("Missing: Morning review", self.body())
                self.assertTrue(self.state["reported"])

    def test_untrusted_issue_cannot_mark_pending_day_reported_after_failed_post(self):
        issue = self.existing_incident(day="2026-11-10")
        issue["user"] = {"login": "untrusted-user"}
        self.state.update(day="2026-11-10", body="- Missing: Morning review.", reported=False)
        self.gh.rest.side_effect = station.GitHubError("unavailable")
        with self.assertRaises(station.GitHubError):
            self.run_check()
        self.assertFalse(self.state["reported"])
        self.api.assert_not_called()

    def test_issue_without_valid_author_cannot_suppress_report(self):
        for user in (None, "example-station[bot]", {}, {"login": None}):
            with self.subTest(user=user):
                self.state.clear()
                self.gh.rest.reset_mock()
                issue = self.existing_incident(day="2026-11-10")
                issue["user"] = user
                self.api.return_value = page()
                self.run_check()
                self.gh.rest.assert_called_once()
                self.assertEqual(self.gh.rest.call_args.args[:2],
                                 ("POST", "/repos/example-owner/project/issues"))

    def test_station_issue_author_comparison_is_case_insensitive(self):
        issue = self.existing_incident()
        issue["user"]["login"] = "Example-Station[bot]"
        self.api.return_value = page()
        self.run_check()
        self.assertEqual(len(status.body_events(issue["body"])), 1)
        self.assertFalse(any(call.args[0] == "POST" for call in self.gh.rest.call_args_list))

    def test_next_day_updates_open_incident_as_status_without_new_issue(self):
        issue = self.existing_incident()
        self.api.return_value = page()
        self.run_check()
        events = status.body_events(issue["body"])
        self.assertEqual(len(events), 1)
        self.assertIn(fabric_reviews.MARKER % "2026-11-10", events[0][1])
        self.assertIn("Morning review", events[0][1])
        self.assertIn("Done review", events[0][1])
        self.assertIn("Original incident", issue["body"])
        self.assertFalse(any(call.args[0] == "POST" for call in self.gh.rest.call_args_list))
        self.assertTrue(self.state["reported"])

    def test_later_day_opens_new_incident_after_previous_closed(self):
        self.existing_incident(state="closed")
        self.api.return_value = page()
        self.run_check()
        self.gh.rest.assert_called_once()
        self.assertEqual(self.gh.rest.call_args.args[:2],
                         ("POST", "/repos/example-owner/project/issues"))
        self.assertIn(fabric_reviews.MARKER % "2026-11-10", self.body())

    def test_healthy_day_does_not_update_open_incident(self):
        issue = self.existing_incident()
        before = issue["body"]
        self.api.return_value = page(note("Morning review"), note("Done review"))
        self.run_check()
        self.gh.rest.assert_not_called()
        self.assertEqual(issue["body"], before)
        self.assertEqual(status.body_events(issue["body"]), [])

    def test_failed_status_write_retries_without_repeating_search(self):
        issue = self.existing_incident()
        self.api.return_value = page()
        with mock.patch.object(status, "add", side_effect=station.GitHubError("unavailable")):
            with self.assertRaises(station.GitHubError):
                self.run_check()
        self.assertFalse(self.state["reported"])
        self.run_check()
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(len(status.body_events(issue["body"])), 1)
        self.assertTrue(self.state["reported"])

    def test_status_write_before_uncertain_save_is_not_duplicated(self):
        issue = self.existing_incident()
        self.api.return_value = page()
        self.run_check()
        self.state["reported"] = False  # The write landed but its durable save did not.
        self.gh.rest.reset_mock()
        self.run_check()
        self.gh.rest.assert_not_called()
        self.assertEqual(len(status.body_events(issue["body"])), 1)
        self.assertEqual(self.api.call_count, 2)

    def test_existing_status_comment_marker_prevents_duplicate_update(self):
        issue = self.existing_incident()
        recorded = {"user": {"login": "example-station[bot]"},
                    "body": status.render([("2026-11-10T19:01:00Z",
                                             fabric_reviews.MARKER % "2026-11-10")])}
        self.gh.paginate.side_effect = lambda path: [recorded] if "/comments" in path else [issue]
        self.api.return_value = page()
        self.run_check()
        self.gh.rest.assert_not_called()
        self.assertTrue(self.state["reported"])

    def test_uncertain_status_comment_then_close_does_not_refile_same_day(self):
        issue = self.existing_incident(state="closed")
        self.state.update(day="2026-11-10", body="- Missing: Morning review.", reported=False)
        recorded = {"user": {"login": "example-station[bot]"},
                    status.EXPANDED: True, "body": fabric_reviews.MARKER % "2026-11-10"}
        self.gh.paginate.side_effect = lambda path: [recorded] if "/comments" in path else [issue]
        self.run_check()
        self.api.assert_not_called()
        self.gh.rest.assert_not_called()
        self.assertTrue(self.state["reported"])

    def test_rewritten_open_incident_is_reused(self):
        issue = self.existing_incident()
        issue["body"] = tickets.with_original("## Why\nA narrowed repair ticket.", issue)
        self.api.return_value = page()
        self.run_check()
        self.assertEqual(len(status.body_events(issue["body"])), 1)
        self.assertFalse(any(call.args[0] == "POST" for call in self.gh.rest.call_args_list))

    def test_untrusted_comment_marker_cannot_suppress_daily_findings(self):
        issue = self.existing_incident()
        spoof = {"user": {"login": "untrusted-user"}, "body": status.render([
            ("2026-11-10T19:01:00Z", fabric_reviews.MARKER % "2026-11-10")])}
        self.gh.paginate.side_effect = lambda path: [spoof] if "/comments" in path else [issue]
        self.api.return_value = page()
        self.run_check()
        self.assertEqual(len(status.body_events(issue["body"])), 1)
        self.assertIn("Morning review", status.body_events(issue["body"])[0][1])

    def test_ordinary_trusted_comment_marker_does_not_claim_a_status_write(self):
        issue = self.existing_incident()
        ordinary = {"user": {"login": "example-station[bot]"},
                    "body": "Example marker: " + fabric_reviews.MARKER % "2026-11-10"}
        self.gh.paginate.side_effect = lambda path: [ordinary] if "/comments" in path else [issue]
        self.api.return_value = page()
        self.run_check()
        self.assertEqual(len(status.body_events(issue["body"])), 1)

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
