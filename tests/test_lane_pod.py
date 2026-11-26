"""lanes/k8s/pod.sh, the lane pod's entrypoint (docs/LANES.md, "Lane pods"):
it starts lanes/run_lane.sh for each slot, as launchd does on the Mac, with
the pod's defaults. A fake run_lane.sh beside a copy of the script records
each start."""

import os
import shutil
import signal
import subprocess
import tempfile
import time
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

    def test_a_drain_mark_left_by_an_earlier_container_is_cleared(self):
        # run/ outlives a container restart within the pod; a mark left there
        # would keep the new container's runners from taking any brief.
        run = self.home / "run"
        run.mkdir(parents=True)
        (run / "zen.drain").touch()
        (run / "codex.drain").touch()
        result = self.pod(LOOM_POD_SLOTS="zen")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((run / "zen.drain").exists())
        self.assertTrue((run / "codex.drain").exists())

    # The drain: a stopping pod marks run/<slot>.drain so each
    # runner takes no new brief, waits LOOM_POD_DRAIN_SECONDS for the runs in
    # flight, then stops each agent still running (the process group in
    # run/<slot>.lock/pgid) and waits LOOM_POD_PRESERVE_SECONDS for its runner
    # to keep the run before the pod exits.
    DRAIN_RUNNER = """#!/usr/bin/env bash
slot="$1"
mkdir -p "$LOOM_HOME/run/$slot.lock"
set -m
sleep "$FAKE_AGENT_SECONDS" &
agent=$!
set +m
echo "$agent" > "$LOOM_HOME/run/$slot.lock/pgid"
echo "start $slot" >> "$FAKE_LOG"
wait "$agent"
status=$?
[ ! -e "$LOOM_HOME/run/$slot.drain" ] || echo "drain-seen $slot" >> "$FAKE_LOG"
echo "agent-exit $slot $status" >> "$FAKE_LOG"
"""

    def stop_pod_mid_run(self, timeout=30, **env):
        (self.root / "lanes" / "run_lane.sh").write_text(self.DRAIN_RUNNER)
        full = {"PATH": os.environ["PATH"], "LOOM_HOME": str(self.home), "FAKE_LOG": str(self.log),
                "LOOM_POD_SLOTS": "zen", "LOOM_POD_INTERVAL": "3600", **env}
        proc = subprocess.Popen(["bash", str(self.root / "lanes" / "k8s" / "pod.sh")], env=full,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.log.exists() and "start zen" in self.log.read_text()):
                self.assertLess(time.monotonic(), deadline, "the runner never started")
                time.sleep(0.05)
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=timeout)
            return proc.returncode, time.monotonic() - started
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_a_stopping_pod_lets_a_run_that_ends_in_time_finish(self):
        code, _ = self.stop_pod_mid_run(FAKE_AGENT_SECONDS="2", LOOM_POD_DRAIN_SECONDS="20")
        self.assertEqual(code, 143)
        lines = self.log.read_text().splitlines()
        self.assertIn("drain-seen zen", lines)
        self.assertIn("agent-exit zen 0", lines)
        self.assertTrue((self.home / "run" / "zen.drain").exists())

    def test_a_stopping_pod_stops_an_agent_past_the_bound_and_lets_its_runner_keep_the_run(self):
        code, took = self.stop_pod_mid_run(FAKE_AGENT_SECONDS="60", LOOM_POD_DRAIN_SECONDS="1",
                                           LOOM_POD_PRESERVE_SECONDS="20")
        self.assertEqual(code, 143)
        lines = self.log.read_text().splitlines()
        # The agent got SIGTERM (128 + 15) and its runner went on to the end.
        self.assertIn("agent-exit zen 143", lines)
        self.assertIn("drain-seen zen", lines)
        self.assertLess(took, 15)

    # A pod fed through the state store (bus/lane_feed.py, loom#3621): each
    # round first pushes the slots' heartbeats and reports and takes their
    # next briefs, then starts the runners. A fake module records the call.
    FAKE_FEED = """import os, sys
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("feed %s\\n" % " ".join(sys.argv[1:]))
"""

    def with_fake_feed(self):
        bus = self.root / "bus"
        bus.mkdir()
        (bus / "__init__.py").write_text("")
        (bus / "lane_feed.py").write_text(self.FAKE_FEED)

    def test_a_fed_pod_syncs_its_slots_before_each_round(self):
        self.with_fake_feed()
        result = self.pod(LOOM_POD_SLOTS="zen opencode-go-5", LOOM_POD_FEED="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.log.read_text().splitlines()
        self.assertEqual(lines[0], "feed sync zen opencode-go-5")
        self.assertEqual(sorted(lines[1:]), ["opencode-go-5 sandbox=bwrap drop=1",
                                             "zen sandbox=bwrap drop=1"])

    def test_a_pod_not_fed_runs_no_sync(self):
        self.with_fake_feed()
        result = self.pod(LOOM_POD_SLOTS="zen")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("feed", self.log.read_text())

    def test_bad_settings_start_nothing(self):
        for env in ({}, {"LOOM_POD_SLOTS": "zen ../x"}, {"LOOM_POD_SLOTS": "zen", "LOOM_POD_INTERVAL": "0"},
                    {"LOOM_POD_SLOTS": "zen", "LOOM_POD_DRAIN_SECONDS": "x"},
                    {"LOOM_POD_SLOTS": "zen", "LOOM_POD_PRESERVE_SECONDS": "-1"}):
            with self.subTest(env=env):
                result = self.pod(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())

    # The sign-in probe (lanes/signin_probe.sh, loom#3572): one per sign-in
    # lane with a slot here, covering every slot of that lane.
    FAKE_PROBE = """#!/usr/bin/env bash
echo "probe $*" >> "$FAKE_LOG"
"""

    def with_fake_probe(self):
        probe = self.root / "lanes" / "signin_probe.sh"
        probe.write_text(self.FAKE_PROBE)
        probe.chmod(0o755)

    def test_one_round_probes_each_sign_in_lane_over_all_its_slots(self):
        self.with_fake_probe()
        result = self.pod(LOOM_POD_SLOTS="codex codex-2 codex-12 zen")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.log.read_text().splitlines()
        self.assertEqual([line for line in lines if line.startswith("probe")],
                         ["probe codex codex-2 codex-12"])

    def test_no_probe_for_api_lanes_or_when_turned_off(self):
        self.with_fake_probe()
        for env in ({"LOOM_POD_SLOTS": "zen opencode-go-5"},
                    {"LOOM_POD_SLOTS": "claude", "LOOM_SIGNIN_PROBE_INTERVAL": "0"},
                    {"LOOM_POD_SLOTS": "claude", "LOOM_SIGNIN_LANES": ""}):
            with self.subTest(env=env):
                if self.log.exists():
                    self.log.unlink()
                result = self.pod(**env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(any(line.startswith("probe")
                                     for line in self.log.read_text().splitlines()))


if __name__ == "__main__":
    unittest.main()
