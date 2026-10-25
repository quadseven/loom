"""lanes/k8s/pod.sh, the lane pod's entrypoint (docs/LANES.md, "Lane pods"):
it starts lanes/run_lane.sh for each slot, as launchd does on the Mac, with
the pod's defaults. A fake run_lane.sh beside a copy of the script records
each start."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

POD = Path(__file__).resolve().parent.parent / "lanes" / "k8s" / "pod.sh"

FAKE_RUNNER = """#!/usr/bin/env bash
printf '%s sandbox=%s drop=%s\\n' "$1" "$LOOM_SANDBOX" "$LOOM_DROP_TREES" >> "$FAKE_LOG"
echo "runner output for $1"
"""


class LanePodTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "lanes" / "k8s").mkdir(parents=True)
        shutil.copy(POD, self.root / "lanes" / "k8s" / "pod.sh")
        runner = self.root / "lanes" / "run_lane.sh"
        runner.write_text(FAKE_RUNNER)
        runner.chmod(0o755)
        self.home = self.root / "loom"
        self.log = self.root / "starts.log"

    def tearDown(self):
        self._tmp.cleanup()

    def pod(self, **env):
        full = {"PATH": os.environ["PATH"], "LOOM_HOME": str(self.home), "FAKE_LOG": str(self.log),
                "LOOM_POD_ONCE": "1", **env}
        return subprocess.run(["bash", str(self.root / "lanes" / "k8s" / "pod.sh")], env=full,
                              capture_output=True, text=True, timeout=30)

    def test_one_round_starts_each_slot_with_the_pod_defaults(self):
        result = self.pod(LOOM_POD_SLOTS="opencode-go-5 zen")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(self.log.read_text().splitlines()),
                         ["opencode-go-5 sandbox=bwrap drop=1", "zen sandbox=bwrap drop=1"])
        self.assertIn("runner output for zen",
                      (self.home / "logs" / "zen-service.log").read_text())

    def test_a_lock_left_by_an_earlier_container_is_cleared(self):
        # A restarted container reuses pids from 1: an old lock could name a
        # live, unrelated process and hold the slot for good.
        stale = self.home / "run" / "zen.lock"
        stale.mkdir(parents=True)
        (stale / "pid").write_text("1\n")
        other = self.home / "run" / "codex.lock"
        other.mkdir()
        result = self.pod(LOOM_POD_SLOTS="zen")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertTrue(other.exists())

    def test_settings_in_the_environment_win_over_the_defaults(self):
        result = self.pod(LOOM_POD_SLOTS="zen", LOOM_SANDBOX="seatbelt", LOOM_DROP_TREES="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), ["zen sandbox=seatbelt drop=0"])

    def test_bad_settings_start_nothing(self):
        for env in ({}, {"LOOM_POD_SLOTS": "zen ../x"}, {"LOOM_POD_SLOTS": "zen", "LOOM_POD_INTERVAL": "0"}):
            with self.subTest(env=env):
                result = self.pod(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
