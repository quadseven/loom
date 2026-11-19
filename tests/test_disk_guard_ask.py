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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, station  # noqa: E402


GB = 1024 ** 3
FLOOR_GB = 15.0
FLOOR = int(FLOOR_GB * GB)
NOW = 1_800_000_000.0
HOME = "/srv/loom"
GOAL_REPO = "quadseven/loom"
GOAL_NUMBER = 123


class FakeGH:
    def __init__(self):
        self.posted = []  # (repo, number, body)
        self.labels_added = []  # (repo, number, labels)
        self.labels_removed = []  # (repo, number, labels)
        self.comments = {}  # (repo, number) -> [comment]

    def rest(self, method, path, data=None):
        if method == "GET" and path.startswith("/repos/") and "/issues/" in path and not path.endswith("/labels") and not "/comments" in path:
            # GET /repos/{owner}/{repo}/issues/{number}
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            number = int(parts[5])
            if number == GOAL_NUMBER:
                return type("Resp", (), {"data": {"number": GOAL_NUMBER, "state": "open", "labels": [{"name": "loom:goal"}]}})()
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
            self.posted.append((repo, number, data.get("body", "")))
        if method == "DELETE" and path.endswith("/labels/needs:human"):
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            number = int(parts[5])
            self.labels_removed.append((repo, number, ["needs:human"]))
        return type("Resp", (), {"data": {}})()

    def paginate(self, path):
        if "/comments" in path:
            parts = path.split("/")
            repo = parts[2] + "/" + parts[3]
            number = int(parts[4])
            return self.comments.get((repo, number), [])
        if "/issues?" in path:
            return [{"number": GOAL_NUMBER, "labels": [{"name": "loom:goal"}],
                     "pull_request": None, "html_url": "https://github.com/" + GOAL_REPO + "/issues/" + str(GOAL_NUMBER)}]
        return []

    def graphql(self, query, variables):
        return {"repository": {"issue": {"editor": {"login": "loom-app[bot]", "__typename": "Bot"}, "lastEditedAt": None}}}


