"""The disk watchdog's folder-size report cannot run past its budget, and a
folder walk stays on the filesystem it starts in."""

import os
import tempfile
import time
import unittest
from unittest import mock

from bus import disk_watchdog as wd


class Moved:
    """A stat result that reads as another device."""

    def __init__(self, st):
        self._st = st
        self.st_dev = st.st_dev + 1

    def __getattr__(self, name):
        return getattr(self._st, name)


class ScanBoundTest(unittest.TestCase):
    def test_a_blocked_folder_read_does_not_hold_the_report(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("stuck", "fine"):
                os.mkdir(os.path.join(root, name))
                with open(os.path.join(root, name, "f"), "wb") as fh:
                    fh.write(b"x" * 10)

            def measure(path, deadline):
                if path.endswith("stuck"):
                    time.sleep(30)
                return 7

            started = time.monotonic()
            found = wd.RealDisk().largest(root, set(), started + 1, measure)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual([os.path.basename(p) for p, _ in found], ["fine"])

    def test_a_walk_does_not_count_files_under_another_mount(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "mnt"))
            for rel in ("own", os.path.join("mnt", "far")):
                with open(os.path.join(root, rel), "wb") as fh:
                    fh.write(b"x" * 8192)
            real = os.lstat

            def lstat(path, *args, **kwargs):
                st = real(path, *args, **kwargs)
                return Moved(st) if os.sep + "mnt" in path else st

            whole = wd._measure(root, time.monotonic() + 60)
            with mock.patch.object(os, "lstat", lstat):
                got = wd._measure(root, time.monotonic() + 60)
            far = real(os.path.join(root, "mnt", "far"))
            self.assertEqual(whole - got, wd._bytes(far) + wd._bytes(real(os.path.join(root, "mnt"))))


if __name__ == "__main__":
    unittest.main()
