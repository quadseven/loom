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

    def test_the_clock_starts_at_the_oldest_commit(self):
        commits = [{"commit": {"committer": {"date": "2026-11-02T00:00:00Z"}}},
                   {"commit": {"committer": {"date": "2026-11-01T00:00:00Z"}}}]
        self.assertEqual(send_back.landed_at(commits), "2026-11-01T00:00:00Z")
        self.assertIsNone(send_back.landed_at([]))


class StationStepTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state)
        self.posted, self.patched = [], []
        self.landed = "2026-11-01T00:00:00Z"
        self.findings = [finding("weakened-test")] * 18

    def run_step(self, now=NOW):
        st = station.Station.__new__(station.Station)
        st.state = self.state
        st.now = now.timestamp()
        st.s = SimpleNamespace(self_repo="example-owner/sample", repos=["example-owner/sample"],
                               review_bot=BOT)
        st.gh = SimpleNamespace(rest=lambda *a, **k: self.patched.append(a))
        st._comment = lambda *a: self.posted.append(a)

        def pages(path):
            if "/commits?" in path:
                return [{"commit": {"committer": {"date": self.landed}}}] if self.landed else []
            return self.findings if "/pulls/comments" in path else []
        st._paginate = pages
        with mock.patch.object(station.shadow, "active", return_value=False):
            st.compare_send_back_counts()

    def saved(self):
        with open(os.path.join(self.state, send_back.STATE_FILE)) as fh:
            return json.load(fh)

    def test_nothing_runs_before_the_file_lands(self):
        self.landed = None
        self.run_step()
        self.assertEqual((self.posted, self.patched), ([], []))
        self.assertNotIn("completed", self.saved())

    def test_nothing_runs_inside_the_seven_days(self):
        self.landed = (NOW - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.run_step()
        self.assertEqual((self.posted, self.patched), ([], []))

    def test_counts_that_have_not_fallen_reopen_the_ticket_once(self):
        self.run_step()
        self.assertEqual(len(self.posted), 1)
        self.assertIn("weakened-test: 18 (was 18)", self.posted[0][2])
        self.assertEqual(self.patched[0][2], {"state": "open"})
        self.run_step(NOW + timedelta(days=1))
        self.assertEqual((len(self.posted), len(self.patched)), (1, 1))
        self.assertTrue(self.saved()["completed"])

    def test_counts_that_fell_finish_with_no_reopen(self):
        self.findings = []
        self.run_step()
        self.assertEqual((self.posted, self.patched), ([], []))
        self.assertTrue(self.saved()["completed"])


if __name__ == "__main__":
    unittest.main()
