"""lanes/mac/install.sh run as a process: the station lock wait is exercised
with a fake launchctl and a throwaway HOME/LOOM_HOME."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL = REPO_ROOT / "lanes" / "mac" / "install.sh"
LOCK_SH = REPO_ROOT / "lanes" / "mac" / "lock.sh"

# Fake launchctl: logs every call; a job is never found (print fails), so
# wait_job_gone returns at once and bootout/bootstrap are no-ops.
FAKE_LAUNCHCTL = """#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> "$FAKE_LOG"
[ "${1:-}" = print ] && exit 1
exit 0
"""


class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.loom = self.tmp / "loom-home"
        self.checkout = self.tmp / "checkout"
        (self.checkout / "lanes" / "mac").mkdir(parents=True)
        shutil.copy(INSTALL, self.checkout / "lanes" / "mac" / "install.sh")
        shutil.copy(LOCK_SH, self.checkout / "lanes" / "mac" / "lock.sh")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "launchctl"
        fake.write_text(FAKE_LAUNCHCTL)
        fake.chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.lock = self.loom / "state" / "station.lock"

    def write_lock(self, pid):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("%s\n" % pid)

    def run_install(self, **env):
        full = dict(os.environ,
                    PATH="%s:/usr/bin:/bin" % self.bin,
                    HOME=str(self.home),
                    LOOM_HOME=str(self.loom),
                    LOOM_REPO_OWNERS="example-owner",
                    LOOM_CODEX_MODEL="codex-test",
                    FAKE_LOG=str(self.log),
                    **env)
        return subprocess.run(
            ["bash", str(self.checkout / "lanes" / "mac" / "install.sh"), "codex"],
            env=full, capture_output=True, text=True, timeout=60)

    def test_a_held_lock_is_waited_for_then_the_station_installs(self):
        # A live pid (this test process) holds the lock; a helper frees it
        # after a moment, so the installer must wait, then install.
        self.write_lock(os.getpid())
        releaser = subprocess.Popen(
            ["bash", "-c", "sleep 0.6; echo 99999999 > \"$1\"", "rel", str(self.lock)])
        self.addCleanup(releaser.wait)
        start = time.time()
        result = self.run_install(LOOM_STATION_WAIT="100")
        elapsed = time.time() - start
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertNotIn("installing anyway", result.stdout)
        self.assertIn("installed com.loom.station", result.stdout)

    def test_the_bound_is_announced_and_the_install_proceeds(self):
        self.write_lock(os.getpid())
        result = self.run_install(LOOM_STATION_WAIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("installing anyway", result.stdout)
        self.assertIn("installed com.loom.station", result.stdout)

    def test_a_free_lock_does_not_wait(self):
        self.write_lock(99999999)
        sleep_log = self.tmp / "sleep.log"
        fake_sleep = self.bin / "sleep"
        fake_sleep.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$SLEEP_LOG\"\n")
        fake_sleep.chmod(0o755)
        result = self.run_install(LOOM_STATION_WAIT="100", SLEEP_LOG=str(sleep_log))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sleep_log.exists(), "the installer waited despite a free lock")
        self.assertNotIn("installing anyway", result.stdout)
        self.assertIn("installed com.loom.station", result.stdout)

    def station_interval(self):
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        return plist.split("<key>StartInterval</key><integer>", 1)[1].split("<", 1)[0]

    def test_the_station_interval_defaults_to_a_minute(self):
        env = {k: v for k, v in os.environ.items() if k != "LOOM_STATION_INTERVAL"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.station_interval(), "60")

    def test_the_station_interval_comes_from_the_environment(self):
        result = self.run_install(LOOM_STATION_INTERVAL="180")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.station_interval(), "180")

    def test_station_path_prefers_the_lanes_opencode_install(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_REWRITE_OPENCODE</key><string>%s/.opencode/bin/opencode</string>"
                      % self.home, plist)

    def test_pin_targets_are_written_to_station_plist(self):
        result = self.run_install(LOOM_PIN_TARGETS='[{"repo":"example-owner/deploy"}]')
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn('<key>LOOM_PIN_TARGETS</key><string>[{"repo":"example-owner/deploy"}]</string>', plist)

    def test_a_bad_station_interval_installs_nothing(self):
        for bad in ("soon", "30"):
            with self.subTest(bad=bad):
                result = self.run_install(LOOM_STATION_INTERVAL=bad)
                self.assertEqual(result.returncode, 1)
                self.assertIn("LOOM_STATION_INTERVAL", result.stderr)
                self.assertFalse((self.home / "Library" / "LaunchAgents").exists())


if __name__ == "__main__":
    unittest.main()
