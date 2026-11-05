"""Repository backpressure leaves existing PR work runnable."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bus.github import GitHubError, Response
import test_bus_station as fixtures
from test_bus_station import Base, BODY_WITH_CRITERIA, REPO, AUTHORS, GitHub, station, FIX_BODY


class RepoPrCapTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.default_body = BODY_WITH_CRITERIA

    def pulls(self, count):
        self.fake.pulls = [dict(number=100+i, state="open", draft=False,
                               user={"login": AUTHORS[0]}, body="",
                               head={"ref": "loom/%d-work" % (100+i)})
                           for i in range(count)]

    def claim(self):
        self.fake.add(12)
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        return runner, runner._claim(REPO, "codex", self.fake.issues[12])

    def test_nine_lane_prs_stop_new_build_at_default_eight(self):
        self.pulls(9)
        runner, claimed = self.claim()
        self.assertFalse(claimed)
        self.assertEqual(self.listing(), [])
        notes = str(self.fake.comments[12])
        self.assertIn("9 open pull requests (cap 8)", notes)
        self.assertNotIn("loom:running", self.fake.names(12))

    def test_fix_round_still_dispatches_over_cap(self):
        self.pulls(9)
        self.fake.add(12)
        fixtures.FixTest.comment(self, 12, FIX_BODY)
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.assertTrue(runner._claim(REPO, "codex", self.fake.issues[12]))
        text = Path(self.inbox("codex", self.listing()[0])).read_text()
        self.assertIn("Fix-PR: 99", text)

    def test_configured_repo_cap_and_default_are_independent(self):
        config = station.Settings.from_env({"LOOM_REPO_OPEN_PR_CAP": "8 %s=2" % REPO})
        self.settings.repo_open_pr_caps = config.repo_open_pr_caps
        self.pulls(2)
        _, claimed = self.claim()
        self.assertFalse(claimed)
        self.assertEqual(config.repo_open_pr_caps.get("*"), 8)

    def test_under_cap_dispatches_with_default_configuration(self):
        self.pulls(7)
        _, claimed = self.claim()
        self.assertTrue(claimed)
        self.assertTrue(self.listing())

    def test_brain_apps_count_but_personal_prs_do_not(self):
        self.pulls(8)
        for pr in self.fake.pulls:
            pr["head"]["ref"] = "manual/work"
        self.fake.pulls[0]["user"]["type"] = "Bot"
        runner, claimed = self.claim()
        self.assertTrue(claimed)
        self.assertEqual(runner._repo_pr_count(REPO), 1)

    def test_second_page_is_counted_and_duplicate_numbers_are_not(self):
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.pulls(100)
        first = self.fake.pulls
        second = [dict(first[0]), dict(first[0], number=200)]
        with mock.patch.object(runner.gh, "rest", side_effect=[
                Response(200, first, {}), Response(200, second, {})]) as read:
            self.assertEqual(runner._repo_pr_count(REPO), 101)
            self.assertEqual(runner._repo_pr_count(REPO), 101)
        self.assertEqual(read.call_count, 2)

    def test_unread_count_holds_build_but_not_fix(self):
        self.fake.add(12)
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        with mock.patch.object(runner, "_read_repo_pr_count", side_effect=GitHubError("unread")) as read:
            self.assertTrue(runner._repo_pr_held(REPO, self.fake.issues[12]))
            self.assertTrue(runner._repo_pr_held(REPO, self.fake.issues[12]))
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(self.fake.comments[12]), 1)

    def test_invalid_cap_does_not_disable_backpressure(self):
        for value in ("0", "-1", "nonsense", "bad=2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                station.Settings.from_env({"LOOM_REPO_OPEN_PR_CAP": value})

    def test_lane_status_shows_count_and_cap(self):
        self.pulls(3)
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.assertIn("Open pull requests: %s 3/8" % REPO, runner._lanes_body([]))

    def test_malformed_listing_never_becomes_zero(self):
        for data in ({}, [None], [{"user": []}], [{"user": {"login": AUTHORS[0]}}]):
            with self.subTest(data=data):
                runner = station.Station(GitHub("token", transport=self.fake), self.settings)
                with mock.patch.object(runner.gh, "rest", return_value=Response(200, data, {})):
                    self.assertIsNone(runner._repo_pr_count(REPO))

    def test_public_status_hides_private_repository_name(self):
        self.settings.status_repo = "example-owner/public"
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        runner._publics = {self.settings.status_repo: True, REPO: False}
        text = "\n".join(runner._repo_pr_lines())
        self.assertNotIn(REPO, text)
        self.assertIn("private repo 1 0/8", text)

    def test_return_to_previous_count_records_current_hold(self):
        self.pulls(9)
        self.fake.add(12)
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        prior = ["held: %s has 9 open pull requests (cap 8)" % REPO,
                 "held: %s has 10 open pull requests (cap 8)" % REPO]
        with mock.patch.object(runner, "_status_texts", return_value=prior), \
                mock.patch.object(runner, "_routine") as note:
            runner._repo_pr_held(REPO, self.fake.issues[12])
        self.assertEqual(note.call_count, 1)
        self.assertEqual(note.call_args.args[2], prior[0])

    def test_non_string_author_and_branch_are_unread(self):
        self.pulls(1)
        valid = self.fake.pulls[0]
        cases = [dict(valid, user={"login": []}), dict(valid, head={}),
                 dict(valid, head={"ref": []}), dict(valid, number=None),
                 dict(valid, number="12")]
        for pr in cases:
            runner = station.Station(GitHub("token", transport=self.fake), self.settings)
            with self.subTest(pr=pr), mock.patch.object(
                    runner.gh, "rest", return_value=Response(200, [pr], {})):
                self.assertIsNone(runner._repo_pr_count(REPO))

    def test_trusted_brain_footer_counts_without_current_model_membership(self):
        self.pulls(1)
        self.fake.pulls[0]["head"]["ref"] = "brain/work"
        self.fake.pulls[0]["body"] = "Loom ticket n/a (direct request), lane main-chat, model retired-model."
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.assertEqual(runner._repo_pr_count(REPO), 1)

    def test_count_change_republishes_lane_status_but_unchanged_does_not(self):
        self.settings.status_repo = REPO
        published = []
        for count in (1, 1, 2):
            self.pulls(count)
            runner = station.Station(GitHub("token", transport=self.fake), self.settings)
            with mock.patch.object(runner, "_lane_rows", return_value=[]), \
                    mock.patch.object(runner, "_spend_shown", return_value=None), \
                    mock.patch.object(runner, "_scorecard_lines", return_value=[]), \
                    mock.patch.object(runner, "_lanes_issue", return_value=99), \
                    mock.patch.object(runner, "_upsert_lanes_comment", side_effect=lambda *a: published.append(a[2])):
                runner.publish_lanes()
        self.assertEqual(len(published), 2)
        self.assertIn("1/8", published[0])
        self.assertIn("2/8", published[1])


if __name__ == "__main__":
    unittest.main()
