"""Disk guard ask: when disk_low has held for 60+ minutes, the station
opens one needs:human ask (Kind: hardware) on the Loom goal home's repo,
naming free space, floor, and loom home size by top-level folder; edits
it in place while paused; closes it when disk recovers; paused under
60 minutes files nothing."""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, station  # noqa: E402


GB = 1024 ** 3
FLOOR_GB = 15.0
FLOOR = int(FLOOR_GB * GB)
NOW = 1_800_000_000.0
HOME = "/srv/loom"
GOAL_REPO = "quadseven/loom"
GOAL_NUMBER = 123
DISK_ASK_NUMBER = 456


class FakeGH:
    def __init__(self):
        self.posted_issues = []  # (repo, title, body) -> returns issue number
        self.posted_comments = []  # (repo, number, body)
        self.labels_added = []  # (repo, number, labels)
        self.labels_removed = []  # (repo, number, labels)
        self.comments = {}  # (repo, number) -> [comment]
        self.patched_comments = []  # (repo, comment_id, body)
        self.closed_issues = []  # (repo, number)
        self.issue_counter = DISK_ASK_NUMBER
        self.created_issues = {}  # (repo, number) -> {"title": ..., "body": ..., "state": "open"}

    def rest(self, method, path, data=None):
        if method == "GET" and path.startswith("/repos/") and "/issues/" in path and not path.endswith("/labels") and not "/comments" in path:
            # GET /repos/{owner}/{repo}/issues/{number}
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            number = int(parts[5])
            if number == GOAL_NUMBER:
                return type("Resp", (), {"data": {"number": GOAL_NUMBER, "state": "open", "labels": [{"name": "loom:goal"}]}})()
            if (repo, number) in self.created_issues:
                issue = self.created_issues[(repo, number)]
                return type("Resp", (), {"data": {"number": number, "state": issue["state"], "labels": [{"name": l} for l in issue.get("labels", [])]}})()
            return type("Resp", (), {"data": {}})()
        if method == "POST" and path.endswith("/labels"):
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            number = int(parts[5])
            self.labels_added.append((repo, number, data.get("labels", [])))
        if method == "POST" and path.endswith("/comments"):
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            number = int(parts[5])
            comment_id = len(self.comments.get((repo, number), [])) + 1
            body = data.get("body", "")
            comment = {"id": comment_id, "body": body}
            self.comments.setdefault((repo, number), []).append(comment)
            self.posted_comments.append((repo, number, body))
        if method == "PATCH" and "/issues/comments/" in path:
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            comment_id = int(parts[6])
            body = data.get("body", "")
            for comments_list in self.comments.values():
                for c in comments_list:
                    if c.get("id") == comment_id:
                        c["body"] = body
                        break
            self.patched_comments.append((repo, comment_id, body))
        if method == "PATCH" and "/issues/" in path and not "/comments" in path and not "/labels" in path:
            # PATCH /repos/{owner}/{repo}/issues/{number} - update issue body/state
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            number = int(parts[5])
            if (repo, number) in self.created_issues:
                if "body" in data:
                    self.created_issues[(repo, number)]["body"] = data["body"]
                if "state" in data:
                    self.created_issues[(repo, number)]["state"] = data["state"]
                if data.get("state") == "closed":
                    self.closed_issues.append((repo, number))
        if method == "DELETE" and path.endswith("/labels/needs:human"):
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            number = int(parts[5])
            self.labels_removed.append((repo, number, ["needs:human"]))
        if method == "POST" and path == "/repos/%s/issues" % GOAL_REPO:
            # Create new issue
            self.issue_counter += 1
            title = data.get("title", "")
            body = data.get("body", "")
            issue_num = self.issue_counter
            self.created_issues[(GOAL_REPO, issue_num)] = {"title": title, "body": body, "state": "open", "labels": []}
            self.posted_issues.append((GOAL_REPO, title, body, issue_num))
            return type("Resp", (), {"data": {"number": issue_num}})()
        return type("Resp", (), {"data": {}})()

    def paginate(self, path):
        if "/comments" in path:
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            if "issues/comments" in path:
                return []
            number = int(parts[5])
            return self.comments.get((repo, number), [])
        if "/issues?" in path:
            issues = []
            for (repo, num), data in self.created_issues.items():
                issues.append({"number": num, "labels": [{"name": l} for l in data.get("labels", [])], "pull_request": None})
            # Also include the goal home
            issues.append({"number": GOAL_NUMBER, "labels": [{"name": "loom:goal"}], "pull_request": None, "html_url": "https://github.com/" + GOAL_REPO + "/issues/" + str(GOAL_NUMBER)})
            return issues
        return []

    def graphql(self, query, variables):
        return {"repository": {"issue": {"editor": {"login": "loom-app[bot]", "__typename": "Bot"}, "lastEditedAt": None}}}


