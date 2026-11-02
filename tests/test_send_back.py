"""The seven-day send-back comparison (loom#2162)."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from shadow_env import setUpModule, tearDownModule  # noqa: F401

from bus import send_back, station

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
BOT = "review-bot"


def finding(rule, age_days=1, login="review-bot[bot]", category="stability"):
    """A finding comment in the review bot's real first-line shape."""
    at = (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"body": "! high | _%s_ | `%s` | heavy lift\n\nDetail." % (category, rule),
            "created_at": at, "user": {"login": login, "type": "Bot"}}


class CountsTest(unittest.TestCase):
    def test_a_rule_counts_from_the_third_field_of_a_real_finding(self):
        found = send_back.counts([finding("weakened-test"), finding("intent-mismatch")], BOT, NOW)
        self.assertEqual(found, {"acceptance-criterion-unmet": 0, "weakened-test": 1,
                                 "intent-mismatch": 1})

    def test_a_category_of_the_same_name_is_not_a_rule(self):
        found = send_back.counts([finding("dead-code", category="weakened-test")], BOT, NOW)
        self.assertEqual(sum(found.values()), 0)

    def test_only_the_bot_inside_the_week_counts(self):
        found = send_back.counts([finding("weakened-test", age_days=8),
                                  finding("weakened-test", login="someone")], BOT, NOW)
        self.assertEqual(sum(found.values()), 0)

    def test_a_count_at_its_filed_number_has_not_fallen(self):
        fell = {"acceptance-criterion-unmet": 26, "weakened-test": 17, "intent-mismatch": 12}
        self.assertFalse(send_back.not_fallen(fell))
        self.assertTrue(send_back.not_fallen(dict(fell, **{"weakened-test": 18})))

    def test_the_numbers_to_beat_are_the_filed_week(self):
        # The prior week's 0 weakened-test findings would reopen on any one.
        self.assertEqual(send_back.FILED_COUNTS, {"acceptance-criterion-unmet": 27,
                                                  "weakened-test": 18, "intent-mismatch": 13})
        self.assertFalse(send_back.not_fallen({"acceptance-criterion-unmet": 9,
                                               "weakened-test": 1, "intent-mismatch": 6}))

    def test_a_rule_missing_from_the_counts_reads_as_none(self):
        self.assertTrue(send_back.not_fallen({"weakened-test": 18}))
        self.assertFalse(send_back.not_fallen({}))
        self.assertIn("intent-mismatch: 0 (was 13)", send_back.report({}))

    def test_the_clock_starts_at_the_merge_of_2170(self):
        self.assertEqual(send_back.LANDED_AT, "2026-11-01T09:08:45Z")


class StationStepTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state)
        self.posted, self.patched = [], []
        self.findings = [finding("weakened-test")] * 18
        self.shadow = False
        self.repos = [send_back.REPO]
        self.by_repo = {}

    def run_step(self, now=NOW):
        st = station.Station.__new__(station.Station)
        st.state = self.state
        st.now = now.timestamp()
        st.s = SimpleNamespace(repos=self.repos, review_bot=BOT)
        st.gh = SimpleNamespace(rest=lambda *a, **k: self.patched.append(a))
        st._comment = lambda *a: self.posted.append(a)

        def pages(path):
            if "/pulls/comments" in path:
                return self.by_repo.get(path.split("/")[2] + "/" + path.split("/")[3],
                                        self.findings)
            return [{"body": c[2]} for c in self.posted] if "/comments" in path else []
        st._paginate = pages
        with mock.patch.object(station.shadow, "active", return_value=self.shadow):
            st.compare_send_back_counts()

    def saved(self):
        with open(os.path.join(self.state, send_back.STATE_FILE)) as fh:
            return json.load(fh)

    def test_nothing_runs_inside_the_seven_days_after_2170_merged(self):
        self.run_step(datetime(2026, 11, 8, 9, 8, tzinfo=timezone.utc))
        self.assertEqual((self.posted, self.patched), ([], []))
        self.assertNotIn("completed", self.saved())

    def test_the_step_runs_once_seven_days_after_2170_merged(self):
        self.run_step(datetime(2026, 11, 8, 9, 9, tzinfo=timezone.utc))
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(self.saved()["completed"])

    def test_counts_that_have_not_fallen_reopen_the_ticket_once(self):
        self.run_step()
        self.assertEqual(len(self.posted), 1)
        self.assertIn("weakened-test: 18 (was 18)", self.posted[0][2])
        self.assertEqual(self.patched[0][2], {"state": "open"})
        self.run_step(NOW + timedelta(days=1))
        self.assertEqual((len(self.posted), len(self.patched)), (1, 1))
        self.assertTrue(self.saved()["completed"])

    def test_counts_that_fell_post_the_numbers_and_do_not_reopen(self):
        self.findings = []
        self.run_step()
        self.assertEqual(len(self.posted), 1)
        self.assertIn("All three fell.", self.posted[0][2])
        self.assertEqual(self.patched, [])
        self.assertTrue(self.saved()["completed"])

    def test_shadow_mode_holds_the_reopen_and_posts_nothing(self):
        self.shadow = True
        with mock.patch.object(station.shadow, "record_held") as held:
            self.run_step()
        self.assertEqual((self.posted, self.patched), ([], []))
        held.assert_called_once_with(station.shadow.REOPEN, send_back.REPO, 2162, "issue",
                                     source="seven-day send-back comparison",
                                     reason="send-back counts have not fallen")

    def test_counts_add_up_across_the_served_repos(self):
        self.repos = [send_back.REPO, "example-owner/sample", "not a repo?x=1"]
        self.by_repo = {send_back.REPO: [finding("weakened-test")] * 10,
                        "example-owner/sample": [finding("weakened-test")] * 8
                        + [finding("intent-mismatch")] * 2}
        self.findings = [finding("weakened-test")] * 50  # what an unvetted path would read
        self.run_step()
        self.assertIn("weakened-test: 18 (was 18), intent-mismatch: 2 (was 13)",
                      self.posted[0][2])
        self.assertEqual(self.patched[0][2], {"state": "open"})

    def test_a_station_not_serving_the_home_repo_never_runs_it(self):
        self.repos = ["example-owner/sample"]
        self.run_step()
        self.assertEqual((self.posted, self.patched), ([], []))
        self.assertFalse(os.path.exists(os.path.join(self.state, send_back.STATE_FILE)))


if __name__ == "__main__":
    unittest.main()
