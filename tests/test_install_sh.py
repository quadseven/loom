"""lanes/mac/install.sh run as a process: the station lock wait is exercised
with a fake launchctl and a throwaway HOME/LOOM_HOME."""

import os
import re
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

    def run_install(self, *lanes, **env):
        # The watched repos, status repo and App settings come only from the
        # test: an operator's own settings in the environment would trip the
        # owner check or the App check.
        full = {k: v for k, v in os.environ.items()
                if k not in ("LOOM_REPOS", "LOOM_STATUS_REPO")
                and not k.startswith(("LOOM_GITHUB_APP_", "LOOM_LANES_GITHUB_APP_",
                                       "LOOM_BRAIN_GITHUB_APP_"))}
        full.update(PATH="%s:/usr/bin:/bin" % self.bin,
                    HOME=str(self.home),
                    LOOM_HOME=str(self.loom),
                    LOOM_REPO_OWNERS="example-owner",
                    LOOM_CODEX_MODEL="codex-test",
                    FAKE_LOG=str(self.log))
        full.update(env)
        return subprocess.run(
            ["bash", str(self.checkout / "lanes" / "mac" / "install.sh"), *(lanes or ("codex",))],
            env=full, capture_output=True, text=True, timeout=60)

    def test_a_runner_slot_gets_no_local_runner_and_the_station_gets_its_settings(self):
        # LOOM_RUNNER_SLOTS (docs/LANES.md, "Lanes on cluster runners"): the
        # station dispatches the slot's briefs, so no launchd runner may take
        # them, and the station needs the slot's settings to send.
        shutil.copytree(INSTALL.parents[2] / "bus", self.checkout / "bus",
                        ignore=shutil.ignore_patterns("__pycache__"))
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.loom.zen.plist").write_text("old")
        result = self.run_install("zen", "codex", LOOM_RUNNER_SLOTS="zen",
                                  LOOM_ZEN_KEY_SSM="/x/zen", LOOM_LANES_GITHUB_APP_ID="42",
                                  LOOM_LANES_GITHUB_APP_INSTALLATION_ID="7",
                                  LOOM_LANES_GITHUB_APP_KEY_SSM="/x/lanes-app")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((agents / "com.loom.zen.plist").exists())
        self.assertIn("no com.loom.zen: zen runs on cluster runners", result.stdout)
        self.assertIn("launchctl bootout gui/%d/com.loom.zen" % os.getuid(), self.log.read_text())
        self.assertTrue((agents / "com.loom.codex.plist").exists())
        station = (agents / "com.loom.station.plist").read_text()
        for key, value in (("LOOM_RUNNER_SLOTS", "zen"), ("LOOM_ZEN_KEY_SSM", "/x/zen"),
                           ("LOOM_LANES_GITHUB_APP_ID", "42"),
                           ("LOOM_LANES_GITHUB_APP_KEY_SSM", "/x/lanes-app")):
            self.assertIn("<key>%s</key><string>%s</string>" % (key, value), station)
        self.assertEqual(station.count("<key>LOOM_ZEN_MODEL</key>"), 1)

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

    def test_private_rewrite_defaults_to_the_codex_subscription(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_REWRITE_PRIVATE_CMD</key><string>lanes/rewrite_agent.sh - codex/codex-test</string>", plist)

    def test_private_rewrite_keeps_an_explicit_command(self):
        command = "lanes/rewrite_agent.sh - claude/claude-test"
        result = self.run_install(LOOM_REWRITE_PRIVATE_CMD=command)
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_REWRITE_PRIVATE_CMD</key><string>%s</string>" % command, plist)

    def test_pin_targets_are_written_to_station_plist(self):
        result = self.run_install(LOOM_PIN_TARGETS='[{"repo":"example-owner/deploy"}]')
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn('<key>LOOM_PIN_TARGETS</key><string>[{"repo":"example-owner/deploy"}]</string>', plist)

    def test_the_fabric_key_parameter_is_written_to_station_plist(self):
        result = self.run_install(LOOM_FABRIC_KEY_SSM="/example/fabric/key")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_FABRIC_KEY_SSM</key><string>/example/fabric/key</string>", plist)

    def test_the_brain_plist_omits_the_fabric_key_without_the_opt_in(self):
        # The brain's Fabric question is opt-in (LOOM_BRAIN_FABRIC_ASK) and
        # off by default, so its plist must not carry the key parameter name
        # at all; the station's plist keeps it exactly as before.
        env = {k: v for k, v in os.environ.items() if k != "LOOM_BRAIN_FABRIC_ASK"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            result = self.run_install(LOOM_FABRIC_KEY_SSM="/example/fabric/key")
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = self.home / "Library" / "LaunchAgents"
        brain = (agents / "com.loom.brain.plist").read_text()
        self.assertNotIn("LOOM_FABRIC_KEY_SSM", brain)
        station = (agents / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_FABRIC_KEY_SSM</key><string>/example/fabric/key</string>", station)

    def test_the_brain_plist_gets_the_fabric_key_with_the_opt_in(self):
        result = self.run_install(LOOM_FABRIC_KEY_SSM="/example/fabric/key",
                                  LOOM_BRAIN_FABRIC_ASK="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = self.home / "Library" / "LaunchAgents"
        brain = (agents / "com.loom.brain.plist").read_text()
        self.assertIn("<key>LOOM_FABRIC_KEY_SSM</key><string>/example/fabric/key</string>", brain)
        self.assertIn("<key>LOOM_BRAIN_FABRIC_ASK</key><string>1</string>", brain)
        station = (agents / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_FABRIC_KEY_SSM</key><string>/example/fabric/key</string>", station)

    def test_roll_ship_documents_are_written_to_station_plist(self):
        documents = '[{"goal":"example-owner/project#12","repo":"example-owner/project","path":"docs/ship.md"}]'
        result = self.run_install(LOOM_ROLL_SHIP_DOCS=documents)
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn('<key>LOOM_ROLL_SHIP_DOCS</key><string>%s</string>' % documents, plist)

    def test_pin_ship_repos_are_written_to_station_plist(self):
        result = self.run_install(LOOM_PIN_SHIP_REPOS="example-owner/deploy")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_PIN_SHIP_REPOS</key><string>example-owner/deploy</string>", plist)

    def test_the_station_plist_installs_in_shadow_mode_unless_told_otherwise(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_SHADOW")}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_SHADOW</key><string>1</string>", plist)
        self.assertIn("<key>LOOM_SHADOW_REPOS</key><string></string>", plist)

    def test_shadow_repos_and_revert_labelers_are_written_to_station_plist(self):
        result = self.run_install(LOOM_SHADOW="1", LOOM_SHADOW_REPOS="example-owner/a example-owner/b",
                                  LOOM_REVERT_LABELERS="example-station[bot]")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_SHADOW_REPOS</key><string>example-owner/a example-owner/b</string>",
                      plist)
        self.assertIn("<key>LOOM_REVERT_LABELERS</key><string>example-station[bot]</string>", plist)

    def test_ship_rules_are_written_to_station_plist(self):
        result = self.run_install(LOOM_SHIP_ON_MERGE="example-owner/lib",
                                  LOOM_SHIP_WORKFLOWS="example-owner/svc=deploy.yml",
                                  LOOM_SHIP_PATH_WORKFLOWS="example-owner/infra")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_SHIP_ON_MERGE</key><string>example-owner/lib</string>", plist)
        self.assertIn("<key>LOOM_SHIP_WORKFLOWS</key><string>example-owner/svc=deploy.yml</string>",
                      plist)
        self.assertIn("<key>LOOM_SHIP_PATH_WORKFLOWS</key><string>example-owner/infra</string>", plist)

    def test_disk_settings_are_written_to_station_plist(self):
        result = self.run_install(LOOM_LOG_KEEP_DAYS="14", LOOM_DISK_FREE_MIN_GB="20",
                                  LOOM_LOG_MAX_GB="9", LOOM_LOG_ROTATE_MB="30")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_LOG_KEEP_DAYS</key><string>14</string>", plist)
        self.assertIn("<key>LOOM_MIN_FREE_GB</key><string>20</string>", plist)
        self.assertIn("<key>LOOM_LOG_MAX_GB</key><string>9</string>", plist)
        self.assertIn("<key>LOOM_LOG_ROTATE_MB</key><string>30</string>", plist)

    def test_pass_deadline_is_written_to_station_plist(self):
        result = self.run_install(LOOM_PASS_DEADLINE="2400")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_PASS_DEADLINE</key><string>2400</string>", plist)

    def test_claimed_settle_settings_are_written_to_station_plist(self):
        result = self.run_install(LOOM_SETTLE_CLAIMED="on", LOOM_SETTLE_CLAIMED_MAX="5")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_SETTLE_CLAIMED</key><string>on</string>", plist)
        self.assertIn("<key>LOOM_SETTLE_CLAIMED_MAX</key><string>5</string>", plist)

    def test_claim_referee_settings_are_written_to_station_plist(self):
        result = self.run_install(LOOM_CLAIM_REFEREE="dry", LOOM_ENGINE_CLAIMS="claim:x claim:y")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_CLAIM_REFEREE</key><string>dry</string>", plist)
        self.assertIn("<key>LOOM_ENGINE_CLAIMS</key><string>claim:x claim:y</string>", plist)

    def test_trusted_briefers_are_written_to_station_and_brain_plists(self):
        result = self.run_install(LOOM_TRUSTED_BRIEFERS="dana-example")
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = self.home / "Library" / "LaunchAgents"
        entry = "<key>LOOM_TRUSTED_BRIEFERS</key><string>dana-example</string>"
        self.assertIn(entry, (agents / "com.loom.station.plist").read_text())
        self.assertIn(entry, (agents / "com.loom.brain.plist").read_text())

    def trusted_briefers_entries(self):
        agents = self.home / "Library" / "LaunchAgents"
        found = [re.search(r"<key>LOOM_TRUSTED_BRIEFERS</key><string>(.*?)</string>",
                           (agents / (label + ".plist")).read_text())
                 for label in ("com.loom.station", "com.loom.brain")]
        return [match.group(1) if match else "" for match in found]

    def test_a_reinstall_with_the_setting_unset_keeps_the_installed_briefers(self):
        self.assertEqual(self.run_install(LOOM_TRUSTED_BRIEFERS="dana-example app[bot]").returncode, 0)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trusted_briefers_entries(), ["dana-example app[bot]"] * 2)

    def test_a_reinstall_that_sets_the_briefers_replaces_them_even_with_empty(self):
        self.assertEqual(self.run_install(LOOM_TRUSTED_BRIEFERS="dana-example").returncode, 0)
        self.assertEqual(self.run_install(LOOM_TRUSTED_BRIEFERS="lee-example").returncode, 0)
        self.assertEqual(self.trusted_briefers_entries(), ["lee-example"] * 2)
        self.assertEqual(self.run_install(LOOM_TRUSTED_BRIEFERS="").returncode, 0)
        self.assertEqual(self.trusted_briefers_entries(), ["", ""])

    def test_an_unreadable_installed_plist_stops_a_reinstall_that_names_no_briefers(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "com.loom.station.plist").write_text("not a plist")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOOM_TRUSTED_BRIEFERS", result.stderr)

    def installed_setting(self, name):
        agents = self.home / "Library" / "LaunchAgents"
        found = [re.search(r"<key>%s</key><string>(.*?)</string>" % name,
                           (agents / (label + ".plist")).read_text())
                 for label in ("com.loom.station", "com.loom.brain")]
        return [match.group(1) if match else "" for match in found]

    def test_ask_answerers_are_written_to_station_and_brain_plists(self):
        result = self.run_install(LOOM_ASK_ANSWERERS="dana-example app[bot]")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_setting("LOOM_ASK_ANSWERERS"), ["dana-example app[bot]"] * 2)

    def test_a_reinstall_with_a_login_list_unset_keeps_the_installed_list(self):
        for name in ("LOOM_ASK_ANSWERERS", "LOOM_BRIEF_AUTHORS"):
            with self.subTest(name):
                self.assertEqual(self.run_install(**{name: "dana-example app[bot]"}).returncode, 0)
                env = {k: v for k, v in os.environ.items() if k != name}
                with unittest.mock.patch.dict(os.environ, env, clear=True):
                    result = self.run_install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.installed_setting(name), ["dana-example app[bot]"] * 2)

    def test_a_reinstall_that_sets_a_login_list_replaces_it_even_with_empty(self):
        for name in ("LOOM_ASK_ANSWERERS", "LOOM_BRIEF_AUTHORS"):
            with self.subTest(name):
                self.assertEqual(self.run_install(**{name: "dana-example"}).returncode, 0)
                self.assertEqual(self.run_install(**{name: "lee-example"}).returncode, 0)
                self.assertEqual(self.installed_setting(name), ["lee-example"] * 2)
                self.assertEqual(self.run_install(**{name: ""}).returncode, 0)
                self.assertEqual(self.installed_setting(name), ["", ""])

    def test_an_unreadable_installed_plist_names_each_unset_login_list(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "com.loom.station.plist").write_text("not a plist")
        result = self.run_install(LOOM_TRUSTED_BRIEFERS="", LOOM_BRIEF_AUTHORS="x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOOM_ASK_ANSWERERS", result.stderr)
        result = self.run_install(LOOM_TRUSTED_BRIEFERS="", LOOM_ASK_ANSWERERS="x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOOM_BRIEF_AUTHORS", result.stderr)

    def test_goal_limits_are_written_to_station_and_brain_plists(self):
        result = self.run_install(LOOM_GOAL_CAP="28", LOOM_UNVERIFIED_LIMIT="60",
                                  LOOM_NO_LANE_REROUTE="4")
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = self.home / "Library" / "LaunchAgents"
        for label in ("com.loom.station", "com.loom.brain"):
            plist = (agents / (label + ".plist")).read_text()
            self.assertIn("<key>LOOM_GOAL_CAP</key><string>28</string>", plist)
            self.assertIn("<key>LOOM_UNVERIFIED_LIMIT</key><string>60</string>", plist)
        # The factory runs in the station (bus/factory.py no_lane_reroute).
        self.assertIn("<key>LOOM_NO_LANE_REROUTE</key><string>4</string>",
                      (agents / "com.loom.station.plist").read_text())

    def test_lane_model_settings_are_written_to_station_plist(self):
        # The factory reads what a lane ran from its setting (bus/brain.py
        # lane_model) when no Model: line or runner footer names the model.
        result = self.run_install(LOOM_CLAUDE_MODEL="claude-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_CLAUDE_MODEL</key><string>claude-test</string>", plist)
        self.assertIn("<key>LOOM_CODEX_MODEL</key><string>codex-test</string>", plist)

    def station_plist_with(self, **env):
        # KUBECONFIG and the allow-list come only from the test, never from
        # the operator's own environment.
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("KUBECONFIG", "LOOM_STATION_PASS_ENV")}
        with unittest.mock.patch.dict(os.environ, clean, clear=True):
            result = self.run_install(**env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()

    def test_station_pass_env_writes_kubeconfig_to_station_plist(self):
        # Live evidence (LOOM_LIVE_EVIDENCE_PASS_ENV=KUBECONFIG) needs the
        # station to have KUBECONFIG; a reinstall must keep it.
        plist = self.station_plist_with(KUBECONFIG="/example/kube/config",
                                        LOOM_STATION_PASS_ENV="KUBECONFIG")
        self.assertIn("<key>KUBECONFIG</key><string>/example/kube/config</string>", plist)
        self.assertEqual(plist.count("<key>KUBECONFIG</key>"), 1)

    def test_without_station_pass_env_the_station_plist_is_unchanged(self):
        before = self.station_plist_with()
        after = self.station_plist_with(KUBECONFIG="/example/kube/config")
        self.assertEqual(after, before)
        self.assertNotIn("KUBECONFIG", after)

    def test_station_pass_env_skips_unset_loom_and_written_names(self):
        plist = self.station_plist_with(LOOM_STATION_PASS_ENV="KUBECONFIG, PATH HOME LOOM_X bad-name",
                                        LOOM_X="x")
        self.assertNotIn("KUBECONFIG", plist)
        self.assertNotIn("LOOM_X", plist)
        self.assertNotIn("bad-name", plist)
        self.assertEqual(plist.count("<key>PATH</key>"), 1)
        self.assertEqual(plist.count("<key>HOME</key>"), 1)

    def test_a_bad_station_interval_installs_nothing(self):
        for bad in ("soon", "30"):
            with self.subTest(bad=bad):
                result = self.run_install(LOOM_STATION_INTERVAL=bad)
                self.assertEqual(result.returncode, 1)
                self.assertIn("LOOM_STATION_INTERVAL", result.stderr)
                self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def app_key(self, mode):
        key = self.tmp / "app-key.pem"
        key.write_text("not a real key\n")
        key.chmod(mode)
        return key

    def test_an_app_key_file_is_written_to_station_plist(self):
        key = self.app_key(0o600)
        result = self.run_install(LOOM_GITHUB_APP_ID="123",
                                  LOOM_GITHUB_APP_INSTALLATION_ID="456",
                                  LOOM_GITHUB_APP_KEY_FILE=str(key))
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_GITHUB_APP_KEY_FILE</key><string>%s</string>" % key, plist)
        self.assertIn("<key>LOOM_GITHUB_APP_ID</key><string>123</string>", plist)

    def test_half_an_app_setup_installs_nothing(self):
        key = self.app_key(0o600)
        cases = {
            "LOOM_GITHUB_APP_INSTALLATION_ID": dict(LOOM_GITHUB_APP_ID="123",
                                                    LOOM_GITHUB_APP_KEY_SSM="/app/key"),
            "LOOM_GITHUB_APP_ID": dict(LOOM_GITHUB_APP_INSTALLATION_ID="456",
                                       LOOM_GITHUB_APP_KEY_FILE=str(key)),
            "LOOM_GITHUB_APP_KEY_FILE or LOOM_GITHUB_APP_KEY_SSM":
                dict(LOOM_GITHUB_APP_ID="123", LOOM_GITHUB_APP_INSTALLATION_ID="456"),
        }
        for missing, env in cases.items():
            with self.subTest(missing=missing):
                result = self.run_install(**env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(missing + " is missing", result.stderr)
                self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def job_env(self, label):
        plist = (self.home / "Library" / "LaunchAgents" / ("%s.plist" % label)).read_text()
        return dict(re.findall(r"<key>(LOOM_[A-Z_]*GITHUB_APP_[A-Z_]+)</key><string>([^<]*)</string>",
                               plist))

    def test_each_writer_gets_its_own_app_in_its_own_job(self):
        # loom#1142: the lanes and the brain post as their own Apps.
        result = self.run_install(
            LOOM_GITHUB_APP_ID="1", LOOM_GITHUB_APP_INSTALLATION_ID="2",
            LOOM_GITHUB_APP_KEY_SSM="/station/key",
            LOOM_LANES_GITHUB_APP_ID="11", LOOM_LANES_GITHUB_APP_INSTALLATION_ID="12",
            LOOM_LANES_GITHUB_APP_KEY_SSM="/lanes/key",
            LOOM_BRAIN_GITHUB_APP_ID="21", LOOM_BRAIN_GITHUB_APP_INSTALLATION_ID="22",
            LOOM_BRAIN_GITHUB_APP_KEY_SSM="/brain/key")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.job_env("com.loom.codex"), {
            "LOOM_GITHUB_APP_ID": "11", "LOOM_GITHUB_APP_INSTALLATION_ID": "12",
            "LOOM_GITHUB_APP_KEY_SSM": "/lanes/key"})
        self.assertEqual(self.job_env("com.loom.brain"), {
            "LOOM_GITHUB_APP_ID": "21", "LOOM_GITHUB_APP_INSTALLATION_ID": "22",
            "LOOM_GITHUB_APP_KEY_SSM": "/brain/key"})
        station = self.job_env("com.loom.station")
        self.assertEqual((station["LOOM_GITHUB_APP_ID"], station["LOOM_GITHUB_APP_KEY_SSM"]),
                         ("1", "/station/key"))
        self.assertFalse([k for k in station if not k.startswith("LOOM_GITHUB_APP_")], station)

    def test_without_their_own_app_a_lane_gets_none_and_the_brain_keeps_the_stations(self):
        result = self.run_install(LOOM_GITHUB_APP_ID="1", LOOM_GITHUB_APP_INSTALLATION_ID="2",
                                  LOOM_GITHUB_APP_KEY_SSM="/station/key")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.job_env("com.loom.codex"), {})
        self.assertEqual(self.job_env("com.loom.brain")["LOOM_GITHUB_APP_ID"], "1")

    def test_half_a_lanes_app_installs_nothing(self):
        result = self.run_install(LOOM_LANES_GITHUB_APP_ID="11",
                                  LOOM_LANES_GITHUB_APP_KEY_SSM="/lanes/key")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("LOOM_LANES_GITHUB_APP_INSTALLATION_ID is missing", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_the_app_id_alone_names_the_missing_installation_id(self):
        result = self.run_install(LOOM_GITHUB_APP_ID="123")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("LOOM_GITHUB_APP_INSTALLATION_ID", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_a_key_file_readable_by_others_is_refused(self):
        key = self.app_key(0o644)
        result = self.run_install(LOOM_GITHUB_APP_ID="123",
                                  LOOM_GITHUB_APP_INSTALLATION_ID="456",
                                  LOOM_GITHUB_APP_KEY_FILE=str(key))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(str(key), result.stderr)
        self.assertIn("644", result.stderr)
        self.assertIn("600", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_a_missing_key_file_is_refused(self):
        key = self.tmp / "absent.pem"
        result = self.run_install(LOOM_GITHUB_APP_ID="123",
                                  LOOM_GITHUB_APP_INSTALLATION_ID="456",
                                  LOOM_GITHUB_APP_KEY_FILE=str(key))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(str(key), result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_an_unreadable_key_file_is_refused(self):
        # Mode 000 has no group or other bits, but the station could not read it.
        key = self.app_key(0o000)
        self.addCleanup(key.chmod, 0o600)
        result = self.run_install(LOOM_GITHUB_APP_ID="123",
                                  LOOM_GITHUB_APP_INSTALLATION_ID="456",
                                  LOOM_GITHUB_APP_KEY_FILE=str(key))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(str(key), result.stderr)
        self.assertIn("can read", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_a_key_file_whose_mode_cannot_be_read_is_refused(self):
        key = self.app_key(0o600)
        fake_stat = self.bin / "stat"
        fake_stat.write_text("#!/usr/bin/env bash\nexit 1\n")
        fake_stat.chmod(0o755)
        result = self.run_install(LOOM_GITHUB_APP_ID="123",
                                  LOOM_GITHUB_APP_INSTALLATION_ID="456",
                                  LOOM_GITHUB_APP_KEY_FILE=str(key))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("could not read its mode", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_no_app_settings_install_as_before(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_GITHUB_APP_ID</key><string></string>", plist)

    # A second operator's settings (docs/LANES.md, Two Looms on shared repos):
    # the status repo and every watched repo must belong to LOOM_REPO_OWNERS.
    def set_origin(self, url):
        subprocess.run(["git", "init", "-q", str(self.checkout)], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "remote", "add", "origin", url],
                       check=True)

    def test_an_unset_status_repo_outside_the_owners_installs_nothing(self):
        self.set_origin("git@github.com:b/loom.git")
        result = self.run_install(LOOM_REPOS="a/x", LOOM_REPO_OWNERS="a")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOOM_STATUS_REPO", result.stderr)
        self.assertIn("b/loom", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_a_status_repo_of_another_owner_installs_nothing(self):
        result = self.run_install(LOOM_REPOS="a/x", LOOM_REPO_OWNERS="a",
                                  LOOM_STATUS_REPO="b/loom")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOOM_STATUS_REPO", result.stderr)
        self.assertIn("b/loom", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_a_watched_repo_of_another_owner_installs_nothing(self):
        result = self.run_install(LOOM_REPOS="a/x c/y", LOOM_REPO_OWNERS="a",
                                  LOOM_STATUS_REPO="a/x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("c/y", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_consistent_owner_settings_install_as_before(self):
        result = self.run_install(LOOM_REPOS="a/x", LOOM_REPO_OWNERS="a",
                                  LOOM_STATUS_REPO="a/x")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()
        self.assertIn("<key>LOOM_STATUS_REPO</key><string>a/x</string>", plist)

    def test_an_unset_status_repo_of_an_own_owner_installs(self):
        # The single-operator default: origin's owner is the default owner.
        self.set_origin("https://github.com/A/loom.git")
        result = self.run_install(LOOM_REPOS="a/x, A/z", LOOM_REPO_OWNERS="a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("installed com.loom.station", result.stdout)


    # The dashboard agent (issue 678): LOOM_DASHBOARD=1 installs it,
    # LOOM_DASHBOARD=0 removes it, unset leaves it alone.
    DASHBOARD_KEYS = ("LOOM_DASHBOARD", "LOOM_DASHBOARD_HOST", "LOOM_DASHBOARD_PORT",
                      "LOOM_TRANSCRIPTS_MEMORY", "LOOM_DB_PASSWORD")

    def run_dashboard_install(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in self.DASHBOARD_KEYS}
        with unittest.mock.patch.dict(os.environ, clean, clear=True):
            return self.run_install(**env)

    def dashboard_plist(self):
        return self.home / "Library" / "LaunchAgents" / "com.loom.dashboard.plist"

    def assert_valid_plist(self, path):
        plutil = shutil.which("plutil")
        if plutil:
            lint = subprocess.run([plutil, "-lint", str(path)], capture_output=True, text=True)
            self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_dashboard_is_installed_on_the_given_host_with_memory_transcripts(self):
        result = self.run_dashboard_install(LOOM_DASHBOARD="1", LOOM_DASHBOARD_HOST="127.0.0.1",
                                            LOOM_DASHBOARD_PORT="8181",
                                            LOOM_DASHBOARD_BUDGET_FLOOR="250")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.dashboard_plist().read_text()
        self.assertIn("<key>KeepAlive</key><true/>", plist)
        self.assertIn("<string>-m</string>\n    <string>dashboard</string>", plist)
        self.assertIn("<key>WorkingDirectory</key><string>%s</string>" % self.checkout, plist)
        self.assertIn("<key>LOOM_DASHBOARD_HOST</key><string>127.0.0.1</string>", plist)
        self.assertIn("<key>LOOM_DASHBOARD_PORT</key><string>8181</string>", plist)
        self.assertIn("<key>LOOM_TRANSCRIPTS_MEMORY</key><string>1</string>", plist)
        self.assertIn("<key>LOOM_DASHBOARD_BUDGET_FLOOR</key><string>250</string>", plist)
        for key in ("LOOM_DASHBOARD_HOST", "LOOM_DASHBOARD_PORT", "LOOM_TRANSCRIPTS_MEMORY"):
            self.assertEqual(plist.count("<key>%s</key>" % key), 1, key)
        self.assertNotIn("<key>LOOM_DASHBOARD</key>", plist)
        self.assert_valid_plist(self.dashboard_plist())
        self.assertIn("transcripts are kept in memory", result.stdout)
        self.assertIn("launchctl bootstrap gui/%d %s" % (os.getuid(), self.dashboard_plist()),
                      self.log.read_text())

    def test_a_crafted_loom_value_stays_one_plist_string(self):
        # A LOOM_* value is XML-escaped into an EnvironmentVariables string, so
        # it cannot close the string and add a key launchd would run.
        crafted = "</string><key>ProgramArguments</key><array><string>/bin/sh</string></array><string>"
        result = self.run_dashboard_install(LOOM_DASHBOARD="1", LOOM_DASHBOARD_BUDGET_FLOOR=crafted)
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.dashboard_plist().read_text()
        self.assertEqual(plist.count("<key>ProgramArguments</key>"), 1)
        self.assertNotIn("<string>/bin/sh</string>", plist)
        self.assertIn("<key>LOOM_DASHBOARD_BUDGET_FLOOR</key><string>&lt;/string&gt;", plist)
        self.assert_valid_plist(self.dashboard_plist())

    def test_dashboard_keeps_the_database_when_it_is_configured(self):
        result = self.run_dashboard_install(LOOM_DASHBOARD="1", LOOM_DB_PASSWORD="placeholder")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.dashboard_plist().read_text()
        self.assertIn("<key>LOOM_TRANSCRIPTS_MEMORY</key><string></string>", plist)
        self.assertIn("<key>LOOM_DB_PASSWORD</key><string>placeholder</string>", plist)
        self.assertIn("<key>LOOM_DASHBOARD_HOST</key><string></string>", plist)
        self.assertIn("<key>LOOM_DASHBOARD_PORT</key><string>8080</string>", plist)
        self.assertNotIn("transcripts are kept in memory", result.stdout)

    def test_dashboard_zero_removes_an_installed_dashboard(self):
        self.assertEqual(self.run_dashboard_install(LOOM_DASHBOARD="1").returncode, 0)
        self.assertTrue(self.dashboard_plist().exists())
        result = self.run_dashboard_install(LOOM_DASHBOARD="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dashboard_plist().exists())
        self.assertIn("removed com.loom.dashboard", result.stdout)
        self.assertIn("launchctl bootout gui/%d/com.loom.dashboard" % os.getuid(),
                      self.log.read_text())

    def test_dashboard_unset_installs_nothing(self):
        result = self.run_dashboard_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dashboard_plist().exists())
        self.assertNotIn("com.loom.dashboard", result.stdout)

    def test_a_dashboard_host_that_is_not_one_ipv4_address_installs_nothing(self):
        for bad in ("127.0.0.1; true", "dashboard.example", "127.0.0.1 10.0.0.1", "01.2.3.4", "10.0.0.08", "300.1.1.1"):
            with self.subTest(bad=bad):
                result = self.run_dashboard_install(LOOM_DASHBOARD="1", LOOM_DASHBOARD_HOST=bad)
                self.assertEqual(result.returncode, 1)
                self.assertIn("LOOM_DASHBOARD_HOST", result.stderr)
                self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_a_bad_dashboard_port_installs_nothing(self):
        result = self.run_dashboard_install(LOOM_DASHBOARD="1", LOOM_DASHBOARD_PORT="web")
        self.assertEqual(result.returncode, 1)
        self.assertIn("LOOM_DASHBOARD_PORT", result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())


if __name__ == "__main__":
    unittest.main()
