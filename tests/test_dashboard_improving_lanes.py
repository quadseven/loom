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


if __name__ == "__main__":
    unittest.main()