class DiskGuardAskTest(unittest.TestCase):
    """Tests for the disk guard ask feature."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "loom")
        os.makedirs(os.path.join(self.home, "state", "station"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "logs"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "worktrees"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "run"), exist_ok=True)
        # Create some folders with sizes
        os.makedirs(os.path.join(self.home, "worktrees", "repo1"), exist_ok=True)
        with open(os.path.join(self.home, "logs", "a.log"), "w") as f:
            f.write("x" * 1000)
        with open(os.path.join(self.home, "worktrees", "repo1", "f.txt"), "w") as f:
            f.write("y" * 2000)
        os.makedirs(os.path.join(self.home, "run", "lane1"), exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_disk_guard(self, low, free, at):
        from bus.station import DISK_GUARD_FILE, _write_json
        path = os.path.join(self.home, "state", "station", DISK_GUARD_FILE)
        _write_json(path, {"low": low, "free": free, "floor": FLOOR, "at": at})

    def _make_station(self, free_space=None):
        from bus.station import Station
        st = Station.__new__(Station)
        st.s = type("Settings", (), {
            "home": self.home,
            "repos": [GOAL_REPO],
            "authors": ["loom-app[bot]"],
            "disk_free_min_gb": FLOOR_GB,
            "thin_snapshots": False,
        })()
        st.gh = FakeGH()
        st.state = os.path.join(self.home, "state", "station")
        st.now = NOW
        st._free_read = False
        st._free = free_space if free_space is not None else int(0.5 * FLOOR)
        st._disk_free = lambda: st._free
        st._goal_homes = lambda: [(GOAL_REPO, {"number": GOAL_NUMBER, "labels": [{"name": "loom:goal"}]})]
        st._num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        st._goals = {}
        return st

    def test_disk_low_under_60_minutes_files_nothing(self):
        """When disk_low has held for less than 60 minutes, no ask is filed."""
        st = self._make_station()

        # Disk has been low for 30 minutes (1800 seconds)
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 1800)

        # Call the disk guard ask check
        st._check_disk_guard_ask()

        # No ask should be filed
        self.assertEqual(len(st.gh.posted_issues), 0, "No ask should be filed under 60 minutes")

    def test_disk_low_over_60_minutes_creates_ask(self):
        """When disk_low has held for 60+ minutes, one needs:human ask issue is created."""
        st = self._make_station()

        # Disk has been low for 61 minutes (3660 seconds)
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 3660)

        # Call the disk guard ask check
        st._check_disk_guard_ask()

        # One ask issue should be created
        self.assertEqual(len(st.gh.posted_issues), 1, "One ask issue should be created after 60+ minutes")
        repo, title, body, issue_num = st.gh.posted_issues[0]
        self.assertEqual(repo, GOAL_REPO)
        self.assertIn("Disk guard", title)
        self.assertIn("Kind: hardware", body)
        self.assertIn("Free space: 7.5 GB", body)
        self.assertIn("Floor: 15.0 GB", body)
        self.assertIn("Free disk space on the lane machine.", body)
        # The ask state should track the new issue number
        self.assertEqual(issue_num, DISK_ASK_NUMBER + 1)

    def test_ask_edited_in_place_while_paused(self):
        """The ask issue body is edited in place (not a new issue) while disk stays low."""
        st = self._make_station()

        # Disk has been low for 61 minutes
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 3660)

        # First check creates the ask issue
        st._check_disk_guard_ask()
        first_issue_count = len(st.gh.posted_issues)
        first_issue_num = st.gh.posted_issues[0][3]

        # Second check (later pass) should edit in place, not create new issue
        st.now = NOW + 3600  # One hour later
        st._free = 9 * GB
        station._write_json(os.path.join(st.state, "disk.json"), {"folders": {"logs": 2 * GB}})
        st._check_disk_guard_ask()

        # Should still have only one ask issue (edited in place)
        self.assertEqual(len(st.gh.posted_issues), first_issue_count, "Ask issue should be edited in place, not duplicated")
        # Verify the issue body was patched
        self.assertIn((GOAL_REPO, first_issue_num), st.gh.created_issues)
        body = st.gh.created_issues[(GOAL_REPO, first_issue_num)]["body"]
        self.assertIn("Kind: hardware", body)
        self.assertIn("Free space: 9.0 GB", body)
        self.assertIn("Floor: 15.0 GB", body)
        self.assertIn("Loom home holds 2.0 GB (logs 2.0 GB)", body)

    def test_ask_closed_when_disk_recovers(self):
        """When disk recovers (free space above floor), the ask issue is closed."""
        st = self._make_station()

        # Disk has been low for 61 minutes - ask created
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 3660)
        st._check_disk_guard_ask()
        issue_num = st.gh.posted_issues[0][3]

        # Now disk recovers
        st._free = int(2 * FLOOR)  # Above floor
        st._free_read = False  # Force re-read
        self._write_disk_guard(False, int(2 * FLOOR), NOW)

        st._check_disk_guard_ask()

        # The ask issue should be closed
        self.assertIn((GOAL_REPO, issue_num), st.gh.created_issues)
        self.assertEqual(st.gh.created_issues[(GOAL_REPO, issue_num)]["state"], "closed")
        self.assertIn((GOAL_REPO, issue_num), st.gh.closed_issues)

    def test_goal_home_with_existing_needs_human_untouched(self):
        """A goal home that already carries needs:human keeps its label and comment untouched."""
        st = self._make_station()
        gh = st.gh

        # Pre-populate goal home with needs:human label and comment
        gh.created_issues[(GOAL_REPO, GOAL_NUMBER)] = {
            "title": "Goal: Loom",
            "body": "Goal home body",
            "state": "open",
            "labels": ["loom:goal", "needs:human"]
        }
        goal_comment_id = 1
        gh.comments[(GOAL_REPO, GOAL_NUMBER)] = [{
            "id": goal_comment_id,
            "body": "<!-- loom:human v1 -->\nKind: hardware\nExisting ask on goal home"
        }]

        # Disk has been low for 61 minutes
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 3660)
        st._check_disk_guard_ask()

        # A new ask issue should be created (not on the goal home)
        self.assertEqual(len(gh.posted_issues), 1, "One ask issue should be created")
        issue_num = gh.posted_issues[0][3]
        self.assertNotEqual(issue_num, GOAL_NUMBER, "Ask should be a new issue, not the goal home")

        # Goal home should still have needs:human label
        goal_labels = gh.created_issues[(GOAL_REPO, GOAL_NUMBER)]["labels"]
        self.assertIn("needs:human", goal_labels, "Goal home should keep its needs:human label")

        # Goal home comment should be unchanged
        goal_comments = gh.comments.get((GOAL_REPO, GOAL_NUMBER), [])
        self.assertEqual(len(goal_comments), 1, "Goal home should have one comment")
        self.assertEqual(goal_comments[0]["body"], "<!-- loom:human v1 -->\nKind: hardware\nExisting ask on goal home",
                         "Goal home comment should be untouched")

        # Now disk recovers
        st._free = int(2 * FLOOR)
        st._free_read = False
        self._write_disk_guard(False, int(2 * FLOOR), NOW)
        st._check_disk_guard_ask()

        # Goal home should still have needs:human label (not removed)
        goal_labels = gh.created_issues[(GOAL_REPO, GOAL_NUMBER)]["labels"]
        self.assertIn("needs:human", goal_labels, "Goal home should still keep its needs:human label after resume")

        # Goal home comment should still be unchanged
        goal_comments = gh.comments.get((GOAL_REPO, GOAL_NUMBER), [])
        self.assertEqual(goal_comments[0]["body"], "<!-- loom:human v1 -->\nKind: hardware\nExisting ask on goal home",
                         "Goal home comment should still be untouched after resume")

        # The disk ask issue should be closed
        self.assertEqual(gh.created_issues[(GOAL_REPO, issue_num)]["state"], "closed",
                         "Disk ask issue should be closed on resume")


if __name__ == "__main__":
    unittest.main()