"""Fixture-backed tests for the Brain and Hot spots views."""
import unittest
from datetime import datetime, timezone

from dashboard.brain_view import build_brain, render_brain, tag_note
from dashboard.hot_spots import (build_hot_spots, finding_rules, lane_failure_rates,
                                 render_hot_spots, stuck_tickets)

STEPS = ["lock", "status", "merge", "verify", "failed", "asks", "brief", "triage", "journal", "unlock"]
DEFER = "<!-- loom:defer v1 stage=triage item=example-owner/sample#40 rev=abc123 holder=muse -->"
DONE = "<!-- loom:done v1 stage=merge item=example-owner/sample#12 holder=muse -->"
JOURNAL = {"body": "<!-- loom:journal v1 -->\nTick 2030-01-02T14:30:00Z, muse\n- Shipped: example-owner/sample#12\n"
                   "- Briefed: none\n- Waiting: none\n- Asks: none\n- Cost: 10 requests, 5 minutes\n"
                   + DONE + "\n" + DEFER,
           "created_at": "2030-01-02T14:40:00Z"}
RECORD = {"holder": "muse", "started_at": "2030-01-02T14:30:00Z", "step": "merge", "step_index": 3,
          "steps": STEPS, "notes": [
              {"ts": "2030-01-02T14:31:00Z", "step": "status", "text": "Sample p95 regressed after a ship."},
              {"ts": "2030-01-02T14:33:00Z", "step": "merge", "text": "Merged sample#12."},
              {"ts": "2030-01-02T14:34:00Z", "step": "merge", "text": "Plain note."}]}


class BrainViewTest(unittest.TestCase):
    def test_current_step_is_marked_and_markers_are_verbatim(self):
        rendered = render_brain(build_brain(RECORD, None, [JOURNAL], []))
        self.assertIn("Step 3 of 10, <strong>merge</strong>", rendered)
        self.assertIn("3. merge</span> current", rendered)
        self.assertIn("2. status</span> done", rendered)
        self.assertIn("4. verify</span> ahead", rendered)
        self.assertEqual(rendered.count('class="done"'), 2)
        self.assertEqual(rendered.count('class="current"'), 1)
        self.assertIn("<code>%s</code>" % DONE.replace("<", "&lt;").replace(">", "&gt;"), rendered)
        self.assertIn(DEFER.replace("<", "&lt;").replace(">", "&gt;"), rendered)

    def test_no_step_record_says_not_recorded_not_step_one(self):
        empty = {"holder": None, "started_at": None, "step": None, "step_index": None, "steps": STEPS, "notes": []}
        for record, error in ((empty, None), (None, "step record unreadable: boom")):
            rendered = render_brain(build_brain(record, error, [JOURNAL], []))
            self.assertIn("not recorded", rendered)
            self.assertNotIn("Step 1 of", rendered)
            self.assertNotIn('class="current"', rendered)

    def test_notes_are_tagged_and_recent_ticks_listed(self):
        data = build_brain(RECORD, None, [JOURNAL], [])
        self.assertEqual([n["tag"] for n in data["notes"]], ["regression", "merged", None])
        self.assertEqual(tag_note("Discord note from the operator"), "operator")
        self.assertEqual(data["ticks"][0]["holder"], "muse")
        self.assertEqual(data["ticks"][0]["outcome"], "ok")
        self.assertIn("shipped example-owner/sample#12", data["ticks"][0]["line"])

    def test_missing_sources_say_so(self):
        rendered = render_brain(build_brain(None, "no lock repo", [], []))
        for what in ("Next move: missing", "Ranking: missing", "Handoff markers: missing", "Recent ticks: missing"):
            self.assertIn(what, rendered)


def outcome(lane, kind="failed"):
    text = {"failed": "failed: CI red", "ok": "opened a pull request: https://example.com/pr/1"}[kind]
    return {"body": "Lane %s %s" % (lane, text), "created_at": "2030-01-02T00:00:00Z"}


class HotSpotsTest(unittest.TestCase):
    NOW = datetime(2030, 1, 10, tzinfo=timezone.utc)

    def test_lane_above_twenty_percent_is_marked_with_a_word(self):
        comments = ([outcome("codex-2")] * 1 + [outcome("codex", "ok")] * 3      # 25%, family codex
                    + [outcome("claude", "ok")] * 4 + [outcome("claude")] * 1)    # exactly 20%
        rows = {row["lane"]: row for row in lane_failure_rates(comments)}
        self.assertEqual(rows["codex"]["rate"], 25)
        self.assertTrue(rows["codex"]["over"])
        self.assertFalse(rows["claude"]["over"])
        data = build_hot_spots([], None, {"issue_comments": comments, "review_comments": []}, self.NOW)
        rendered = render_hot_spots(data)
        self.assertIn("over 20%, failing too often", rendered)
        self.assertEqual(rendered.count("failing too often"), 1)

    def test_finding_rules_trend_over_two_weeks(self):
        def bot(rule, day):
            return {"body": "\U0001f7e0 medium | _robustness_ | `%s` | quick win" % rule,
                    "created_at": "2030-01-%02dT00:00:00Z" % day}
        rows = finding_rules([bot("a-rule", 9), bot("a-rule", 8), bot("a-rule", 5), bot("b-rule", 6),
                              bot("b-rule", 1), bot("b-rule", 2), bot("c-rule", 2)], self.NOW)
        by_rule = {r["rule"]: r for r in rows}
        self.assertEqual((by_rule["a-rule"]["count"], by_rule["a-rule"]["trend"]), (3, "new"))
        self.assertEqual((by_rule["b-rule"]["count"], by_rule["b-rule"]["before"], by_rule["b-rule"]["trend"]), (1, 2, "down"))
        self.assertNotIn("c-rule", by_rule)

    def test_stuck_is_failed_or_rung_above_zero(self):
        tickets = [{"repo": "example-owner/sample", "number": n, "state": s, "title": "t%d" % n}
                   for n, s in ((1, "failed"), (2, "running"), (3, "running"), (4, "pr"))]
        rung = lambda n, r, at: {"body": "<!-- loom:rung %d -->" % r, "created_at": at,
                                 "issue_url": "https://api.example.com/repos/example-owner/sample/issues/%d" % n}
        comments = [rung(2, 1, "2030-01-01T00:00:00Z"), rung(3, 2, "2030-01-01T00:00:00Z"),
                    rung(3, 0, "2030-01-02T00:00:00Z")]
        self.assertEqual([r["ticket"]["number"] for r in stuck_tickets(tickets, comments)], [2, 1])

    def test_missing_sources_and_waits(self):
        rendered = render_hot_spots(build_hot_spots([], None, {"errors": ["example-owner/sample comments: boom"]}, self.NOW))
        for what in ("Wait times: missing", "Review findings: missing", "Lane failure rate: missing", "Stuck tickets: missing"):
            self.assertIn(what, rendered)
        waits = {"queued": [1, 2, 3], "pr": [5, 6, 8]}
        data = build_hot_spots([], waits, None, self.NOW)
        self.assertEqual(data["bottleneck"], "pr")
        self.assertIn("median 6.0h, worst 8.0h over 3 tickets", render_hot_spots(data))


if __name__ == "__main__":
    unittest.main()
