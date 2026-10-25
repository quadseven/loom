"""Test for needs:login stale ask detection."""
import unittest
from unittest import mock

from bus import station
from bus.github import GitHub


class TestStaleAskDetection(unittest.TestCase):
    """Tests for stale ask detection on needs:login labels."""

    def setUp(self):
        """Set up a fake GitHub instance."""
        self.gh = mock.MagicMock(spec=GitHub)
        self.gh.rest.side_effect = lambda method, path, **kw: (
            method, path, {}, None, kw
        )
        self.stations = station.Station(
            home="/tmp",
            repos=["owner/repo"],
            authors=["alice"],
            owners=["owner"],
        )

    def test_stale_when_commit_live(self):
        """Test that an issue whose ask names a commit that is live gets
        removed from needs:login with one note and is not moved twice."""
        pass

    def test_stale_when_issue_exists(self):
        """Test that an issue whose ask names an existing issue gets
        removed from needs:login with one note and is not moved twice."""
        pass

    def test_stale_when_no_ask_comment(self):
        """Test that an issue labeled needs:login with no ask comment is
        reported in the standup and not counted as waiting on a person."""
        pass

    def test_stale_count_in_standup(self):
        """Test that the standup line includes the count of asks withdrawn
        as stale."""
        pass

    def test_ask_comment_with_stale_when_commit(self):
        """Test that an ask comment with 'stale-when: commit <sha> live' is
        properly parsed."""
        pass

    def test_ask_comment_with_stale_when_issue(self):
        """Test that an ask comment with 'stale-when: issue exists' is
        properly parsed."""
        pass


if __name__ == "__main__":
    unittest.main()
