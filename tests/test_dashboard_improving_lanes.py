"""Fixture-backed acceptance tests for the new dashboard views."""
import unittest

from dashboard.views import (build_improvements, build_scoreboard, render_lanes,
                             render_improving)


class ImprovingViewTest(unittest.TestCase):
    def test_journal_link_and_moved_only_when_metric_changed(self):
        journal = {"body": "<!-- loom:journal v1 -->\n- Friction: reduce repeat failures in example-owner/sample#8",
                   "repo": "example-owner/sample", "issue_number": 30,
                   "html_url": "https://github.com/example-owner/sample/issues/30#issuecomment-1"}
        tickets = [{"repo": "example-owner/sample", "number": 8, "title": "Reduce repeat failures",
                    "state": "shipped", "refs": [("example-owner/sample", "30")]}]
        current = {"body": "<!-- loom:metrics v1 -->\n2030-01-02: 8/9 pass; streak 1\n1. PASS Quality: 0 failures (target 0)"}
        previous = {"body": "<!-- loom:metrics v1 -->\n2030-01-01: 7/9 pass; streak 0\n1. FAIL Quality: 2 failures (target 0)"}
        data = build_improvements([journal], tickets, [previous, current])
        self.assertIs(data["records"][0]["ticket"], tickets[0])
        rendered = render_improving(data)
        self.assertIn(journal["html_url"], rendered)
        self.assertIn("moved: Quality", rendered)
        unchanged = build_improvements([journal], tickets, [current, current])
        self.assertNotIn("moved:", render_improving(unchanged))


class LaneViewTest(unittest.TestCase):
    def test_models_counted_by_model_and_tier(self):
        prs = [
            {"body": "Loom ticket example-owner/sample#1, lane codex-2, model model-alpha.",
             "tier": "easy", "state": "closed", "merged_at": "2030-01-01"},
            {"body": "Loom ticket example-owner/sample#2, lane codex-2, model model-alpha.",
             "tier": "easy", "state": "open"},
            {"body": "Loom ticket example-owner/sample#3, lane anthropic-2, model model-beta.",
             "tier": "hard", "state": "closed", "merged_at": None},
        ]
        scoreboard = build_scoreboard(prs)
        self.assertEqual(scoreboard["easy"]["model-alpha"]["merged"], 1)
        self.assertEqual(scoreboard["easy"]["model-alpha"]["open"], 1)
        self.assertEqual(scoreboard["hard"]["model-beta"]["closed"], 1)

    def test_held_lane_reason_is_visible(self):
        html = render_lanes({"lanes": [{"lane": "codex-2", "available": False,
                    "ticket": None, "model": "model-alpha", "quota": None,
                    "reason": "provider limit until reset"}], "scoreboard": {},
                    "anthropic_share": 0})
        self.assertIn("held", html)
        self.assertIn("provider limit until reset", html)

    def test_quota_is_integer_clamped_before_rendering(self):
        rendered = render_lanes({"lanes": [{"lane": "codex-2", "available": True,
                    "ticket": None, "model": "model-alpha", "quota": 120.8}],
                    "scoreboard": {}, "anthropic_share": 0})
        self.assertIn('style="width:100%"', rendered)


METRICS_TODAY = {"body": (
    "<!-- loom:metrics v1 -->\n2030-01-02: 5/9 pass; streak 0 day(s)\n"
    "1. FAIL Flow: 1 PRs >6h; median 2.3h (72 tickets); 0 past last rung >24h (target 0; <3h; 0)\n"
    "2. FAIL Throughput: example-owner/sample 53, example-owner/other 0; verified 22/74 (target each goal repo >=1 merge; >=50% verified)\n"
    "3. FAIL Spread: codex 93, claude 78; Anthropic 5/282; idle 1/634 passes (target Anthropic <=50%; idle 0)\n"
    "4. PASS Escalation: 0 tickets failed twice on one model doing the work; 45 failures left out as limit or transient (target 0)\n"
    "5. FAIL Quality: 0 unfixed regressions >2h; 0 unreviewed merges; 1 fix PRs without tests (target 0; 0; 0)\n"
    "6. PASS Autonomy: 1 asks opened (target <=1/day)\n"
    "7. PASS Visibility: example-owner/sample#30 0.9h, example-owner/sample#31 -0.0h (target each goal report <=2h)\n"
    "8. PASS Self-repair: 0 un-ticketed factory faults (target 0)\n"
    "9. PASS Generic: 0 files:  (target 0 files)")}
