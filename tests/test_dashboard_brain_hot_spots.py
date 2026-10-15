"""Fixture-backed tests for the /brain and /hot-spots views."""
import unittest
from datetime import datetime, timezone

from dashboard import brain_view, hot_spots

NOW = datetime(2030, 1, 8, 12, 0, tzinfo=timezone.utc)
DEFER = ("<!-- loom:defer v1 stage=merge item=example-owner/sample#40 "
         "rev=abc123 holder=brain-a -->")
DONE = "<!-- loom:done v1 stage=merge item=example-owner/sample#41 holder=brain-a -->"
JOURNAL = {"created_at": "2030-01-08T11:00:00Z", "body": "\n".join([
    "<!-- loom:journal v1 -->",
    "Tick 2030-01-08T11:00:00Z, brain-a",
    "- Shipped: none",
    "- Briefed: example-owner/sample#42 to codex",
    "- Waiting: example-owner/sample#41 on review",
    "- Asks: none",
    "- Next: merge example-owner/sample#41 once its review is clean",
    "- Cost: 40 requests, 6 minutes",
    DEFER, DONE])}
OLDER = {"created_at": "2030-01-08T10:30:00Z", "body": "\n".join([
    "<!-- loom:journal v1 -->",
    "Tick 2030-01-08T10:30:00Z, brain-b",
    "- Shipped: none",
    "- Step merge: failed the API timed out",
    "- Cost: 12 requests, 2 minutes"])}
RECORD = {"holder": "brain-a", "started_at": "2030-01-08T11:30:00Z", "step": "merge",
          "step_index": 3, "steps": list(brain_view.STEPS), "notes": [
              {"ts": "2030-01-08T11:31:00Z", "step": "status",
               "text": "example-owner/sample#7 regressed the p95 after it shipped"},
              {"ts": "2030-01-08T11:33:00Z", "step": "merge",
               "text": "merged example-owner/sample#41 on its ready head"}]}


def outcome(lane, text, at="2030-01-07T10:00:00Z", number=5):
    return {"body": "Lane %s %s" % (lane, text), "created_at": at,
            "issue_url": "https://api.github.com/repos/example-owner/sample/issues/%d" % number,
            "repo": "example-owner/sample", "user": {"login": "station-bot"}}


class BrainViewTest(unittest.TestCase):
    def test_current_step_is_marked_and_markers_are_verbatim(self):
        body = brain_view.render_brain(brain_view.build_brain(RECORD, "", [OLDER, JOURNAL]))
        self.assertIn('<li class="tick-step current" aria-current="step">'
                      '<span>3</span>merge <small>current</small></li>', body)
        self.assertIn('<li class="tick-step done"><span>1</span>lock <small>done</small></li>', body)
        self.assertIn('<li class="tick-step ahead"><span>4</span>verify <small>ahead</small></li>', body)
        self.assertIn("&lt;!-- loom:defer v1 stage=merge item=example-owner/sample#40 "
                      "rev=abc123 holder=brain-a --&gt;", body)
        self.assertIn("&lt;!-- loom:done v1 stage=merge item=example-owner/sample#41 "
                      "holder=brain-a --&gt;", body)
        self.assertIn("merge example-owner/sample#41 once its review is clean", body)

    def test_thoughts_are_tagged_from_their_text(self):
        data = brain_view.build_brain(RECORD, "", [JOURNAL])
        self.assertEqual([t["tag"] for t in data["thoughts"]], ["regression", "merged"])

    def test_recent_ticks_name_holder_time_outcome_and_one_line(self):
        ticks = brain_view.build_brain(RECORD, "", [OLDER, JOURNAL])["ticks"]
        self.assertEqual([t["holder"] for t in ticks], ["brain-a", "brain-b"])
        self.assertEqual(ticks[0]["outcome"], "ok")
        self.assertEqual(ticks[0]["line"], "Briefed: example-owner/sample#42 to codex")
        self.assertEqual(ticks[1]["outcome"], "failed")
        self.assertEqual(ticks[1]["line"], "Step merge: failed the API timed out")

    def test_no_step_record_says_not_recorded_not_step_one(self):
        empty = {"holder": None, "started_at": None, "step": None, "step_index": None,
                 "steps": list(brain_view.STEPS), "notes": []}
        for record, error in ((empty, ""), (None, "LOOM_STATUS_REPO is not set")):
            body = brain_view.render_brain(brain_view.build_brain(record, error, []))
            self.assertIn("Tick progress not recorded", body)
            self.assertNotIn("current", body)
            self.assertNotIn("step 1", body.lower())
        self.assertIn("LOOM_STATUS_REPO is not set", body)

    def test_ranking_states_its_arithmetic_in_words(self):
        rows = [{"state": "pr", "median_h": 2.5, "n": 4},
                {"state": "queued", "median_h": 1.0, "n": 3},
                {"state": "merged", "median_h": None, "n": 0}]
        ranking = brain_view.rank_waits(rows, "")
        self.assertEqual(ranking["items"][0]["why"],
                         "4 tickets x 2.5h median wait = 10.0 ticket-hours lost")
        self.assertEqual([item["state"] for item in ranking["items"]], ["pr", "queued"])
        self.assertIn("waits are missing: rate limited", brain_view.render_ranking(
            brain_view.rank_waits([], "rate limited")))


