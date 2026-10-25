"""bus/difficulty.py: a blind settled difficulty after merge, beside the
perceived one, so routing can compare its guess with the delivered work.

The scorer reads only the merged work (diff size, fix rounds, review
findings), never the ticket's Difficulty: line; the ticket is never written;
the lane status comment shows guessed against settled per lane, model and
kind over the last 14 days.
"""
from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import difficulty, scorecard  # noqa: E402
from bus.github import GitHub  # noqa: E402
from dashboard import views  # noqa: E402
from test_scorecard import (BOT, LANES_LOGIN, NOW, REPO, Route, card, finding,  # noqa: E402
                            lane_pr, quiet)


class Logged(Route):
    """The test_scorecard fake, keeping every request's method and path."""

    def __init__(self, routes, threads):
        super().__init__(routes, threads)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.split("api.github.com")[-1]))
        return super().__call__(method, url, headers, body)


class Settle(unittest.TestCase):
    def test_small_clean_work_is_easy(self):
        self.assertEqual(difficulty.settle(20, 5, 2, 0, 0)[0], "easy")

    def test_mid_sized_work_is_medium(self):
        self.assertEqual(difficulty.settle(150, 40, 5, 1, 0)[0], "medium")

    def test_large_work_with_rounds_and_findings_is_hard(self):
        level, why = difficulty.settle(400, 50, 12, 2, 3)
        self.assertEqual(level, "hard")
        self.assertEqual(why, "450 changed lines in 12 files, 2 fix rounds sent back, "
                              "3 review findings")

    def test_a_bad_work_finding_counts_as_many_findings(self):
        self.assertEqual(difficulty.settle(150, 40, 5, 1, 1)[0], "medium")
        self.assertEqual(difficulty.settle(150, 40, 5, 1, 1, bad_work=True)[0], "hard")

    def test_the_scorer_takes_no_perceived_difficulty(self):
        params = set(inspect.signature(difficulty.settle).parameters)
        self.assertEqual(params, {"additions", "deletions", "files", "rounds", "findings",
                                  "bad_work"})

    def test_perceived_reads_the_difficulty_line(self):
        self.assertEqual(difficulty.perceived("Difficulty: Hard\n\n## Why\nx"), "hard")
        self.assertEqual(difficulty.perceived("## Why\nx"), "")
        self.assertEqual(difficulty.perceived(None), "")


def ticket(number, level):
    return {"number": number, "body": "Difficulty: %s\n\n## Why\nx\n" % level}


