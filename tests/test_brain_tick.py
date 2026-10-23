"""lanes/mac/brain.sh run as a process: a copy of the script in a temp repo
whose origin is a local bare repo, and fake python3 (the bus lock), agent
CLIs (claude, codex, opencode, agy), gh and sandbox-exec commands on PATH
that record their calls. No real agent CLI ever runs."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LANES = Path(__file__).resolve().parent.parent / "lanes"
SCRIPT = LANES / "mac" / "brain.sh"

# Fake python3: `-m bus lock` exits $FAKE_LOCK (default 0), `-m bus unlock`
# exits 0, and `-m bus.brain_memory handoff` prints $FAKE_PACKET and exits
# $FAKE_PACKET_EXIT (default 0). Every call is appended to $FAKE_LOG.
FAKE_PYTHON = """#!/usr/bin/env bash
printf 'python3 %s\\n' "$*" >> "$FAKE_LOG"
if [ "$2" = bus.brain_memory ]; then
  printf '%s\\n' "${FAKE_PACKET:-}"
  exit "${FAKE_PACKET_EXIT:-0}"
fi
if [ "$3 $4" = "app token" ]; then
  # FAKE_APP_TOKEN: the Brain App's installation token; unset, minting fails.
  [ -n "${FAKE_APP_TOKEN:-}" ] || { echo "no App key" >&2; exit 2; }
  printf '%s\\n' "$FAKE_APP_TOKEN"
  exit 0
fi
if [ "$3 $4" = "app login" ]; then
  [ -n "${FAKE_APP_LOGIN:-}" ] || { echo "no App key" >&2; exit 2; }
  printf '%s\\n' "$FAKE_APP_LOGIN"
  exit 0
fi
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

# Fake agent CLI NAME: records its arguments and working directory, prints
# $FAKE_<NAME>_OUT, then exits $FAKE_<NAME>_EXIT (default 0).
FAKE_AGENT = """#!/usr/bin/env bash
printf '%(name)s %%s\\n' "$*" >> "$FAKE_LOG"
printf 'cwd %%s\\n' "$PWD" >> "$FAKE_LOG"
printf 'gh-path-%(name)s %%s\\n' "$(command -v gh)" >> "$FAKE_LOG"
[ -z "${FAKE_%(var)s_OUT:-}" ] || printf '%%s\\n' "$FAKE_%(var)s_OUT"
exit "${FAKE_%(var)s_EXIT:-0}"
"""

# Fake opencode: a 1.x version, and a run that records its arguments and the
# GitHub and LOOM_REPOS settings it was given. The sandboxed agent's
# environment is emptied, so the log path is written into the script.
FAKE_OPENCODE = """#!/usr/bin/env bash
FAKE_LOG=@LOG@
[ "$1" = --version ] && { echo "opencode 1.4.0"; exit 0; }
printf 'opencode %s\\n' "$*" >> "$FAKE_LOG"
printf 'opencode env GH_TOKEN=%s GITHUB_TOKEN=%s LOOM_REPOS=%s HOME=%s\\n' \\
  "${GH_TOKEN:-}" "${GITHUB_TOKEN:-}" "${LOOM_REPOS:-}" "$HOME" >> "$FAKE_LOG"
printf 'opencode login %s\\n' "${LOOM_GITHUB_LOGIN:-none}" >> "$FAKE_LOG"
printf 'opencode cwd %s\\n' "$PWD" >> "$FAKE_LOG"
printf 'gh-path-opencode %s\\n' "$(command -v gh)" >> "$FAKE_LOG"
env | grep -E '^(LOOM_|SSL_CERT_FILE=)' | sed 's/^/opencode setting /' >> "$FAKE_LOG"
exit "${FAKE_OPENCODE_EXIT:-0}"
"""

FAKE_GH = """#!/usr/bin/env bash
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
[ "$*" = "auth token" ] && echo gho_faketoken123
exit 0
"""

# Fake sandbox-exec: saves the profile, drops "-p PROFILE" and runs the
# command unsandboxed.
FAKE_SANDBOX = """#!/usr/bin/env bash
printf '%s\\n' "$2" > @PROFILE@
shift 2
exec "$@"
"""