METRICS_YESTERDAY = {"body": METRICS_TODAY["body"].replace("2030-01-02", "2030-01-01")
                     .replace("median 2.3h", "median 4.1h").replace("verified 22/74", "verified 7/111")}


class FactoryNumbersTest(unittest.TestCase):
    def test_each_number_is_a_short_value_never_the_raw_line(self):
        from dashboard.views import metric_summary, parse_metrics
        latest = parse_metrics([METRICS_TODAY])[0][1]
        got = {name: metric_summary(name, row["value"]) for name, row in latest.items()}
        self.assertEqual(got["Flow"], ("2.3h", "1 PR over 6h"))
        self.assertEqual(got["Throughput"], ("22/74", "tickets verified"))
        self.assertEqual(got["Spread"], ("2%", "Anthropic share; idle 1/634 passes"))
        self.assertEqual(got["Escalation"], ("0", "failed twice on one model"))
        self.assertEqual(got["Quality"], ("1", "fix PRs without tests"))
        self.assertEqual(got["Autonomy"], ("1", "asks opened"))
        self.assertEqual(got["Visibility"], ("0.9h", "oldest goal report"))
        self.assertEqual(got["Self-repair"], ("0", "un-ticketed factory faults"))
        self.assertEqual(got["Generic"], ("0", "files naming one product"))

    def test_cards_say_what_each_was_the_day_before(self):
        html = render_improving(build_improvements([], [], [METRICS_YESTERDAY, METRICS_TODAY]))
        self.assertEqual(html.count("factory-card"), 9)
        self.assertIn("was 4.1h / still failing", html)
        self.assertIn("was 7/111", html)
        self.assertNotIn("72 tickets", html)
        self.assertNotIn("target", html)


class LoopTest(unittest.TestCase):
    def journal(self, created, friction):
        return {"body": "<!-- loom:journal v1 -->\n- Friction: %s" % friction, "created_at": created,
                "html_url": "https://example.invalid/journal/" + created}

    def test_a_friction_carried_across_ticks_is_one_card_from_the_newest_journal(self):
        older = self.journal("2030-01-01T00:00:00Z", "Lane PRs that conflict wait for a hand-sent fix.")
        newer = self.journal("2030-01-01T02:00:00Z", "Lane PRs that conflict wait for a hand-sent fix")
        data = build_improvements([older, newer], [], [])
        self.assertEqual(len(data["records"]), 1)
        self.assertEqual(data["records"][0]["journal"], newer)

    def test_the_loop_counts_each_stage_and_moved_is_the_fourth(self):
        tickets = [{"repo": "example-owner/sample", "number": 8, "title": "Fix it", "state": "shipped",
                    "refs": []}]
        journal = self.journal("2030-01-02T00:00:00Z", "slow merges in example-owner/sample#8")
        other = self.journal("2030-01-02T01:00:00Z", "nobody looked at this one yet")
        data = build_improvements([journal, other], tickets, [METRICS_YESTERDAY, METRICS_TODAY])
        stages = sorted(row["stage"] for row in data["records"])
        self.assertEqual(stages, [0, 3])
        html = render_improving(data)
        counts = [int(n) for n in __import__("re").findall(r'class="loop-count">(\d+)<', html)]
        self.assertEqual(counts, [2, 1, 1, 1])
        self.assertIn("moved: Flow", html)
        self.assertTrue(html.isascii())


