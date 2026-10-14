"""lanes/mac/brain.sh run as a process: a copy of the script in a temp repo
whose origin is a local bare repo, and fake python3 (the bus lock) and
claude commands on PATH that record their calls."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "lanes" / "mac" / "brain.sh"

# Fake python3: `-m bus lock` exits $FAKE_LOCK (default 0), `-m bus unlock`
# exits 0. Every call is appended to $FAKE_LOG.
FAKE_PYTHON = """#!/usr/bin/env bash
printf 'python3 %s\\n' "$*" >> "$FAKE_LOG"
if [ "$3" = lock ]; then
  # FAKE_LOCK_FREE_AFTER=n: the lock is held for the first n tries.
  if [ -n "${FAKE_LOCK_FREE_AFTER:-}" ]; then
    n=$(grep -c ' lock ' "$FAKE_LOG")
    [ "$n" -gt "$FAKE_LOCK_FREE_AFTER" ] && exit 0 || exit 1
  fi
  exit "${FAKE_LOCK:-0}"
fi
exit 0
"""

# Fake claude: records its arguments and working directory, then exits
# $FAKE_CLAUDE_EXIT (default 0).
FAKE_CLAUDE = """#!/usr/bin/env bash
printf 'claude %s\\n' "$*" >> "$FAKE_LOG"
printf 'cwd %s\\n' "$PWD" >> "$FAKE_LOG"
exit "${FAKE_CLAUDE_EXIT:-0}"
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class BrainTickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(remote), cwd=self.tmp)
        self.root = self.tmp / "root"
        (self.root / "lanes" / "mac").mkdir(parents=True)
        shutil.copy(SCRIPT, self.root / "lanes" / "mac" / "brain.sh")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "MUSE.md").write_text("# Muse tick instructions\n")
        git("init", "-q", "-b", "main", cwd=self.root)
        git("add", ".", cwd=self.root)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "init", cwd=self.root)
        git("remote", "add", "origin", str(remote), cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("python3", FAKE_PYTHON), ("claude", FAKE_CLAUDE)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.home = self.tmp / "loom"
        self.log = self.tmp / "calls.log"
        self.env = {
            "PATH": "%s:%s" % (bin_dir, os.environ["PATH"]),
            "HOME": str(self.tmp),
            "LOOM_HOME": str(self.home),
            "LOOM_REPOS": "example/project",
            "LOOM_STATUS_REPO": "example/loom",
            "FAKE_LOG": str(self.log),
        }

    def tick(self, **env):
        run = subprocess.run(
            ["bash", str(self.root / "lanes" / "mac" / "brain.sh")],
            env={**self.env, **env}, capture_output=True, text=True, timeout=60,
        )
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return run, calls

    def beat(self):
        state = self.home / "state"
        last = json.loads((state / "brain.json").read_text())
        return last, (state / "brain-ok").exists()

    def test_heartbeats_say_how_each_tick_ended(self):
        self.tick()
        last, ok = self.beat()
        self.assertEqual(("ok", 0), (last["outcome"], last["exit"]))
        self.assertTrue(ok)
        (self.home / "state" / "brain-ok").unlink()
        self.tick(FAKE_CLAUDE_EXIT="124")
        last, ok = self.beat()
        self.assertEqual(("failed", 124), (last["outcome"], last["exit"]))
        self.assertFalse(ok)
        self.tick(FAKE_LOCK="1")
        last, ok = self.beat()
        self.assertEqual("skipped", last["outcome"])
        self.assertTrue(ok)

    def test_a_tick_that_ran_the_agent_records_its_start_and_model(self):
        # The station uploads the session once brain.json names its start.
        self.tick()
        last, _ = self.beat()
        self.assertIsInstance(last["started"], int)
        self.assertLessEqual(last["started"], last["at"])
        self.assertEqual("claude-opus-5-5", last["model"])
        self.tick(FAKE_LOCK="1")
        last, _ = self.beat()
        self.assertNotIn("started", last)

    def test_no_repos_means_no_lock_and_no_model_call(self):
        run, calls = self.tick(LOOM_REPOS="")
        self.assertEqual(0, run.returncode)
        self.assertEqual([], calls)

    def test_a_held_lock_skips_the_tick_before_any_model_call(self):
        run, calls = self.tick(FAKE_LOCK="1")
        self.assertEqual(0, run.returncode)
        self.assertEqual(1, len(calls))
        self.assertIn("-m bus lock example/loom --holder lane-machine --minutes 45", calls[0])
        self.assertFalse(any(c.startswith("claude") for c in calls))

    def test_a_tick_runs_the_agent_in_a_fresh_checkout_then_unlocks(self):
        run, calls = self.tick()
        self.assertEqual(0, run.returncode)
        self.assertIn("-m bus lock", calls[0])
        claude = [c for c in calls if c.startswith("claude ")]
        self.assertEqual(1, len(claude))
        self.assertIn("--model claude-opus-5-5", claude[0])
        self.assertIn("as holder lane-machine", claude[0])
        self.assertIn("%s/brain/loom/docs/MUSE.md" % self.home, claude[0])
        self.assertIn("cwd %s/brain" % self.home, calls)
        self.assertTrue((self.home / "brain" / "loom" / "docs" / "MUSE.md").exists())
        self.assertEqual("python3 -m bus unlock example/loom --holder lane-machine", calls[-1])

    def test_a_failed_agent_still_unlocks(self):
        _, calls = self.tick(FAKE_CLAUDE_EXIT="2")
        self.assertEqual("python3 -m bus unlock example/loom --holder lane-machine", calls[-1])
        self.assertIn("tick end (exit 2)", (self.home / "logs" / "brain.log").read_text())

    def test_the_second_tick_pulls_instead_of_cloning(self):
        self.tick()
        (self.root / "docs" / "MUSE.md").write_text("# Muse tick instructions\nnew\n")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qam", "change", cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        self.tick()
        muse = self.home / "brain" / "loom" / "docs" / "MUSE.md"
        self.assertIn("new", muse.read_text())

    def test_no_origin_never_takes_the_lock(self):
        git("remote", "remove", "origin", cwd=self.root)
        env = {k: v for k, v in self.env.items() if k != "LOOM_STATUS_REPO"}
        run = subprocess.run(
            ["bash", str(self.root / "lanes" / "mac" / "brain.sh")],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(1, run.returncode)
        self.assertFalse(self.log.exists())

    def test_a_live_tick_waits_for_a_held_lock_then_runs(self):
        run, calls = self.tick(LOOM_BRAIN_SCOPE="live", FAKE_LOCK_FREE_AFTER="2",
                               LOOM_BRAIN_WAIT_STEP="0")
        self.assertEqual(0, run.returncode)
        self.assertEqual(3, sum(1 for c in calls if " -m bus lock " in c))
        self.assertTrue(any(c.startswith("claude ") for c in calls))

    def test_a_live_tick_skips_after_its_wait(self):
        run, calls = self.tick(LOOM_BRAIN_SCOPE="live", FAKE_LOCK="1",
                               LOOM_BRAIN_WAIT="0", LOOM_BRAIN_WAIT_STEP="0")
        self.assertEqual(0, run.returncode)
        self.assertFalse(any(c.startswith("claude ") for c in calls))

    def test_a_full_tick_does_not_wait_by_default(self):
        run, calls = self.tick(FAKE_LOCK="1", LOOM_BRAIN_WAIT_STEP="0")
        self.assertEqual(1, sum(1 for c in calls if " -m bus lock " in c))

    def test_an_unknown_scope_never_takes_the_lock(self):
        run, calls = self.tick(LOOM_BRAIN_SCOPE="some")
        self.assertEqual(1, run.returncode)
        self.assertEqual([], calls)

    def test_live_scope_leaves_github_steps_to_muse(self):
        _, calls = self.tick(LOOM_BRAIN_SCOPE="live")
        claude = [c for c in calls if c.startswith("claude ")][0]
        self.assertIn("Do only what needs this machine", claude)
        # Finish-work comes first and is bounded; the freshness rule no longer
        # holds back work another brain yielded.
        # resume checks the tick's own lock, not the parser's default repo.
        self.assertIn("run timeout 120 python3 -m bus.brain resume $LOOM_REPOS --holder lane-machine "
                      "--lock-repo example/loom", claude)
        self.assertIn("Spend at most 13 minutes on them", claude)
        # The verify share: up to a third of the tick, proof only from evidence
        # seen in the tick, otherwise a note naming the missing evidence.
        self.assertIn("up to 5 verify-due tickets", claude)
        self.assertIn("spend up to a third of the tick on them", claude)
        self.assertIn("only from evidence you saw in this tick", claude)
        self.assertIn("naming what evidence is missing", claude)
        _, calls = self.tick()
        claude = [c for c in calls if c.startswith("claude ")][-1]
        self.assertNotIn("Do only what needs this machine", claude)

    def test_an_invalid_time_limit_never_takes_the_lock(self):
        run, calls = self.tick(LOOM_BRAIN_MINUTES="2")
        self.assertEqual(1, run.returncode)
        self.assertEqual([], calls)

    def test_settings_override_model_holder_and_minutes(self):
        _, calls = self.tick(LOOM_BRAIN_MODEL="claude-sonnet-5-5",
                             LOOM_BRAIN_HOLDER="night", LOOM_BRAIN_MINUTES="20")
        self.assertIn("--holder night --minutes 25", calls[0])
        claude = [c for c in calls if c.startswith("claude ")][0]
        self.assertIn("--model claude-sonnet-5-5", claude)
        self.assertIn("stop within 15 minutes; you are cut off at 20", claude)


if __name__ == "__main__":
    unittest.main()
