"""The disk watchdog (loom#3187): below its trigger it clears Loom's own data
in a fixed order and stops once the disk has room again. A fake filesystem
and a fake free-space reader drive the order and the stop; real folders
prove it never removes anything outside the Loom-owned roots."""

import os
import shutil
import tarfile
import tempfile
import unittest

from bus import disk_watchdog as wd

GB = 1024 ** 3
DAY = 86400
NOW = 1_800_000_000.0
HOME = "/srv/loom"


class FakeDisk:
    """A filesystem held in a dict: path -> (is_dir, newest, bytes, link).
    Free space is a fixed capacity less what the entries hold."""

    def __init__(self, capacity: int, used_elsewhere: int = 0):
        self.capacity = capacity
        self.elsewhere = used_elsewhere
        self.entries = {}
        self.calls = []
        self.mounted = set()
        self.fail_verify = set()
        self.sizes = {}

    def add(self, path, size=0, age_days=0.0, is_dir=False, link=None):
        parent = os.path.dirname(path)
        while parent and parent != "/" and parent not in self.entries:
            self.entries[parent] = (True, NOW, 0, None)
            parent = os.path.dirname(parent)
        self.entries[path] = (is_dir, NOW - age_days * DAY, size, link)

    def _used(self):
        return self.elsewhere + sum(e[2] for e in self.entries.values())

    def free(self, path):
        return self.capacity - self._used()

    def listdir(self, folder):
        prefix = folder.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/", 1)[0] for p in self.entries
                       if p.startswith(prefix)})

    def info(self, path):
        if path not in self.entries:
            return None
        is_dir, newest, size, link = self.entries[path]
        total = size + sum(e[2] for p, e in self.entries.items() if p.startswith(path + "/"))
        return wd.Info(is_dir=is_dir, is_link=link is not None, newest=newest, size=total)

    def islink(self, path):
        entry = self.entries.get(path)
        return bool(entry and entry[3])

    def realpath(self, path):
        parts = path.split("/")
        for i in range(len(parts), 0, -1):
            head = "/".join(parts[:i])
            entry = self.entries.get(head)
            if entry and entry[3]:
                return self.realpath("/".join([entry[3]] + parts[i:]))
        return path

    def _drop(self, path):
        for p in [p for p in self.entries if p == path or p.startswith(path + "/")]:
            del self.entries[p]

    def remove(self, path):
        self.calls.append(("remove", path))
        self._drop(path)

    def archive(self, src, dest):
        self.calls.append(("archive", src, dest))
        if src in self.fail_verify:
            raise OSError("archive of %s did not verify" % src)
        size = self.info(src).size
        self._drop(src)
        if not any(dest.startswith(m + "/") for m in self.mounted):
            self.entries[dest] = (False, NOW, size // 4, None)

    def usable(self, folder):
        return folder in self.mounted

    def largest(self, root, skip, deadline):
        return sorted(self.sizes.items(), key=lambda kv: -kv[1])


def roots(trees=(HOME + "/worktrees",), lanes=("codex", "spark"), live=(), idle=("codex", "spark")):
    return wd.Roots(home=HOME, trees=tuple(trees), lanes=tuple(lanes),
                    live=frozenset(live), idle=frozenset(idle))


def tend(disk, cfg=None, saved=None, floor=15 * GB, rts=None, now=NOW):
    said = []
    out = wd.tend(cfg or wd.Config(), rts or roots(), disk, floor, now, said.append,
                  dict(saved or {}))
    return out, said


class ConfigTest(unittest.TestCase):
    def test_defaults_need_no_setting(self):
        cfg = wd.Config.from_env({})
        self.assertEqual(cfg.trigger_gb, 25.0)
        self.assertEqual(cfg.archive_dir, "")
        self.assertEqual(cfg.log_days, 7)
        self.assertEqual(cfg.tree_days, 2)
        self.assertEqual(cfg.run_days, 2)

    def test_settings_are_read_and_bad_ones_ignored(self):
        cfg = wd.Config.from_env({"LOOM_DISK_WATCHDOG_GB": "40", "LOOM_DISK_ARCHIVE_DIR": "/mnt/a",
                                  "LOOM_DISK_RUN_DAYS": "5"})
        self.assertEqual((cfg.trigger_gb, cfg.archive_dir, cfg.run_days), (40.0, "/mnt/a", 5))
        cfg = wd.Config.from_env({"LOOM_DISK_WATCHDOG_GB": "lots", "LOOM_DISK_RUN_DAYS": "-1"})
        self.assertEqual((cfg.trigger_gb, cfg.run_days), (25.0, 2))


class TendTest(unittest.TestCase):
    def test_above_the_trigger_touches_nothing(self):
        disk = FakeDisk(100 * GB)
        disk.add(HOME + "/logs/codex/old.log", GB, age_days=30)
        _, said = tend(disk)
        self.assertEqual(disk.calls, [])
        self.assertEqual(said, [])

    def test_waits_between_runs(self):
        disk = FakeDisk(30 * GB, used_elsewhere=10 * GB)
        disk.add(HOME + "/logs/codex/old.log", 2 * GB, age_days=30)
        tend(disk, saved={"at": NOW - 60})
        self.assertEqual(disk.calls, [])

    def test_clears_in_order_and_stops_once_above_the_trigger(self):
        disk = FakeDisk(40 * GB, used_elsewhere=10 * GB)
        disk.add(HOME + "/logs/codex/a.log", 2 * GB, age_days=9)
        disk.add(HOME + "/logs/codex/new.log", 2 * GB, age_days=1)
        disk.add(HOME + "/run/codex-check", 2 * GB, age_days=3, is_dir=True)
        disk.add(HOME + "/worktrees/quadseven.loom-1", 3 * GB, age_days=3, is_dir=True)
        disk.add(HOME + "/run/codex-home/.npm", GB, is_dir=True)
        # 40 - 10 - 10 = 20 GB free, under 25: logs, then run, then the tree.
        out, said = tend(disk)
        kinds = [c[0] + ":" + c[1].rsplit("/", 1)[-1] for c in disk.calls]
        self.assertEqual(kinds, ["archive:a.log", "remove:codex-check", "archive:quadseven.loom-1"])
        self.assertIn(HOME + "/run/codex-home/.npm", disk.entries)
        self.assertIn(HOME + "/logs/codex/new.log", disk.entries)
        self.assertGreaterEqual(disk.free(HOME), 25 * GB)
        self.assertTrue(any(s.startswith("disk watchdog: logs older than 7 days") for s in said), said)
        self.assertTrue(any("freed" in s for s in said), said)
        self.assertIsNone(out.get("notice"))

    def test_logs_compress_in_place_without_an_archive(self):
        disk = FakeDisk(40 * GB, used_elsewhere=20 * GB)
        disk.add(HOME + "/logs/codex/a.log", 4 * GB, age_days=9)
        disk.add(HOME + "/logs/codex/b.log.gz", GB, age_days=9)
        tend(disk)
        self.assertIn(("archive", HOME + "/logs/codex/a.log", HOME + "/logs/codex/a.log.gz"),
                      disk.calls)
        self.assertFalse(any(c[1].endswith("b.log.gz") for c in disk.calls), disk.calls)

    def test_logs_go_to_the_archive_when_it_is_mounted(self):
        disk = FakeDisk(40 * GB, used_elsewhere=20 * GB)
        disk.mounted.add("/mnt/archive")
        disk.add(HOME + "/logs/codex/a.log", 4 * GB, age_days=9)
        tend(disk, cfg=wd.Config(archive_dir="/mnt/archive"))
        dest = disk.calls[0][2]
        self.assertTrue(dest.startswith("/mnt/archive/"), dest)
        self.assertTrue(dest.endswith("/logs/codex/a.log.gz"), dest)

    def test_an_unmounted_archive_falls_back_to_compressing_in_place(self):
        disk = FakeDisk(40 * GB, used_elsewhere=20 * GB)
        disk.add(HOME + "/logs/codex/a.log", 4 * GB, age_days=9)
        tend(disk, cfg=wd.Config(archive_dir="/mnt/archive"))
        self.assertEqual(disk.calls[0][2], HOME + "/logs/codex/a.log.gz")

    def test_live_briefs_and_busy_lanes_are_kept(self):
        disk = FakeDisk(40 * GB, used_elsewhere=30 * GB)
        disk.add(HOME + "/logs/codex/quadseven.loom-7-r2.log", GB, age_days=9)
        disk.add(HOME + "/worktrees/quadseven.loom-7-r2", GB, age_days=9, is_dir=True)
        disk.add(HOME + "/run/spark-check", GB, age_days=9, is_dir=True)
        disk.add(HOME + "/run/spark-home/.cache", GB, is_dir=True)
        tend(disk, rts=roots(live={"quadseven.loom-7-r2"}, idle={"codex"}))
        self.assertEqual(disk.calls, [])

    def test_caches_of_idle_lanes_go_last(self):
        disk = FakeDisk(40 * GB, used_elsewhere=20 * GB)
        disk.add(HOME + "/run/codex-home/.npm", 3 * GB, is_dir=True)
        disk.add(HOME + "/run/codex-home/Library/Caches", 3 * GB, is_dir=True)
        disk.add(HOME + "/run/codex-home/.config", 3 * GB, is_dir=True)
        tend(disk)
        removed = [c[1] for c in disk.calls if c[0] == "remove"]
        self.assertIn(HOME + "/run/codex-home/.npm", removed)
        self.assertNotIn(HOME + "/run/codex-home/.config", removed)

    def test_a_tree_that_does_not_verify_is_kept(self):
        disk = FakeDisk(40 * GB, used_elsewhere=30 * GB)
        tree = HOME + "/worktrees/quadseven.loom-1"
        disk.add(tree, GB, age_days=3, is_dir=True)
        disk.fail_verify.add(tree)
        _, said = tend(disk)
        self.assertIn(tree, disk.entries)
        self.assertTrue(any("did not verify" in s for s in said), said)

    def test_never_touches_a_path_outside_the_roots(self):
        disk = FakeDisk(40 * GB, used_elsewhere=35 * GB)
        disk.add("/data/personal", 5 * GB, age_days=99, is_dir=True)
        disk.add(HOME + "/logs/codex/escape", 0, age_days=99, link="/data/personal")
        disk.add(HOME + "/worktrees", 0, is_dir=True, link="/data")
        disk.add(HOME + "/run/codex-home/.cache", 0, link="/data/personal")
        tend(disk, rts=roots(trees=(HOME + "/worktrees", "/data")))
        self.assertIn("/data/personal", disk.entries)
        for call in disk.calls:
            self.assertTrue(call[1].startswith(HOME + "/"), call)
            self.assertFalse(call[1].startswith("/data"), call)

    def test_reports_once_an_hour_when_the_floor_is_out_of_reach(self):
        disk = FakeDisk(40 * GB, used_elsewhere=30 * GB)
        disk.sizes = {"/srv/big": 20 * GB, "/srv/small": GB}
        out, said = tend(disk)
        line = [s for s in said if "largest folders outside loom" in s]
        self.assertEqual(len(line), 1, said)
        self.assertIn("big 20.0 GB", line[0])
        self.assertTrue(out["notice"].startswith("Disk watchdog:"), out)
        self.assertNotIn("/srv", out["notice"])
        _, said = tend(disk, saved=out, now=NOW + 600)
        self.assertFalse(any("largest folders" in s for s in said), said)
        _, said = tend(disk, saved=out, now=NOW + 3700)
        self.assertTrue(any("largest folders" in s for s in said), said)

    def test_the_notice_clears_once_above_the_floor(self):
        disk = FakeDisk(40 * GB, used_elsewhere=20 * GB)
        out, _ = tend(disk, saved={"notice": "Disk watchdog: old", "reported": NOW - 60})
        self.assertIsNone(out.get("notice"))

    def test_off_at_zero(self):
        disk = FakeDisk(20 * GB, used_elsewhere=10 * GB)
        disk.add(HOME + "/logs/codex/a.log", GB, age_days=9)
        tend(disk, cfg=wd.Config(trigger_gb=0))
        self.assertEqual(disk.calls, [])


class RealDiskTest(unittest.TestCase):
    """The real filesystem: archives verify before the source goes, and no
    removal reaches through a link to a folder outside the roots."""

    def setUp(self):
        # macOS keeps temp folders behind the /var link: a link above the
        # roots is allowed, one inside them is not.
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "loom")
        self.outside = os.path.join(self.tmp, "personal")
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "keep.txt"), "w") as fh:
            fh.write("mine")
        for d in ("logs/codex", "run/codex-home", "worktrees/quadseven.loom-1/sub"):
            os.makedirs(os.path.join(self.home, d))

    def _old(self, path):
        os.utime(path, (NOW - 30 * DAY, NOW - 30 * DAY), follow_symlinks=False)

    def test_archive_of_a_folder_verifies_and_removes_the_source(self):
        tree = os.path.join(self.home, "worktrees", "quadseven.loom-1")
        with open(os.path.join(tree, "sub", "f.txt"), "w") as fh:
            fh.write("x" * 1000)
        os.symlink(self.outside, os.path.join(tree, "out"))
        dest = os.path.join(self.tmp, "arch", "t1.tar.gz")
        wd.RealDisk().archive(tree, dest)
        self.assertFalse(os.path.exists(tree))
        self.assertEqual(os.listdir(self.outside), ["keep.txt"])
        with tarfile.open(dest) as tf:
            self.assertIn("quadseven.loom-1/sub/f.txt", tf.getnames())
            self.assertTrue(tf.getmember("quadseven.loom-1/out").issym())

    def test_archive_of_a_file_verifies_and_removes_the_source(self):
        log = os.path.join(self.home, "logs", "codex", "a.log")
        with open(log, "w") as fh:
            fh.write("line\n" * 100)
        wd.RealDisk().archive(log, log + ".gz")
        self.assertFalse(os.path.exists(log))
        self.assertTrue(os.path.exists(log + ".gz"))

    def test_a_compressed_log_is_copied_as_is_and_keeps_its_time(self):
        log = os.path.join(self.home, "logs", "codex", "a.log.gz")
        with open(log, "wb") as fh:
            fh.write(b"\x1f\x8b already compressed")
        self._old(log)
        dest = os.path.join(self.tmp, "arch", "loom", "logs", "codex", "a.log.gz")
        wd.RealDisk().archive(log, dest)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"\x1f\x8b already compressed")
        self.assertAlmostEqual(os.lstat(dest).st_mtime, NOW - 30 * DAY, delta=1)
        self.assertFalse(os.path.exists(log))

    def test_links_to_outside_are_never_followed(self):
        links = [os.path.join(self.home, "logs", "codex", "escape"),
                 os.path.join(self.home, "run", "codex-home", ".cache"),
                 os.path.join(self.home, "worktrees", "quadseven.loom-2"),
                 os.path.join(self.home, "worktrees", "quadseven.loom-1", "sub", "out")]
        for link in links:
            os.symlink(self.outside, link)
            self._old(link)
        tree = os.path.join(self.home, "worktrees", "quadseven.loom-1")
        self._old(os.path.join(tree, "sub"))
        self._old(tree)
        disk = wd.RealDisk()
        disk.free = lambda path: 0
        rts = wd.Roots(home=self.home, trees=(os.path.join(self.home, "worktrees"),),
                       lanes=("codex",), live=frozenset(), idle=frozenset({"codex"}))
        wd.tend(wd.Config(), rts, disk, 15 * GB, NOW, lambda s: None, {})
        self.assertEqual(os.listdir(self.outside), ["keep.txt"])
        self.assertFalse(os.path.exists(tree))
        for link in links[:3]:
            self.assertTrue(os.path.islink(link), link)


if __name__ == "__main__":
    unittest.main()