class HeldLaneTest(unittest.TestCase):
    def test_limit_reason_uses_dashboard_timezone_and_configured_lane_model(self):
        from unittest.mock import patch
        from dashboard.server import _lane_view_data
        comment = ("<!-- loom:lanes v1 -->\n| lane | available | heartbeat (UTC) |\n|---|---|---|\n"
                   "| codex | no | 20:17Z |\n")
        usage = [{"provider": "codex", "lanes": ["codex"], "window": "credit",
                  "holds": {"codex": "limit: out of credit; unavailable until 2030-01-02T00:01:27Z"}}]
        payload = {"lanes": [{"body": comment}], "pull_requests": [], "usage": usage}
        with patch.dict("os.environ", {"LOOM_DASHBOARD_TZ": "America/New_York",
                                        "LOOM_CODEX_MODEL": "gpt-6-sol"}, clear=True):
            rendered = render_lanes(_lane_view_data(payload, []))
        self.assertIn("out of credit until 2030-01-01 19:01 EST", rendered)
        self.assertIn("gpt-6-sol", rendered)
        self.assertNotIn("held: the lane machine sent no reason", rendered)

    def test_a_held_lane_names_the_reason_its_heartbeat_gave(self):
        from dashboard.server import _lane_view_data
        comment = ("<!-- loom:lanes v1 -->\n| lane | available | heartbeat (UTC) |\n|---|---|---|\n"
                   "| anthropic-3 | no | 20:17Z |\n| zen | no | 20:17Z |\n")
        usage = [{"provider": "anthropic", "lanes": ["anthropic-3"], "window": "credit",
                  "holds": {"anthropic-3": "limit: out of credit; unavailable until 2030-01-02T00:01:27Z"}}]
        html = render_lanes(_lane_view_data({"lanes": [{"body": comment}], "pull_requests": [],
                                             "tickets": {}, "usage": usage}, []))
        cards = dict(__import__("re").findall(r"<strong>([\w-]+)</strong>(.*?)</article>", html))
        # The lane whose heartbeat gave a reason shows it, and only it.
        self.assertIn("out of credit until 2030-01-02 00:01 UTC", cards["anthropic-3"])
        self.assertNotIn("sent no reason", cards["anthropic-3"])
        # A held lane with no reason uploaded says that, never a stand-in.
        self.assertIn("no reason", cards["zen"])
        self.assertNotIn("held reason missing from lane status comment", html)

    def test_the_upload_keeps_holds_and_refuses_bad_ones(self):
        from dashboard.usage import validate
        row = {"provider": "anthropic", "window": "credit", "source": "spend", "lanes": ["anthropic-3"],
               "as_of": "2030-01-01T00:00:00+00:00", "holds": {"anthropic-3": "limit: out of credit"}}
        self.assertEqual(validate([row])[0]["holds"], {"anthropic-3": "limit: out of credit"})
        with self.assertRaises(ValueError):
            validate([{**row, "holds": {"anthropic-3": 5}}])


class NeedsYouTest(unittest.TestCase):
    def render(self, document, operator_items=()):
        from dashboard.server import _needs_you

        class State:
            human_counts = {"example-owner/sample": 2}
        State.operator_items = list(operator_items)
        return _needs_you(document, State())

    def test_operator_asks_come_first_with_the_exact_ask_and_the_backlog_is_collapsed(self):
        ask = {"repo": "example-owner/sample", "number": 7, "title": "A token for the dashboard",
               "html_url": "https://example.invalid/7", "created_at": "2030-01-01T00:00:00Z",
               "ask": "## Ask\n\nCreate a read-only token.\n\n## Why\n\nIt is too broad.",
               "waiting_since": "2030-01-01", "waiting_since_source": "updated", "unblocks": 0}
        document = {"ready_for_human": [{"repo": "example-owner/sample", "number": 9, "title": "Epic: tidy up",
                                         "unblocks": 4},
                                        {"repo": "example-owner/sample", "number": 7, "title": "duplicate"}]}
        html = self.render(document, [ask])
        self.assertIn("<blockquote>Create a read-only token.</blockquote>", html)
        self.assertNotIn("It is too broad", html)
        self.assertLess(html.index("example-owner/sample#7"), html.index("<details"))
        self.assertIn("From the backlog (1)", html)
        self.assertEqual(html.count("example-owner/sample#7"), 1)
        self.assertIn("loom:human counts by repo: example-owner/sample: 2", html)

    def test_an_ask_without_an_ask_section_says_so_instead_of_repeating_the_title(self):
        item = {"repo": "example-owner/sample", "number": 3, "title": "Move the drive",
                "ask": "Move the drive\n\nMore detail.", "unblocks": 0}
        html = self.render({}, [item])
        self.assertIn("No exact ask is written on the issue", html)
        self.assertNotIn("<blockquote>Move the drive</blockquote>", html)


if __name__ == "__main__":
    unittest.main()
