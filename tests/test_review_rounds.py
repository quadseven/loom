"""Merged review rounds are measured independently of routing and difficulty."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import difficulty
import test_difficulty
from test_scorecard import BOT, NOW, REPO, card


def check(head, conclusion="failure", title="Elder markings", summary=""):
    return {"head_sha": head, "name": "Grug - Elder", "status": "completed",
            "conclusion": conclusion, "output": {"title": title, "summary": summary}}


def review(number, head, kind="User", state="CHANGES_REQUESTED"):
    return {"id": number, "commit_id": head, "state": state,
            "user": {"type": kind, "login": "reviewer" if kind == "User" else BOT}}


class ReviewRounds(unittest.TestCase):
    refresh = test_difficulty.AfterMerge.refresh

    def setUp(self):
        test_difficulty.AfterMerge.setUp(self)
        self.pulls[:] = self.pulls[:1]
        self.reviews = []
        self.fake.routes["/repos/%s/pulls/1/reviews" % REPO] = self.reviews
        self.fake.routes["/repos/%s/pulls/1/commits" % REPO] = [
            {"sha": "a" * 40}, {"sha": "b" * 40}, {"sha": "c" * 40}]
        for head, result in (("a", "failure"), ("b", "failure"), ("c", "success")):
            self.fake.routes[self.path(head)] = {"check_runs": [check(head * 40, result)]}

    def path(self, head):
        return "/repos/%s/commits/%s/check-runs" % (REPO, head * 40)

    def merged(self, state=None):
        return self.refresh(state or card()).data["records"]["%s#1" % REPO]

    def test_two_failed_heads_then_success_record_two_rounds(self):
        got = self.merged()
        self.assertEqual(got["rounds"], 2)
        self.assertEqual(got["score"], 1)  # hard=3, minus two rounds

    def test_duplicate_runs_on_one_head_count_once(self):
        self.fake.routes[self.path("a")]["check_runs"] *= 2
        self.assertEqual(self.merged()["rounds"], 2)

    def test_force_pushed_reviewed_head_and_human_reviews_are_retained(self):
        self.reviews.extend([review(1, "d" * 40, "Bot"), review(2, "c" * 40),
                             review(2, "c" * 40)])
        self.fake.routes[self.path("d")] = {"check_runs": [check("d" * 40)]}
        self.assertEqual(self.merged()["rounds"], 4)

    def test_successful_elder_bot_changes_request_is_not_a_failed_head(self):
        self.reviews.append(review(1, "c" * 40, "Bot"))
        self.assertEqual(self.merged()["rounds"], 2)

    def test_degraded_and_neutral_reviewers_do_not_penalize_lane(self):
        self.fake.routes[self.path("a")] = {"check_runs": [check("a" * 40, "neutral")]}
        self.fake.routes[self.path("b")] = {"check_runs": [check(
            "b" * 40, summary="Degraded review (model failure): not a verdict.")]}
        self.assertEqual(self.merged()["rounds"], 0)

    def test_failed_history_remains_unknown_until_retry(self):
        self.fake.fail.add(self.path("b"))
        state = card()
        self.assertNotIn("rounds", self.merged(state))
        self.fake.fail.clear()
        self.assertEqual(self.merged(state)["rounds"], 2)

    def test_later_check_pages_are_read_before_score_is_committed(self):
        path = self.path("a") + "?filter=all&per_page=100&page="
        self.fake.routes[path + "1"] = {"check_runs": [check("a" * 40, "success")] * 100}
        self.fake.routes[path + "2"] = {"check_runs": [check("a" * 40)]}
        self.assertEqual(self.merged()["rounds"], 2)
        self.fake.fail.add(path + "2")
        self.assertNotIn("rounds", self.merged())

    def test_no_history_and_malformed_checks_are_unknown_not_zero(self):
        self.fake.routes[self.path("a")] = {"not_checks": []}
        self.assertNotIn("rounds", self.merged())
        self.fake.routes["/repos/%s/pulls/1/commits" % REPO] = []
        self.assertNotIn("rounds", self.merged())

    def test_custom_review_name_and_partial_title_use_existing_contract(self):
        self.fake.routes[self.path("a")] = {"check_runs": [check(
            "a" * 40, title="WARN Elder review coverage partial")]}
        self.fake.routes[self.path("b")]["check_runs"][0]["name"] = "Custom review"
        state = card(env={"LOOM_REVIEW_CHECK": "Custom review"})
        self.assertEqual(self.merged(state)["rounds"], 1)
        self.assertEqual(self.merged()["rounds"], 0)

    def test_new_metrics_do_not_change_demotion_or_routing_table(self):
        state = card()
        self.merged(state)
        before = state.table(), state.demoted()
        record = state.data["records"]["%s#1" % REPO]
        record["rounds"], record["score"] = 100, 0
        self.assertEqual((state.table(), state.demoted()), before)

    def test_malformed_user_and_output_leave_score_unknown(self):
        self.reviews.append(dict(review(1, "c" * 40), user=["malformed"]))
        self.assertNotIn("rounds", self.merged())
        self.reviews.clear()
        self.fake.routes[self.path("a")]["check_runs"][0]["output"] = ["malformed"]
        self.assertNotIn("rounds", self.merged())

    def test_malformed_commit_row_does_not_drop_a_possible_failed_head(self):
        self.fake.routes["/repos/%s/pulls/1/commits" % REPO].append({"missing_sha": True})
        self.assertNotIn("rounds", self.merged())

    def test_complete_empty_check_history_has_no_failed_verdicts(self):
        for head in "abc":
            self.fake.routes[self.path(head)] = {"check_runs": []}
        self.assertEqual(self.merged()["rounds"], 0)

    def test_more_than_25_heads_are_fully_counted(self):
        commits = [{"sha": "%040x" % i} for i in range(1, 27)]
        self.fake.routes["/repos/%s/pulls/1/commits" % REPO] = commits
        for commit in commits:
            sha = commit["sha"]
            path = "/repos/%s/commits/%s/check-runs" % (REPO, sha)
            self.fake.routes[path] = {"check_runs": [check(sha)]}
        self.assertEqual(self.merged()["rounds"], 26)

    def test_failure_on_fourth_check_page_is_counted(self):
        path = self.path("a") + "?filter=all&per_page=100&page="
        for page in range(1, 4):
            self.fake.routes[path + str(page)] = {"check_runs": [check("a" * 40, "success")] * 100}
        self.fake.routes[path + "4"] = {"check_runs": [check("a" * 40)]}
        self.assertEqual(self.merged()["rounds"], 2)

    def test_old_settled_record_is_backfilled_without_rescoring(self):
        state = card()
        state.data["records"]["%s#1" % REPO] = {
            "lane": "spark", "model": "example", "settled": "easy", "perceived": "easy",
            "merged": True, "at": NOW, "review": "clean"}
        got = self.merged(state)
        self.assertEqual(got["rounds"], 2)
        self.assertEqual(got["score"], 0)
        self.assertEqual(got["settled"], "easy")


class RoundReport(unittest.TestCase):
    def test_review_round_readers_stay_under_review_complexity_cap(self):
        from test_brain_station_complexity import CAP, complexity, qualified_names
        names = dict(qualified_names("bus/scorecard.py"))
        for name in ("Scorecard._review_rounds", "Scorecard._human_review_rounds",
                     "Scorecard._failed_review_head"):
            self.assertIn(name, names)
            self.assertLessEqual(complexity(names[name]), CAP, name)

    def test_mean_score_and_rounds_ignore_unknown_history_and_old_records(self):
        base = {"lane": "codex", "model": "example", "kind": "logic", "merged": True,
                "at": NOW, "settled": "hard", "perceived": "hard"}
        records = {"one": dict(base, rounds=2, score=1),
                   "two": dict(base, rounds=0, score=3), "unknown": dict(base),
                   "old": dict(base, at=NOW-15*86400, rounds=50, score=0)}
        got = difficulty.report(records, NOW-14*86400)[("codex", "example", "logic")]
        self.assertEqual(got["rounds"], 2)
        self.assertEqual(got["mean_score"], 2.0)
        self.assertEqual(got["scored"], 3)
        self.assertIn("mean score", "\n".join(difficulty.markdown(records, NOW)))


if __name__ == "__main__":
    unittest.main()
