"""Test for needs:login stale ask detection."""
import unittest
from unittest import mock
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus.github import GitHub
from bus import station
from bus.station import Settings


class TestStaleAskDetection(unittest.TestCase):
    """Tests for stale ask detection on needs:login labels."""

    def setUp(self):
        """Set up a fake GitHub instance."""
        self.gh = mock.MagicMock(spec=GitHub)
        self.gh.rest.side_effect = lambda method, path, *args, **kw: (
            method, path, {}, None, kw
        )
        self.gh.paginate.side_effect = lambda path, **kw: []
        self.gh.paginate.iter.side_effect = lambda path: iter([])

        self.state = ".test_state"
        self.home = "."
        os.makedirs(self.state, exist_ok=True)

        self.settings = Settings(
            home=self.home,
            repos=["owner/repo"],
            authors=["operator"],
            owners=["owner"],
        )

        self.station = station.Station(
            gh=self.gh,
            settings=self.settings,
        )

    def tearDown(self):
        """Clean up state."""
        if os.path.exists(self.state):
            import shutil
            shutil.rmtree(self.state, ignore_errors=True)

    def test_stale_when_commit_live(self):
        """Test that an issue whose ask names a commit that the live roll contains is
        moved off needs:login with one note, and is not moved twice."""
        commit_sha = "7d7a163"

        issue = {
            "number": 123,
            "state": "open",
            "title": "Test issue",
            "body": "Fix bug\nstale-when: commit %s live" % commit_sha,
            "repository": {
                "full_name": "owner/repo"
            },
            "labels": [
                {"name": "needs:login"}
            ],
        }

        deployed_commits = {commit_sha}

        self.gh.paginate.side_effect = lambda path, **kw: [
            {"body": issue["body"]}
        ] if "comments" in path else []

        def mock_rest(method, path, *args, **kw):
            if method == "GET" and "/repos/owner/repo/commits/%s" % commit_sha in path:
                return mock.Mock(data={"sha": commit_sha})
            elif method == "POST" and "/repos/owner/repo/issues/123/comments" in path:
                return mock.Mock()
            elif method == "PATCH" and "/repos/owner/repo/issues/123" in path:
                return mock.Mock()
            return (method, path, {}, None, kw)

        self.gh.rest.side_effect = mock_rest

        withdrawn = self.station._check_and_withdraw_stale_asks(
            [issue], deployed_commits, set(), 0
        )

        self.assertEqual(withdrawn, 1)

        comment_calls = [call for call in self.station.gh.rest.call_args_list
                        if call[0][1].endswith("/comments")]
        self.assertEqual(len(comment_calls), 1)

        label_calls = [call for call in self.station.gh.rest.call_args_list
                      if call[0][1].endswith("/issues/123") and "PATCH" in call[0]]
        self.assertEqual(len(label_calls), 1)

        # Confirm needs:login was removed and moved to awaiting-live-check
        labels = [l["name"] for l in issue["labels"]]
        self.assertNotIn("needs:login", labels)
        self.assertIn("awaiting-live-check", labels)

        # Verify it is not moved twice
        withdrawn_second = self.station._check_and_withdraw_stale_asks(
            [issue], deployed_commits, set(), 0
        )
        self.assertEqual(withdrawn_second, 0)
        self.assertEqual(len([call for call in self.station.gh.rest.call_args_list
                              if call[0][1].endswith("/comments")]), 1)
        self.assertEqual(len([call for call in self.station.gh.rest.call_args_list
                              if call[0][1].endswith("/issues/123") and "PATCH" in call[0]]), 1)

    def test_stale_when_issue_exists(self):
        """Test that an issue whose ask names an existing issue gets
        removed from needs:login with one note and is not moved twice."""
        deployed_issues = {("owner/repo", 456)}

        issue = {
            "number": 123,
            "state": "open",
            "title": "Test issue",
            "body": "Help me\nstale-when: issue exists",
            "repository": {
                "full_name": "owner/repo"
            },
            "labels": [
                {"name": "needs:login"}
            ],
        }

        self.gh.paginate.side_effect = lambda path, **kw: [
            {"body": issue["body"]}
        ] if "comments" in path else []

        def mock_rest(method, path, *args, **kw):
            if method == "GET" and "/repos/owner/repo/issues/456" in path:
                return mock.Mock(data={"number": 456})
            elif method == "POST" and "/repos/owner/repo/issues/123/comments" in path:
                return mock.Mock()
            elif method == "PATCH" and "/repos/owner/repo/issues/123" in path:
                return mock.Mock()
            return (method, path, {}, None, kw)

        self.gh.rest.side_effect = mock_rest

        withdrawn = self.station._check_and_withdraw_stale_asks(
            [issue], set(), deployed_issues, 0
        )

        self.assertEqual(withdrawn, 1)

        comment_calls = [call for call in self.station.gh.rest.call_args_list
                        if call[0][1].endswith("/comments")]
        self.assertEqual(len(comment_calls), 1)

        label_calls = [call for call in self.station.gh.rest.call_args_list
                      if call[0][1].endswith("/issues/123") and "PATCH" in call[0]]
        self.assertEqual(len(label_calls), 1)

        # Confirm needs:login was removed
        labels = [l["name"] for l in issue["labels"]]
        self.assertNotIn("needs:login", labels)

        # Verify it is not moved twice
        withdrawn_second = self.station._check_and_withdraw_stale_asks(
            [issue], set(), deployed_issues, 0
        )
        self.assertEqual(withdrawn_second, 0)
        self.assertEqual(len([call for call in self.station.gh.rest.call_args_list
                              if call[0][1].endswith("/comments")]), 1)

    def test_stale_when_no_ask_comment(self):
        """Test that an issue labeled needs:login with no ask comment is
        reported in the standup and not counted as waiting on a person."""
        issue = {
            "number": 123,
            "state": "open",
            "title": "Test issue",
            "body": "Just a random comment\nNo stale-when line",
            "repository": {
                "full_name": "owner/repo"
            },
            "labels": [
                {"name": "loom:ticket"},
                {"name": "needs:login"}
            ],
            "comments": 0,
        }

        self.gh.paginate.return_value = []

        # 1. Standup: verify reported in standup and not counted as waiting on a person
        groups = self.station._standup_ticket_groups([issue])
        self.assertEqual(len(groups["asks"]), 0)
        self.assertEqual(len(groups["queued"]), 1)
        self.assertEqual(groups["queued"][0]["number"], 123)

        body = self.station._overdue_standup_body([issue])
        self.assertIn("#123 Test issue", body)
        self.assertNotIn("waiting on you", body)

        # 2. Check and withdraw: withdraws stale ask because it carries no ask comment
        withdrawn = self.station._check_and_withdraw_stale_asks(
            [issue], set(), set(), 0
        )
        self.assertEqual(withdrawn, 1)

        # 3. Verify it is not moved twice
        withdrawn_second = self.station._check_and_withdraw_stale_asks(
            [issue], set(), set(), 0
        )
        self.assertEqual(withdrawn_second, 0)

    def test_stale_count_in_standup(self):
        """Test that the standup line includes the count of asks withdrawn
        as stale."""
        self.station._stale_asks_withdrawn = 3

        groups = {"asks": [], "merged": [], "verified": [], "sent_back": [], "failed": [],
                 "regressed": [], "aged_pr": [], "in_flight": [], "queued": [], "needs_difficulty": []}
        summary = self.station._standup_summary(groups, self.station)
        self.assertIn("3 stale asks withdrawn", summary)

        body = self.station._overdue_standup_body([])
        self.assertIn("3 stale asks withdrawn", body)

    def test_ask_comment_with_stale_when_commit(self):
        """Test that an ask comment with 'stale-when: commit <sha> live' is
        properly parsed."""
        commit_sha = "7d7a163"
        comment_body = "Fix bug\nstale-when: commit %s live" % commit_sha

        self.assertEqual(self.station._parse_stale_when(comment_body), commit_sha)

        issue = {
            "number": 123,
            "state": "open",
            "body": comment_body,
            "repository": {
                "full_name": "owner/repo"
            },
            "labels": [
                {"name": "needs:login"}
            ],
        }

        def mock_rest(method, path, *args, **kw):
            if method == "GET" and "/repos/owner/repo/commits/%s" % commit_sha in path:
                return mock.Mock(data={"sha": commit_sha})
            return (method, path, {}, None, kw)

        self.gh.rest.side_effect = mock_rest

        withdrawn = self.station._check_and_withdraw_stale_asks(
            [issue], {commit_sha}, set(), 0
        )
        self.assertEqual(withdrawn, 1)

    def test_ask_comment_with_stale_when_issue(self):
        """Test that an ask comment with 'stale-when: issue exists' is
        properly parsed."""
        comment_body = "Help me\nstale-when: issue exists"
        self.assertEqual(self.station._parse_stale_when(comment_body), "issue exists")

        deployed_issues = {("owner/repo", 456)}

        issue = {
            "number": 123,
            "state": "open",
            "body": comment_body,
            "repository": {
                "full_name": "owner/repo"
            },
            "labels": [
                {"name": "needs:login"}
            ],
        }

        def mock_rest(method, path, *args, **kw):
            if method == "GET" and "/repos/owner/repo/issues/456" in path:
                return mock.Mock(data={"number": 456})
            return (method, path, {}, None, kw)

        self.gh.rest.side_effect = mock_rest

        withdrawn = self.station._check_and_withdraw_stale_asks(
            [issue], set(), deployed_issues, 0
        )
        self.assertEqual(withdrawn, 1)


if __name__ == "__main__":
    unittest.main()
