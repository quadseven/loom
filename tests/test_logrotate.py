from __future__ import annotations

import gzip
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import logrotate  # noqa: E402

NOW = 1_800_000_000.0


def nobody(path):
    return False


def everybody(path):
    return True


class RotateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.said = []

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, text):
        with open(self.path(name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def rotate(self, now=NOW, limit=100, keep=2, writers=nobody):
        logrotate.rotate(self.dir, limit, keep, now, self.said.append, writers=writers)

    def packed(self, name="station.log"):
        return sorted(n for n in os.listdir(self.dir)
                      if n.startswith(name + ".") and n.endswith(".gz"))

    def test_a_log_past_the_limit_is_rotated_and_compressed(self):
        text = "a line\n" * 100
        self.write("station.log", text)
        self.rotate()
        self.assertFalse(os.path.exists(self.path("station.log")))
        names = self.packed()
        self.assertEqual(len(names), 1)
        with gzip.open(self.path(names[0]), "rt", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), text)
        self.assertEqual(os.listdir(self.dir), names)

    def test_a_log_under_the_limit_is_left_alone(self):
        self.write("station.log", "short\n")
        self.rotate()
        self.assertEqual(os.listdir(self.dir), ["station.log"])

    def test_only_logs_at_the_top_are_rotated(self):
        os.mkdir(self.path("codex"))
        self.write(os.path.join("codex", "example.repo-1.log"), "x" * 500)
        self.write("events.jsonl", "x" * 500)
        self.rotate()
        self.assertEqual(sorted(os.listdir(self.dir)), ["codex", "events.jsonl"])
        self.assertEqual(os.listdir(self.path("codex")), ["example.repo-1.log"])

    def test_a_log_that_a_service_never_reopens_is_not_rotated(self):
        # These two run under KeepAlive: launchd opens the path once, so a
        # renamed log would never get a successor.
        self.write("viewer.log", "x" * 500)
        self.write("dashboard.log", "x" * 500)
        self.rotate()
        self.assertEqual(sorted(os.listdir(self.dir)), ["dashboard.log", "viewer.log"])

    def test_a_link_is_never_followed(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(os.unlink, outside.name)
        outside.write(b"x" * 500)
        outside.close()
        os.symlink(outside.name, self.path("station.log"))
        self.rotate()
        self.assertEqual(os.listdir(self.dir), ["station.log"])

    def test_only_the_newest_rotations_are_kept_for_each_log(self):
        for step in range(4):
            self.write("station.log", "x" * 500)
            self.write("codex-service.log", "y" * 500)
            self.rotate(now=NOW + step * 3600)
        self.assertEqual(len(self.packed()), 2)
        self.assertEqual(len(self.packed("codex-service.log")), 2)
        self.assertTrue(self.packed()[-1].startswith("station.log.20270115T1"))

    def test_lines_an_open_writer_adds_after_the_rename_are_kept(self):
        self.write("station.log", "x" * 500)
        writer = open(self.path("station.log"), "a", encoding="utf-8")
        self.addCleanup(writer.close)
        self.rotate(writers=everybody)
        self.assertEqual(self.packed(), [])
        writer.write("the last line\n")
        writer.flush()
        rotated = [n for n in os.listdir(self.dir)]
        self.assertEqual(len(rotated), 1)
        self.rotate(now=NOW + 3600, writers=everybody)
        self.assertEqual(os.listdir(self.dir), rotated)
        writer.write("one more\n")
        writer.close()
        self.rotate(now=NOW + 7200)
        with gzip.open(self.path(self.packed()[0]), "rt", encoding="utf-8") as fh:
            self.assertTrue(fh.read().endswith("the last line\none more\n"))

    def test_a_writer_that_adds_during_compression_keeps_the_file(self):
        self.write("station.log", "x" * 500)
        self.rotate(writers=everybody)
        rotated = os.listdir(self.dir)[0]

        def late_writer(path):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("late\n")
            return False

        real = logrotate._gzip
        def racing(path, target):
            real(path, target)
            late_writer(path)
        logrotate._gzip = racing
        self.addCleanup(setattr, logrotate, "_gzip", real)
        self.rotate(now=NOW + 60)
        self.assertEqual(os.listdir(self.dir), [rotated])
        logrotate._gzip = real
        self.rotate(now=NOW + 120)
        with gzip.open(self.path(self.packed()[0]), "rt", encoding="utf-8") as fh:
            self.assertTrue(fh.read().endswith("late\n"))

    @unittest.skipUnless(shutil.which("lsof"), "needs lsof")
    def test_a_real_open_file_is_seen_and_a_closed_one_is_not(self):
        self.write("station.log.20270115T000000Z", "x\n")
        path = self.path("station.log.20270115T000000Z")
        self.assertFalse(logrotate.has_writer(path))
        with open(path, "a", encoding="utf-8"):
            self.assertTrue(logrotate.has_writer(path))

    def test_a_writer_check_that_cannot_run_counts_as_a_writer(self):
        real = logrotate.LSOF
        logrotate.LSOF = self.path("no-such-tool")
        self.addCleanup(setattr, logrotate, "LSOF", real)
        self.assertTrue(logrotate.has_writer(self.path("anything")))


if __name__ == "__main__":
    unittest.main()