AGENTS = ("claude", "codex", "agy")


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
        for shared in ("agent_cli.sh", "agent_sandbox.sh"):
            if (LANES / shared).exists():
                shutil.copy(LANES / shared, self.root / "lanes" / shared)
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
        self.log = self.tmp / "calls.log"
        self.profile = self.tmp / "profile.sb"
        fakes = [("python3", FAKE_PYTHON), ("gh", FAKE_GH),
                 ("sandbox-exec", FAKE_SANDBOX.replace("@PROFILE@", shlex.quote(str(self.profile)))),
                 ("opencode", FAKE_OPENCODE.replace("@LOG@", shlex.quote(str(self.log))))]
        fakes += [(n, FAKE_AGENT % {"name": n, "var": n.upper()}) for n in AGENTS]
        for name, body in fakes:
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.home = self.tmp / "loom"
        self.env = {
            "PATH": "%s:%s" % (bin_dir, os.environ["PATH"]),
            "HOME": str(self.tmp),
            "LOOM_HOME": str(self.home),
            "LOOM_REPOS": "example/project",
            "LOOM_STATUS_REPO": "example/loom",
            "FAKE_LOG": str(self.log),
        }
        # The claude lane's runner says it has usage, as on the lane machine.
        self.usage("claude", True)

    def usage(self, lane, available, reason="", age=0):
        usage = self.home / "state" / "usage"
        usage.mkdir(parents=True, exist_ok=True)
        (usage / ("%s.json" % lane)).write_text(json.dumps(
            {"lane": lane, "at": int(time.time()) - age, "available": available,
             "reason": reason}) + "\n")

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

    def test_a_tick_that_ran_the_agent_names_its_holder(self):
        self.tick(LOOM_BRAIN_HOLDER="lane-two")
        last, _ = self.beat()
        self.assertEqual("lane-two", last["holder"])

    def test_the_handoff_packet_goes_into_the_prompt(self):
        dashboard = {"LOOM_TRANSCRIPT_URL": "https://dashboard.example.com/api/transcripts",
                     "LOOM_TRANSCRIPT_TOKEN_SSM": "/example/token",
                     "FAKE_PACKET": "## Open commitments (1)"}
        _, calls = self.tick(**dashboard)
        self.assertIn("python3 -m bus.brain_memory handoff", calls)
        claude = [c for c in calls if c.startswith("claude ")]
        self.assertIn("Read this handoff packet before anything else.", claude[0])
        self.assertIn("## Open commitments (1)", "\n".join(calls))

    def test_no_dashboard_or_a_failed_read_runs_the_tick_without_the_packet(self):
        _, calls = self.tick()
        self.assertFalse(any("bus.brain_memory" in c for c in calls))
        self.log.unlink()
        _, calls = self.tick(LOOM_TRANSCRIPT_URL="https://dashboard.example.com/api/transcripts",
                             LOOM_TRANSCRIPT_TOKEN_SSM="/example/token",
                             FAKE_PACKET="half a packet", FAKE_PACKET_EXIT="1")
        claude = [c for c in calls if c.startswith("claude ")]
        self.assertEqual(1, len(claude))
        self.assertNotIn("handoff packet", claude[0])
        self.assertIn("could not read the handoff packet",
                      (self.home / "logs" / "brain.log").read_text())

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
        self.assertEqual("python3 -m bus.brain step lock --holder lane-machine "
                         "--lock-repo example/loom", calls[1])
        claude = [c for c in calls if c.startswith("claude ")]
        self.assertEqual(1, len(claude))
        self.assertIn("--model claude-opus-5-5", claude[0])
        self.assertIn("as holder lane-machine", claude[0])
        self.assertIn("%s/brain/loom/docs/MUSE.md" % self.home, claude[0])
        self.assertIn("cwd %s/brain" % self.home, calls)
        self.assertTrue((self.home / "brain" / "loom" / "docs" / "MUSE.md").exists())
        self.assertEqual("python3 -m bus unlock example/loom --holder lane-machine", calls[-1])

    def test_the_agent_records_its_steps_as_the_wrapper_holder(self):
        _, calls = self.tick()
        claude = [c for c in calls if c.startswith("claude ")]
        self.assertIn("python3 -m bus.brain step NAME --holder lane-machine "
                      "--lock-repo example/loom", claude[0])
        self.assertEqual("python3 -m bus.brain step unlock --holder lane-machine "
                         "--lock-repo example/loom", calls[-2])

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
        # Muse ships and rolls; this tick keeps the cluster, log and
        # database reads, and a ship or roll Muse left undone.
        self.assertIn("Muse does the GitHub steps, ships and rolls", claude)
        self.assertIn("checks or measurements that need cluster, log or", claude)
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

    def agent_calls(self, calls, name):
        return [c for c in calls if c.startswith(name + " ")]

    def test_claude_without_usage_hands_the_tick_to_codex(self):
        self.usage("claude", False, "limit: provider limit reached; lane waits")
        self.usage("codex", True)
        run, calls = self.tick(LOOM_CODEX_MODEL="gpt-test-high")
        self.assertEqual(0, run.returncode)
        self.assertEqual([], self.agent_calls(calls, "claude"))
        codex = self.agent_calls(calls, "codex")
        self.assertEqual(1, len(codex))
        # The brain needs GitHub: full access and no git-repo check, unlike a
        # lane's sandboxed run; the same prompt as the claude rung.
        self.assertTrue(codex[0].startswith(
            "codex exec --sandbox danger-full-access --skip-git-repo-check --model gpt-test-high "))
        self.assertIn("%s/brain/loom/docs/MUSE.md" % self.home, codex[0])
        self.assertIn("as holder lane-machine", codex[0])
        self.assertIn("cwd %s/brain" % self.home, calls)
        last, ok = self.beat()
        self.assertEqual(("ok", "codex", "gpt-test-high"), (last["outcome"], last["lane"], last["model"]))
        self.assertTrue(ok)
        log = (self.home / "logs" / "brain.log").read_text()
        self.assertIn("tick start (lane codex, model gpt-test-high, scope all)", log)
        self.assertEqual("python3 -m bus unlock example/loom --holder lane-machine", calls[-1])

    def test_a_stale_heartbeat_has_no_usage(self):
        # A runner that stopped writes no heartbeat; its old one does not count.
        self.usage("claude", True, age=3600)
        self.usage("codex", True)
        _, calls = self.tick(LOOM_CODEX_MODEL="gpt-test-high")
        self.assertEqual([], self.agent_calls(calls, "claude"))
        self.assertEqual(1, len(self.agent_calls(calls, "codex")))

    def test_a_lane_mid_run_keeps_its_usage_though_its_heartbeat_aged(self):
        # A runner writes its heartbeat between runs, not during one: at 11:04Z
        # on 2026-10-21 the claude lane was 30 minutes into a run and the spark
        # lane 40, so the tick skipped with "no brain lane has usage".
        self.usage("claude", True, age=1800)
        lock = self.home / "run" / "claude.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text("%d\n" % os.getpid())
        _, calls = self.tick()
        self.assertEqual(1, len(self.agent_calls(calls, "claude")))

    def test_a_dead_runners_lock_does_not_keep_an_aged_heartbeat(self):
        self.usage("claude", True, age=1800)
        lock = self.home / "run" / "claude.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text("999999\n")
        _, calls = self.tick()
        self.assertEqual([], self.agent_calls(calls, "claude"))

    def test_a_limit_failure_moves_the_tick_to_the_next_rung_once(self):
        for lane in ("codex", "spark"):
            self.usage(lane, True)
        run, calls = self.tick(
            LOOM_CODEX_MODEL="gpt-test-high", LOOM_SPARK_MODEL="spark/coder",
            LOOM_SPARK_URL="http://spark.invalid:8000/v1",
            FAKE_CLAUDE_EXIT="1", FAKE_CLAUDE_OUT="Claude AI usage limit reached|1792250000",
            FAKE_CODEX_EXIT="1", FAKE_CODEX_OUT="ERROR: rate limit exceeded, try again in 2h")
        self.assertEqual(0, run.returncode)
        self.assertEqual(1, len(self.agent_calls(calls, "claude")))
        self.assertEqual(1, len(self.agent_calls(calls, "codex")))
        # Once only: the third rung is not tried, though it has usage.
        self.assertEqual([], self.agent_calls(calls, "opencode"))
        # One lock, one unlock: the second rung runs inside the same tick.
        self.assertEqual(1, sum(1 for c in calls if " -m bus lock " in c))
        self.assertEqual("python3 -m bus unlock example/loom --holder lane-machine", calls[-1])
        last, ok = self.beat()
        self.assertEqual(("failed", 1, "codex", "gpt-test-high"),
                         (last["outcome"], last["exit"], last["lane"], last["model"]))
        self.assertFalse(ok)
        log = (self.home / "logs" / "brain.log").read_text()
        self.assertIn("lane claude hit a limit; the tick moves to the next rung with usage", log)

    def test_the_rung_after_a_limit_can_finish_the_tick(self):
        self.usage("codex", True)
        _, calls = self.tick(LOOM_CODEX_MODEL="gpt-test-high", FAKE_CLAUDE_EXIT="1",
                             FAKE_CLAUDE_OUT="API Error: 429 Too Many Requests")
        self.assertEqual(1, len(self.agent_calls(calls, "codex")))
        last, ok = self.beat()
        self.assertEqual(("ok", "codex"), (last["outcome"], last["lane"]))
        self.assertTrue(ok)

    def test_a_failure_that_is_not_a_limit_stays_on_its_rung(self):
        self.usage("codex", True)
        _, calls = self.tick(LOOM_CODEX_MODEL="gpt-test-high", FAKE_CLAUDE_EXIT="2",
                             FAKE_CLAUDE_OUT="Error: could not read the goal home")
        self.assertEqual([], self.agent_calls(calls, "codex"))
        last, _ = self.beat()
        self.assertEqual(("failed", 2, "claude"), (last["outcome"], last["exit"], last["lane"]))

    def test_no_rung_with_usage_skips_with_a_reason_and_runs_nothing(self):
        self.usage("claude", False, "limit: out of credit")
        self.usage("spark", True)
        run, calls = self.tick(LOOM_CODEX_MODEL="gpt-test-high")
        self.assertEqual(0, run.returncode)
        # No CLI and no lock: nothing was going to run.
        self.assertEqual([], calls)
        last, ok = self.beat()
        self.assertEqual(("skipped", 0), (last["outcome"], last["exit"]))
        self.assertNotIn("started", last)
        self.assertIn("no brain lane has usage", last["reason"])
        self.assertIn("claude: unavailable (limit: out of credit)", last["reason"])
        self.assertIn("codex: no usage heartbeat", last["reason"])
        self.assertIn("spark: LOOM_SPARK_URL is not set", last["reason"])
        # Not a good tick: the station's brain-stopped check sees it go stale.
        self.assertFalse(ok)

    def test_a_rung_without_a_model_or_runner_is_passed_over(self):
        self.usage("agy", True)
        self.usage("codex", True)
        run, calls = self.tick(LOOM_BRAIN_LADDER="agy codex", LOOM_CODEX_MODEL="")
        self.assertEqual([], calls)
        last, _ = self.beat()
        self.assertIn("agy: no brain runner for this lane", last["reason"])
        self.assertIn("codex: LOOM_CODEX_MODEL is not set", last["reason"])

    def test_the_ladder_order_is_a_setting(self):
        self.usage("codex", True)
        _, calls = self.tick(LOOM_BRAIN_LADDER="codex claude", LOOM_CODEX_MODEL="gpt-test-high")
        self.assertEqual(1, len(self.agent_calls(calls, "codex")))
        self.assertEqual([], self.agent_calls(calls, "claude"))

    def test_the_spark_rung_runs_opencode_in_the_lane_sandbox_with_a_github_token(self):
        self.usage("claude", False, "limit: provider limit reached")
        self.usage("spark", True)
        run, calls = self.tick(LOOM_SPARK_MODEL="spark/coder",
                               LOOM_SPARK_URL="http://spark.invalid:8000/v1")
        self.assertEqual(0, run.returncode)
        opencode = self.agent_calls(calls, "opencode")
        self.assertTrue(opencode[0].startswith("opencode run -m spark/coder "), opencode)
        self.assertIn("%s/brain/loom/docs/MUSE.md" % self.home, opencode[0])
        # The token reaches the agent's environment, never its argv.
        self.assertNotIn("gho_faketoken123", opencode[0])
        self.assertIn("opencode env GH_TOKEN=gho_faketoken123 GITHUB_TOKEN=gho_faketoken123 "
                      "LOOM_REPOS=example/project HOME=%s/run/brain-home" % self.home, calls)
        last, _ = self.beat()
        self.assertEqual(("ok", "spark", "spark/coder"), (last["outcome"], last["lane"], last["model"]))

    def test_with_its_app_settings_the_tick_writes_as_the_brain_app_not_the_operator(self):
        # loom#1142: the brain's rungs used the operator's own GitHub token.
        self.usage("claude", False, "limit: provider limit reached")
        self.usage("spark", True)
        run, calls = self.tick(LOOM_SPARK_MODEL="spark/coder",
                               LOOM_SPARK_URL="http://spark.invalid:8000/v1",
                               LOOM_GITHUB_APP_ID="123", FAKE_APP_TOKEN="ghs_brain.tok-en",
                               GITHUB_TOKEN="ghp_personal")
        self.assertEqual(0, run.returncode)
        self.assertIn("opencode env GH_TOKEN=ghs_brain.tok-en GITHUB_TOKEN=ghs_brain.tok-en "
                      "LOOM_REPOS=example/project HOME=%s/run/brain-home" % self.home, calls)
        self.assertNotIn("gh auth token", calls)

    def test_the_brains_agent_is_told_its_app_login(self):
        # GET /user refuses an installation token, so the agent's journal
        # needs the App's login handed to it (2026-10-21).
        self.usage("claude", False, "limit: provider limit reached")
        self.usage("spark", True)
        _, calls = self.tick(LOOM_SPARK_MODEL="spark/coder",
                             LOOM_SPARK_URL="http://spark.invalid:8000/v1",
                             LOOM_GITHUB_APP_ID="123", FAKE_APP_TOKEN="ghs_brain.tok-en",
                             FAKE_APP_LOGIN="loom-brain[bot]")
        self.assertIn("opencode login loom-brain[bot]", calls)

    def test_a_tick_whose_app_token_cannot_be_minted_runs_no_rung(self):
        self.usage("codex", True)
        run, calls = self.tick(LOOM_BRAIN_LADDER="codex claude", LOOM_GITHUB_APP_ID="123",
                               GITHUB_TOKEN="ghp_personal")
        self.assertEqual([], self.agent_calls(calls, "codex") + self.agent_calls(calls, "claude"))
        self.assertTrue(any(" unlock " in c for c in calls), calls)
        last, ok = self.beat()
        self.assertEqual("failed", last["outcome"])
        self.assertFalse(ok)

    def test_a_tick_longer_than_its_app_token_lasts_runs_no_rung(self):
        # An installation token lasts an hour; a 58-minute tick would outlive it.
        self.usage("codex", True)
        run, calls = self.tick(LOOM_BRAIN_LADDER="codex claude", LOOM_GITHUB_APP_ID="123",
                               FAKE_APP_TOKEN="ghs_brain.tok-en", LOOM_BRAIN_MINUTES="58")
        self.assertEqual([], self.agent_calls(calls, "codex") + self.agent_calls(calls, "claude"))
        self.assertNotEqual(0, run.returncode)

    def spark_tick(self, **env):
        self.usage("claude", False, "limit: provider limit reached")
        self.usage("spark", True)
        return self.tick(LOOM_SPARK_MODEL="spark/coder",
                         LOOM_SPARK_URL="http://spark.invalid:8000/v1", **env)

    def test_the_spark_rung_gets_only_the_allowlisted_settings(self):
        # Found by the automated security review: a denylist forwarded every
        # other LOOM_* variable, keys and SSM names included.
        _, calls = self.spark_tick(LOOM_FOO_TOKEN="foo-secret-value",
                                   LOOM_OPENCODE_GO_KEY_SSM="/example/key",
                                   LOOM_BRIEF_AUTHORS="example-author")
        settings = [c for c in calls if c.startswith("opencode setting ")]
        self.assertIn("opencode setting LOOM_REPOS=example/project", settings)
        self.assertIn("opencode setting LOOM_BRIEF_AUTHORS=example-author", settings)
        self.assertIn("opencode setting LOOM_GITHUB_TOKEN=gho_faketoken123", settings)
        # TLS inside the sandbox, which denies the keychain server.
        self.assertIn("opencode setting SSL_CERT_FILE=/etc/ssl/cert.pem", settings)
        self.assertFalse(any("foo-secret-value" in c or "LOOM_FOO_TOKEN" in c for c in calls))
        self.assertFalse(any("LOOM_OPENCODE_GO_KEY_SSM" in c for c in settings))
        self.assertFalse(any("LOOM_SPARK_URL" in c for c in settings))

    def test_the_shadow_switch_reaches_the_sandboxed_brain(self):
        # bus/shadow.py: the brain's own merges and closes honor the same
        # switch and repo list as the station's.
        _, calls = self.spark_tick(LOOM_SHADOW="1", LOOM_SHADOW_REPOS="example/project")
        settings = [c for c in calls if c.startswith("opencode setting ")]
        self.assertIn("opencode setting LOOM_SHADOW=1", settings)
        self.assertIn("opencode setting LOOM_SHADOW_REPOS=example/project", settings)

    def test_every_rung_runs_gh_through_the_shadow_guard(self):
        # bus/shadow.py: the rung holds a GitHub token, so the gh it runs is
        # the guard, and the prompt names shadow mode.
        _, calls = self.tick()
        guard = self.home / "run" / "brain-bin" / "gh"
        self.assertIn("gh-path-claude %s" % guard, calls)
        self.assertIn("python3 -B -m bus.shadow gh-guard", guard.read_text())
        claude = self.agent_calls(calls, "claude")
        self.assertIn("never merge, close an issue, approve a pull request", claude[0])
        self.log.unlink()
        _, calls = self.spark_tick()
        self.assertIn("gh-path-opencode %s/run/brain-home/bin/gh" % self.home, calls)

    def test_the_guard_refuses_a_merge_in_shadow_and_runs_gh_otherwise(self):
        # The rung's checkout carries the real bus package, as origin does.
        shutil.copytree(LANES.parent / "bus", self.root / "bus",
                        ignore=shutil.ignore_patterns("__pycache__"))
        git("add", ".", cwd=self.root)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "bus", cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        self.tick()
        guard = str(self.home / "run" / "brain-bin" / "gh")
        real = self.tmp / "real-python"
        real.mkdir()
        (real / "python3").symlink_to(sys.executable)
        ledger = self.tmp / "ledger.jsonl"
        env = {k: v for k, v in self.env.items() if not k.startswith("LOOM_SHADOW")}
        env.update(PATH="%s:%s" % (real, self.env["PATH"]), LOOM_SHADOW_LEDGER=str(ledger))

        def gh(*args, **extra):
            run = subprocess.run([guard, *args], env={**env, **extra}, capture_output=True,
                                 text=True, timeout=60)
            calls = self.log.read_text().splitlines()
            return run, calls

        # No switch at all is shadow: the merge never reaches gh.
        run, calls = gh("pr", "merge", "5", "-R", "example/project", "--squash")
        self.assertEqual(1, run.returncode, run.stderr)
        self.assertIn("shadow mode: would merge %s#%d" % ("example/project", 5), run.stderr)
        self.assertNotIn("gh pr merge 5 -R example/project --squash", calls)
        self.assertEqual("brain rung gh", json.loads(ledger.read_text())["source"])
        run, calls = gh("pr", "view", "5", "-R", "example/project")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("gh pr view 5 -R example/project", calls)
        run, calls = gh("pr", "merge", "5", "-R", "example/project", LOOM_SHADOW="0")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("gh pr merge 5 -R example/project", calls)

    def test_the_spark_sandbox_keeps_the_checkout_git_dir_read_only(self):
        # Found by the automated security review: with the tree at the brain
        # folder, the checkout's .git (one level down) was writable.
        _, calls = self.spark_tick()
        checkout = os.path.realpath(self.home / "brain" / "loom")
        cwd = [os.path.realpath(c[len("opencode cwd "):]) for c in calls
               if c.startswith("opencode cwd ")]
        self.assertEqual([checkout], cwd)
        profile = self.profile.read_text()
        self.assertIn('(deny file-write* (subpath "%s/.git"))' % checkout, profile)

    def test_the_refresh_runs_no_hook_and_drops_what_a_run_left(self):
        self.tick()
        checkout = self.home / "brain" / "loom"
        marker = self.tmp / "hook-ran"
        hooks = self.tmp / "planted-hooks"
        hooks.mkdir()
        for name in ("post-merge", "post-checkout", "reference-transaction", "fsmonitor"):
            (hooks / name).write_text("#!/bin/sh\ntouch %s\n" % shlex.quote(str(marker)))
            (hooks / name).chmod(0o755)
        git("config", "core.hooksPath", str(hooks), cwd=checkout)
        git("config", "core.fsmonitor", str(hooks / "fsmonitor"), cwd=checkout)
        (checkout / "docs" / "MUSE.md").write_text("# planted instructions\n")
        (checkout / "planted.txt").write_text("left by a run\n")
        (self.root / "docs" / "MUSE.md").write_text("# Muse tick instructions\nnew\n")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qam", "change", cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        run, _ = self.tick()
        self.assertEqual(0, run.returncode)
        self.assertFalse(marker.exists())
        self.assertEqual("# Muse tick instructions\nnew\n", (checkout / "docs" / "MUSE.md").read_text())
        self.assertFalse((checkout / "planted.txt").exists())

    def test_the_refresh_removes_a_nested_repository(self):
        # Found by the automated security review: clean -fdx keeps an
        # untracked nested repository, so a run could leave files for later.
        self.tick()
        nested = self.home / "brain" / "loom" / "planted"
        nested.mkdir()
        git("init", "-q", cwd=nested)
        (nested / "notes.md").write_text("kept across ticks\n")
        run, _ = self.tick()
        self.assertEqual(0, run.returncode)
        self.assertFalse(nested.exists())

    def test_a_checkout_that_will_not_reset_is_cloned_again(self):
        self.tick()
        checkout = self.home / "brain" / "loom"
        git("branch", "--unset-upstream", cwd=checkout)
        (checkout / "planted.txt").write_text("left by a run\n")
        run, calls = self.tick()
        self.assertEqual(0, run.returncode)
        self.assertFalse((checkout / "planted.txt").exists())
        self.assertTrue((checkout / "docs" / "MUSE.md").exists())
        self.assertIn("did not come clean; cloning it again",
                      (self.home / "logs" / "brain.log").read_text())
        # The calls log spans both ticks: the second one ran its agent too.
        self.assertEqual(2, len(self.agent_calls(calls, "claude")))

    def test_settings_override_model_holder_and_minutes(self):
        _, calls = self.tick(LOOM_BRAIN_MODEL="claude-sonnet-5-5",
                             LOOM_BRAIN_HOLDER="night", LOOM_BRAIN_MINUTES="20")
        self.assertIn("--holder night --minutes 25", calls[0])
        claude = [c for c in calls if c.startswith("claude ")][0]
        self.assertIn("--model claude-sonnet-5-5", claude)
        self.assertIn("stop within 15 minutes; you are cut off at 20", claude)


if __name__ == "__main__":
    unittest.main()