class AfterMerge(unittest.TestCase):
    def setUp(self):
        big = lane_pr(1)
        big["labels"] = [{"name": "class:logic"}]
        self.pulls = [big, lane_pr(2), lane_pr(3, merged=False)]
        self.fake = Logged({
            "/repos/%s/pulls?state=closed" % REPO: self.pulls,
            "/repos/%s/pulls/1" % REPO: {"additions": 400, "deletions": 60, "changed_files": 12},
            "/repos/%s/pulls/2" % REPO: {"additions": 400, "deletions": 60, "changed_files": 12},
            "/repos/%s/issues/101/comments" % REPO: [{"body": "<!-- loom:fix v1 -->\nfix"}],
            "/repos/%s/issues/102/comments" % REPO: [{"body": "<!-- loom:fix v1 -->\nfix"}],
            "/repos/%s/issues/103/comments" % REPO: [],
            "/repos/%s/issues/101" % REPO: ticket(101, "easy"),
            "/repos/%s/issues/102" % REPO: ticket(102, "hard"),
            "/repos/%s/issues/103" % REPO: ticket(103, "easy"),
        }, {1: [finding("naming", "low")], 2: [finding("naming", "low")]})

    def refresh(self, got):
        quiet(got.refresh, GitHub("t", transport=self.fake), [REPO], [LANES_LOGIN], BOT, {}, {})
        return got

    def test_each_merged_ticket_gets_a_settled_difficulty_beside_its_guess(self):
        got = self.refresh(card())
        one = got.data["records"]["%s#1" % REPO]
        self.assertEqual((one["perceived"], one["settled"], one["kind"]),
                         ("easy", "hard", "logic"))
        self.assertIn("460 changed lines in 12 files", one["settled_why"])
        # Not merged: no settled difficulty.
        self.assertNotIn("settled", got.data["records"]["%s#3" % REPO])

    def test_the_guess_does_not_steer_the_score(self):
        got = self.refresh(card())
        records = got.data["records"]
        self.assertEqual(records["%s#2" % REPO]["perceived"], "hard")
        self.assertEqual(records["%s#1" % REPO]["settled"], records["%s#2" % REPO]["settled"])

    def test_the_perceived_difficulty_on_the_ticket_is_never_changed(self):
        self.refresh(card())
        writes = [c for c in self.fake.calls if c[0] not in ("GET",)
                  and c[1] != "/graphql"]
        self.assertEqual(writes, [])

    def test_a_diff_that_cannot_be_read_is_settled_on_a_later_refresh(self):
        self.fake.fail.add("/repos/%s/pulls/1" % REPO)
        state = card().path.rsplit("/", 1)[0]
        first = self.refresh(card(state=state))
        self.assertNotIn("settled", first.data["records"]["%s#1" % REPO])
        first.save()
        self.fake.fail.clear()
        later = self.refresh(card(now=NOW + scorecard.REFRESH, state=state))
        self.assertEqual(later.data["records"]["%s#1" % REPO]["settled"], "hard")

    def test_the_status_shows_agreement_and_bias(self):
        got = self.refresh(card())
        lines = got.markdown()
        self.assertIn(difficulty.TABLE_HEAD, lines)
        self.assertIn("| spark | nemotron-3.5-super-vl | - | 1 | 1 | 0 | 0 | +0.00 |", lines)
        self.assertIn("| spark | nemotron-3.5-super-vl | logic | 1 | 0 | 1 | 0 | +2.00 |", lines)
        body = "\n".join(lines)
        # The dashboard's readers of the same comment are not confused by it.
        self.assertEqual(len(scorecard.parse_rows(body)), len(got.table()))
        self.assertEqual(views.parse_lane_rows(body), [])


class Report(unittest.TestCase):
    def record(self, guess, got, lane="codex", model="gpt-6-luna", kind="logic", ago=0):
        return {"lane": lane, "model": model, "kind": kind, "merged": True,
                "at": NOW - ago, "perceived": guess, "settled": got}

    def test_agreement_and_bias_per_lane_model_and_kind(self):
        records = {"a": self.record("easy", "easy"), "b": self.record("easy", "medium"),
                   "c": self.record("hard", "easy"), "d": self.record("medium", "hard"),
                   "e": self.record("easy", "hard", kind="docs"),
                   "f": self.record("easy", "hard", model="gpt-6-sol")}
        rows = difficulty.report(records, NOW - 86400)
        row = rows[("codex", "gpt-6-luna", "logic")]
        self.assertEqual((row["scored"], row["agree"], row["harder"], row["easier"]),
                         (4, 1, 2, 1))
        self.assertAlmostEqual(row["bias"], 0.0)
        self.assertEqual(rows[("codex", "gpt-6-luna", "docs")]["bias"], 2.0)
        self.assertIn(("codex", "gpt-6-sol", "logic"), rows)

    def test_only_the_last_14_days_of_scored_merges_count(self):
        records = {"old": self.record("easy", "hard", ago=15 * 86400),
                   "new": self.record("easy", "medium", ago=13 * 86400),
                   "unguessed": self.record("", "hard"),
                   "open": dict(self.record("easy", "hard"), merged=False)}
        lines = difficulty.markdown(records, NOW)
        self.assertIn("the last 14 days", lines[1])
        self.assertEqual([x for x in lines if x.startswith("| codex")],
                         ["| codex | gpt-6-luna | logic | 1 | 0 | 1 | 0 | +1.00 |"])

    def test_an_empty_report_still_has_its_table(self):
        lines = difficulty.markdown({}, NOW)
        self.assertEqual(lines[-1], "| - | - | - | 0 | 0 | 0 | 0 | 0.00 |")
        self.assertEqual(json.loads(json.dumps(difficulty.report({}, NOW))), {})


if __name__ == "__main__":
    unittest.main()