class FakeStation:
    def __init__(self, home=HOME, now=NOW):
        self.s = type("Settings", (), {
            "home": home,
            "repos": [GOAL_REPO],
            "authors": ["loom-app[bot]"],
            "disk_free_min_gb": FLOOR_GB,
            "thin_snapshots": False,
        })()
        self.gh = FakeGH()
        self.state = os.path.join(home, "state", "station")
        self.now = now
        self._free_read = False
        self._free = None
        os.makedirs(self.state, exist_ok=True)
        os.makedirs(os.path.join(home, "logs"), exist_ok=True)
        os.makedirs(os.path.join(home, "worktrees"), exist_ok=True)
        os.makedirs(os.path.join(home, "run"), exist_ok=True)

    def _disk_free(self):
        if not self._free_read:
            self._free_read = True
            try:
                self._free = int(2 * self.s.disk_free_min_gb * GB)  # Above floor initially
            except OSError:
                self._free = None
        return self._free

    def disk_low(self):
        from bus.station import DISK_GUARD_FILE, _read_json, _write_json, _gb
        path = os.path.join(self.state, DISK_GUARD_FILE)
        saved = _read_json(path, {}, strict=False)
        was = bool(saved.get("low")) if isinstance(saved, dict) else False
        free = self._disk_free()
        floor = self.s.disk_free_min_gb * GB
        low = was if free is None else free < floor
        if free is not None and low != was:
            if low:
                print("claims paused: only %s free on the loom home's disk (under %s)" % (_gb(free), _gb(floor)))
            else:
                print("claims resumed: %s free on the loom home's disk (at least %s)" % (_gb(free), _gb(floor)))
            _write_json(path, {"low": low, "free": free, "floor": floor, "at": self.now})
        return low

    def tend_disk(self):
        from bus.station import DISK_EVERY, _read_json, _write_json, _gb, _tree_stat
        free = self._disk_free()
        path = os.path.join(self.state, "disk.json")
        saved = _read_json(path, {}, strict=False)
        last = saved.get("at") if isinstance(saved, dict) else None
        if self._num(last) and 0 <= self.now - last < DISK_EVERY:
            return
        sizes = {}
        for name in os.listdir(self.s.home):
            try:
                full = os.path.join(self.s.home, name)
                is_dir = os.path.isdir(full)
                key = name if is_dir else "files"
                sizes[key] = sizes.get(key, 0) + _tree_stat(full)[1]
            except OSError:
                continue
        total = sum(sizes.values())
        parts = ", ".join("%s %s" % (k, _gb(v)) for k, v in
                          sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0])))
        print("loom home holds %s (%s); %s free on its disk"
              % (_gb(total), parts or "empty", "unknown" if free is None else _gb(free)))
        _write_json(path, {"at": self.now, "bytes": total, "free": free, "folders": sizes})

    def _num(self, v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def _goal_homes(self):
        return [(GOAL_REPO, {"number": GOAL_NUMBER, "labels": [{"name": "loom:goal"}]})]


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

    def test_disk_low_under_60_minutes_files_nothing(self):
        """When disk_low has held for less than 60 minutes, no ask is filed."""
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
        st._free = int(0.5 * FLOOR)  # Below floor
        st._disk_free = lambda: st._free
        st._goal_homes = lambda: [(GOAL_REPO, {"number": GOAL_NUMBER, "labels": [{"name": "loom:goal"}]})]
        st._num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        st._goals = {}  # Required by _goal_homes

        # Disk has been low for 30 minutes (1800 seconds)
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 1800)

        # Call the disk guard ask check
        st._check_disk_guard_ask()

        # No ask should be filed
        self.assertEqual(len(st.gh.posted), 0, "No ask should be filed under 60 minutes")

    def test_disk_low_over_60_minutes_creates_ask(self):
        """When disk_low has held for 60+ minutes, one needs:human ask is created."""
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
        st._free = int(0.5 * FLOOR)  # Below floor
        st._disk_free = lambda: st._free
        st._goal_homes = lambda: [(GOAL_REPO, {"number": GOAL_NUMBER, "labels": [{"name": "loom:goal"}]})]
        st._num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        st._goals = {}  # Required by _goal_homes

        # Disk has been low for 61 minutes (3660 seconds)
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 3660)

        # Call the disk guard ask check
        st._check_disk_guard_ask()

        # One ask should be filed
        self.assertEqual(len(st.gh.posted), 1, "One ask should be filed after 60+ minutes")
        repo, number, body = st.gh.posted[0]
        self.assertEqual(repo, GOAL_REPO)
        self.assertEqual(number, GOAL_NUMBER)
        self.assertIn("Kind: hardware", body)
        self.assertIn("free", body.lower())
        self.assertIn("floor", body.lower())

    def test_ask_edited_in_place_while_paused(self):
        """The ask comment is edited in place (not a new comment) while disk stays low."""
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
        st._free = int(0.5 * FLOOR)
        st._disk_free = lambda: st._free
        st._goal_homes = lambda: [(GOAL_REPO, {"number": GOAL_NUMBER, "labels": [{"name": "loom:goal"}]})]
        st._num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        st._goals = {}  # Required by _goal_homes

        # Disk has been low for 61 minutes
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 3660)

        # First check creates the ask
        st._check_disk_guard_ask()
        first_posted = len(st.gh.posted)

        # Second check (later pass) should edit in place, not create new
        st.now = NOW + 3600  # One hour later
        st._check_disk_guard_ask()

        # Should still have only one ask (edited in place)
        self.assertEqual(len(st.gh.posted), first_posted, "Ask should be edited in place, not duplicated")

    def test_ask_closed_when_disk_recovers(self):
        """When disk recovers (free space above floor), the ask is closed."""
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
        st._free = int(0.5 * FLOOR)
        st._disk_free = lambda: st._free
        st._goal_homes = lambda: [(GOAL_REPO, {"number": GOAL_NUMBER, "labels": [{"name": "loom:goal"}]})]
        st._num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        st._goals = {}  # Required by _goal_homes

        # Disk has been low for 61 minutes - ask created
        self._write_disk_guard(True, int(0.5 * FLOOR), NOW - 3660)
        st._check_disk_guard_ask()

        # Now disk recovers
        st._free = int(2 * FLOOR)  # Above floor
        st._free_read = False  # Force re-read
        self._write_disk_guard(False, int(2 * FLOOR), NOW)

        # Next check should close the ask
        st._check_disk_guard_ask()

        # The needs:human label should be removed
        removed = [l for r, n, l in st.gh.labels_removed if "needs:human" in l]
        self.assertTrue(len(removed) > 0, "needs:human label should be removed when disk recovers")


if __name__ == "__main__":
    unittest.main()