class HotSpotsTest(unittest.TestCase):
    def test_lane_failure_rates_mark_a_lane_above_20_percent_with_a_word(self):
        comments = ([outcome("zen", "failed: tests failed")] * 2
                    + [outcome("zen", "opened a pull request: https://example.com/pr/1")] * 3
                    + [outcome("codex-2", "opened a pull request: https://example.com/pr/2")] * 4
                    + [outcome("codex", "failed: tests failed")]
                    + [outcome("codex", "is unavailable (provider limit). This is not a failure")]
                    + [outcome("zen", "failed: tests failed", at="2029-12-01T00:00:00Z")])
        rates = hot_spots.lane_failure_rates(comments, NOW)
        by_lane = {row["lane"]: row for row in rates}
        self.assertEqual((by_lane["zen"]["failed"], by_lane["zen"]["runs"]), (2, 5))
        self.assertEqual((by_lane["codex"]["failed"], by_lane["codex"]["runs"]), (1, 5))
        body = hot_spots.render_failure_rates(rates)
        self.assertRegex(body, r'class="lane-bar bad"[^>]*>.*?zen.*?40% failed.*?failing often')
        self.assertRegex(body, r'class="lane-bar"[^>]*>.*?codex.*?20% failed')
        self.assertNotRegex(body, r'codex[^<]*<[^>]*>[^<]*20% failed[^<]*failing often')

    def test_findings_counted_over_seven_days_with_a_trend(self):
        def finding(rule, at, login="review-bot[bot]"):
            return {"body": "\U0001f7e0 medium | _robustness_ | `%s` | quick win\n\ntext" % rule,
                    "created_at": at, "user": {"login": login, "type": "Bot"}}
        comments = [finding("no-test", "2030-01-07T00:00:00Z"),
                    finding("no-test", "2030-01-06T00:00:00Z"),
                    finding("no-test", "2029-12-30T00:00:00Z"),
                    finding("em-dash", "2029-12-30T00:00:00Z"),
                    finding("em-dash", "2029-12-29T00:00:00Z"),
                    finding("em-dash", "2030-01-07T00:00:00Z"),
                    finding("no-test", "2030-01-07T00:00:00Z", login="someone")]
        rows = hot_spots.finding_counts(comments, "review-bot", NOW)
        self.assertEqual([(r["rule"], r["count"], r["trend"]) for r in rows],
                         [("no-test", 2, "up from 1"), ("em-dash", 1, "down from 2")])

    def test_stuck_tickets_are_above_rung_zero_or_failed(self):
        tickets = [{"repo": "example-owner/sample", "number": 5, "title": "Climbing", "state": "pr"},
                   {"repo": "example-owner/sample", "number": 6, "title": "Failed", "state": "failed"},
                   {"repo": "example-owner/sample", "number": 7, "title": "Fine", "state": "pr"}]
        comments = [outcome("codex", "failed\n<!-- loom:rung 1 -->", number=5),
                    outcome("codex", "x\n<!-- loom:rung 0 -->", number=7)]
        stuck = hot_spots.stuck_tickets(tickets, comments)
        self.assertEqual([(row["number"], row["rung"]) for row in stuck], [(5, 1), (6, 0)])

    def test_missing_sources_say_why(self):
        body = hot_spots.render_hot_spots(hot_spots.build_hot_spots(
            {"wait_rows": [], "tickets": {}, "hot_spots_error": "comments unreadable"},
            "waits rate limited", NOW))
        self.assertIn("Waits are missing: waits rate limited", body)
        self.assertIn("Review findings are missing: comments unreadable", body)
        self.assertIn("Lane outcomes are missing: comments unreadable", body)


