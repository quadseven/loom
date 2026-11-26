"""The lane checks' shared uv directory helper (lanes/uv_cache.py, loom#3591):
the bound on one repo's cache, its lock, and its refusal of symlinks."""

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "lanes" / "uv_cache.py"


class UvCacheBoundTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.shared = self.tmp / "run" / "uv-shared"
        self.cache = self.shared / "repos" / "example_repo" / "cache"
        self.cache.mkdir(parents=True)
        (self.shared / "locks").mkdir()
        self.calls = self.tmp / "uv-calls"
        self.uv = self.tmp / "bin" / "uv"
        self.uv.parent.mkdir()
        # A stand-in uv: logs each call; prune drops the bulk when SHRINK
        # is set; clean empties the cache as uv does.
        self.uv.write_text(
            '#!/bin/sh\nprintf "%%s cache=%%s\\n" "$*" "$UV_CACHE_DIR" >> "%s"\n'
            '[ "$2" = prune ] && [ -n "$SHRINK" ] && rm -f "$UV_CACHE_DIR/bulk"\n'
            '[ "$2" = clean ] && rm -rf "$UV_CACHE_DIR"\nexit 0\n' % self.calls)
        self.uv.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def fill(self, kb, where=None):
        (where or self.cache).joinpath("bulk").write_bytes(b"x" * kb * 1024)

    def bound(self, max_mb=1, uv=True, shrink=False, key="example_repo"):
        args = [sys.executable, "-B", str(TOOL), "bound", str(self.shared), key, str(max_mb)]
        if uv:
            args.append(str(self.uv))
        result = subprocess.run(args, capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, SHRINK="1" if shrink else ""))
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.calls.read_text().splitlines() if self.calls.exists() else []), result.stderr

    def test_a_cache_under_its_bound_is_left_alone(self):
        self.fill(512)
        calls, _ = self.bound(max_mb=4)
        self.assertEqual(calls, [])
        self.assertTrue((self.cache / "bulk").exists())

    def test_a_cache_over_its_bound_is_pruned(self):
        self.fill(2048)
        calls, _ = self.bound(shrink=True)
        self.assertEqual(calls, ["cache prune cache=%s" % self.cache])
        self.assertTrue(self.cache.is_dir())

    def test_a_cache_still_over_its_bound_after_a_prune_is_cleaned(self):
        self.fill(2048)
        calls, _ = self.bound()
        self.assertEqual(calls, ["cache prune cache=%s" % self.cache, "cache clean cache=%s" % self.cache])
        self.assertTrue(self.cache.is_dir())
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_without_a_runner_uv_a_cache_over_its_bound_is_removed(self):
        self.fill(2048)
        calls, _ = self.bound(uv=False)
        self.assertEqual(calls, [])
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_symlinked_cache_is_refused_and_its_target_kept(self):
        # A check can write inside its cache; were the cache itself a link,
        # a prune or removal outside the sandbox would reach the target.
        target = self.tmp / "precious"
        target.mkdir()
        self.fill(2048, target)
        os.rmdir(self.cache)
        os.symlink(target, self.cache)
        calls, err = self.bound(uv=False)
        self.assertEqual(calls, [])
        self.assertTrue((target / "bulk").exists())
        self.assertIn("symlink", err)
        calls, _ = self.bound()
        self.assertEqual(calls, [])

    def test_a_symlink_anywhere_above_the_cache_is_refused(self):
        target = self.tmp / "elsewhere"
        (target / "cache").mkdir(parents=True)
        self.fill(2048, target / "cache")
        os.rename(self.cache.parent, self.tmp / "moved")
        os.symlink(target, self.cache.parent)
        calls, _ = self.bound(uv=False)
        self.assertEqual(calls, [])
        self.assertTrue((target / "cache" / "bulk").exists())

    def test_a_symlink_inside_the_cache_is_not_followed(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self.fill(2048, outside)
        os.symlink(outside, self.cache / "link")
        self.fill(1200)
        calls, _ = self.bound(uv=False, max_mb=1)
        self.assertTrue((outside / "bulk").exists())
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_cache_a_check_holds_is_not_bounded(self):
        # A check holds its cache's lock shared while it runs; the bound
        # takes it exclusively without waiting, so it skips the busy cache.
        self.fill(2048)
        fd = os.open(self.shared / "locks" / "example_repo.lock", os.O_RDONLY | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            calls, _ = self.bound(uv=False)
        finally:
            os.close(fd)
        self.assertEqual(calls, [])
        self.assertTrue((self.cache / "bulk").exists())
        self.bound(uv=False)
        self.assertFalse((self.cache / "bulk").exists())

    def test_a_key_that_leaves_the_repos_folder_is_refused(self):
        self.fill(2048)
        for key in ("..", "../example_repo", "a/b", ""):
            with self.subTest(key=key):
                calls, err = self.bound(uv=False, key=key)
                self.assertIn("refusing key", err)
        self.assertTrue((self.cache / "bulk").exists())

    def test_shared_holds_the_lock_while_the_command_runs(self):
        lock = self.shared / "locks" / "example_repo.lock"
        probe = ("import fcntl, os, sys\n"
                 "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
                 "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); print('free')\n"
                 "except OSError:\n    print('held')\n")
        result = subprocess.run([sys.executable, "-B", str(TOOL), "shared", str(lock), "--",
                                 sys.executable, "-c", probe, str(lock)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "held")


if __name__ == "__main__":
    unittest.main()