class StoreReadTest(unittest.TestCase):
    class FakeGH:
        rate = {}

        def __init__(self, message):
            self.message, self.paths = message, []

        def rest(self, method, path, body=None, ok=()):
            from types import SimpleNamespace
            data = {"object": {"sha": "c"}} if "/git/ref/" in path else {"message": self.message}
            return SimpleNamespace(status=200, data=data)

        def paginate(self, path):
            self.paths.append(path)
            if "/issues/comments" in path:
                return [outcome("zen", "failed: tests failed"),
                        dict(outcome("zen", "failed: tests failed"), user={"login": "stranger"})]
            return []

    def test_store_reads_the_step_record_and_trusted_outcome_comments(self):
        import os
        from unittest import mock
        from dashboard.github import TicketStore
        message = 'holder=brain-a\nloom:tick={"started_at":"2030-01-08T11:30:00Z","step":"merge","step_index":3,"notes":[]}'
        gh = self.FakeGH(message)
        store = TicketStore(["example-owner/sample"], gh=gh)
        data = {}
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "station-bot"}):
            self.assertEqual(store._read_brain_step("example-owner/status")[0]["step"], "merge")
            store._read_hot_spot_comments(data)
        self.assertEqual(len(data["issue_comments"]), 1)
        self.assertEqual(data["issue_comments"][0]["repo"], "example-owner/sample")
        self.assertEqual(data["hot_spots_error"], "")
        self.assertRegex(gh.paths[0], r"since=\d{4}-\d\d-\d\dT00:00:00Z")
        record, error = store._read_brain_step("")
        self.assertIsNone(record)
        self.assertIn("LOOM_STATUS_REPO is not set", error)


class RouteTest(unittest.TestCase):
    def _get(self, path, payload):
        from dashboard.server import _dispatch_get

        class State:
            stale = False
            waits_error = ""

            def response(self):
                return payload

        class Handler:
            def send_page(self, body, code=200):
                self.result = (code, body)
        handler = Handler()
        handler.path = path
        _dispatch_get(handler, State())
        return handler.result

    def test_brain_and_hot_spots_routes_render(self):
        payload = {"tickets": {}, "stale": False, "brain_step": RECORD, "brain_step_error": "",
                   "improving": {"journals": [JOURNAL]}, "wait_rows": [], "issue_comments": [],
                   "review_comments": []}
        code, body = self._get("/brain", payload)
        self.assertEqual(code, 200)
        self.assertIn('aria-current="page">Brain</a>', body)
        self.assertIn("merge <small>current</small>", body)
        code, body = self._get("/hot-spots", payload)
        self.assertEqual(code, 200)
        self.assertIn("Where it slows", body)


if __name__ == "__main__":
    unittest.main()
