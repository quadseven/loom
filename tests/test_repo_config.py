"""Repository targets and settings are configuration (loom#2584).

A fresh clone must not act on the original owner's repositories or labels
because a literal in the code said so: every repository or label the code
acts on is read from a setting, and every operator-facing LOOM_* setting
the code reads is named in .env.example. These tests pin that at the
public seams: the sweep's repo list, the send-back comparison's home
repo, the brain's lock and lanes repo defaults, the dispatcher's default
repo, the invariants person-claim rule, and the .env.example catalogue.
"""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dispatch"))

from bus import __main__ as bus_main  # noqa: E402
from bus import brain, invariants, send_back, sweep  # noqa: E402
import dispatcher  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# The operator-facing settings the audit on #2584 found read by Python.
PYTHON_SETTINGS = (
    "LOOM_BACKLOG_SOURCE", "LOOM_DASHBOARD_TZ", "LOOM_DEPLOY_CHECK",
    "LOOM_MUSE_BUILD_LOGIN", "LOOM_OPENCODE_GO_MODELS_URL",
    "LOOM_RUNNER_REPO", "LOOM_VIEWER_PORT",
)
# The operator-facing settings the audit found read by the lane scripts.
LANE_SETTINGS = (
    "LOOM_AGENT_TIMEOUT", "LOOM_AGY_BIN", "LOOM_AGY_CODESIGN",
    "LOOM_AGY_HOME", "LOOM_AGY_ONLY_MODEL", "LOOM_AGY_RUNAS",
    "LOOM_AGY_USER", "LOOM_ANTHROPIC_MODEL", "LOOM_APP_TOKEN_CMD",
    "LOOM_BRAIN_FABRIC_ASK", "LOOM_CLONE_BASE", "LOOM_CODEX_ONLY_MODEL",
    "LOOM_DEPLOY_BRANCH", "LOOM_EGRESS_PROBE_SECONDS", "LOOM_MUSE_MODEL",
    "LOOM_NET_TIMEOUT", "LOOM_OPENCODE_GO_MODEL", "LOOM_OPENCODE_GO_3_MODEL",
    "LOOM_OPENCODE_GO_4_MODEL", "LOOM_OPENCODE_GO_PROBE_URL",
    "LOOM_OPENROUTER_MODEL", "LOOM_REWRITE_FREE_COOLDOWN",
    "LOOM_REWRITE_FREE_TIMEOUT", "LOOM_REWRITE_FREE_URL",
    "LOOM_REWRITE_OPENCODE", "LOOM_REWRITE_TIMEOUT", "LOOM_SHA256SUM_DIRS",
    "LOOM_SPARK_3_URL", "LOOM_STATION_WAIT", "LOOM_VIEWER",
    "LOOM_WATCHDOG_POLL", "LOOM_WORKERSAI_MODEL", "LOOM_ZAI_MODEL",
    "LOOM_ZEN_MODEL",
)
# Settings this change adds.
NEW_SETTINGS = ("LOOM_SWEEP_REPOS", "LOOM_FACTORY_REPO", "LOOM_INFRA_PUBLIC_REPO")
# Set by one Loom script for another, or by tests: not operator settings,
# so .env.example names them in a comment instead of as keys.
INTERNAL_SETTINGS = ("LOOM_BRAIN_NOW", "LOOM_BRAIN_WAIT_STEP", "LOOM_POD_ONCE")


class SweepReposTest(unittest.TestCase):
    def test_sweep_repos_come_from_the_sweep_setting(self):
        env = {"LOOM_SWEEP_REPOS": "example-owner/one, example-owner/two"}
        self.assertEqual(sweep.sweep_repos(env),
                         ("example-owner/one", "example-owner/two"))

    def test_sweep_repos_fall_back_to_the_fleet_setting(self):
        env = {"LOOM_REPOS": "example-owner/one example-owner/two"}
        self.assertEqual(sweep.sweep_repos(env),
                         ("example-owner/one", "example-owner/two"))

    def test_no_setting_means_no_sweep_repos(self):
        self.assertEqual(sweep.sweep_repos({}), ())


class SendBackRepoTest(unittest.TestCase):
    def test_the_home_repo_is_the_self_repo_setting(self):
        env = {"LOOM_SELF_REPO": "example-owner/self",
               "LOOM_STATUS_REPO": "example-owner/status"}
        self.assertEqual(send_back.repo(env), "example-owner/self")

    def test_the_home_repo_falls_back_to_the_status_repo(self):
        self.assertEqual(send_back.repo({"LOOM_STATUS_REPO": "example-owner/status"}),
                         "example-owner/status")

    def test_no_setting_means_no_home_repo(self):
        self.assertEqual(send_back.repo({}), "")


class PersonClaimTest(unittest.TestCase):
    def test_the_repo_owners_claim_is_not_a_person_claim(self):
        self.assertFalse(invariants.person_claim(["claim:example-owner"],
                                                 "example-owner/factory"))

    def test_any_other_claim_is_a_person_claim(self):
        self.assertTrue(invariants.person_claim(["claim:someone-else"],
                                                "example-owner/factory"))

    def test_no_claim_label_is_not_a_person_claim(self):
        self.assertFalse(invariants.person_claim(["loom:ticket"],
                                                 "example-owner/factory"))


class ParserDefaultsTest(unittest.TestCase):
    ENV = {"LOOM_STATUS_REPO": "example-owner/status",
           "LOOM_SELF_REPO": "example-owner/self"}

    def test_brain_lock_repo_default_is_the_status_setting(self):
        with mock.patch.dict(os.environ, self.ENV):
            args = brain.parser().parse_args(["step", "--show"])
        self.assertEqual(args.lock_repo, "example-owner/status")

    def test_brain_lanes_repo_default_is_the_self_setting(self):
        with mock.patch.dict(os.environ, self.ENV):
            args = brain.parser().parse_args(["status", "example-owner/one"])
        self.assertEqual(args.lanes_repo, "example-owner/self")

    def test_bus_lock_repo_default_is_the_status_setting(self):
        with mock.patch.dict(os.environ, self.ENV):
            args = bus_main._parser().parse_args(
                ["journal", "example-owner/one" + "#1", "--file", "x"])
        self.assertEqual(args.lock_repo, "example-owner/status")

    def test_no_setting_means_no_default_repo(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = brain.parser().parse_args(["step", "--show"])
        self.assertEqual(args.lock_repo, "")


class DispatcherDefaultTest(unittest.TestCase):
    def test_a_ticket_without_a_repo_uses_the_status_setting(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ticket = root / "T042-fix.md"
            ticket.write_text("# T042: Fix\n\n## Why\nBroken.\n")
            with mock.patch.dict(os.environ,
                                 {"LOOM_STATUS_REPO": "example-owner/self"}):
                with mock.patch.object(dispatcher, "BRIEFS", root / "briefs"):
                    brief = dispatcher.render_brief(ticket, "codex").read_text()
        self.assertIn("Repo: example-owner/self\n", brief)


class EnvExampleTest(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".env.example").read_text()
        self.keys = set(re.findall(r"^(LOOM_[A-Z0-9_]+)=", self.text, re.M))

    def test_every_operator_facing_setting_is_a_key(self):
        missing = [name for name in PYTHON_SETTINGS + LANE_SETTINGS + NEW_SETTINGS
                   if name not in self.keys]
        self.assertEqual(missing, [])

    def test_internal_settings_are_named_as_internal_in_a_comment(self):
        comments = "\n".join(line for line in self.text.splitlines()
                             if line.lstrip().startswith("#"))
        for name in INTERNAL_SETTINGS:
            self.assertIn(name, comments)
        self.assertIn("internal", comments.lower())


class NoOwnerLiteralTest(unittest.TestCase):
    """No code line in the touched modules carries the original owner's
    repo as a string literal the code acts on."""

    FILES = ("bus/invariants.py", "bus/send_back.py", "bus/sweep.py",
             "bus/__main__.py", "bus/brain.py", "bus/app.py",
             "bus/station.py", "dispatch/dispatcher.py")

    def test_no_owner_repo_literal_in_code(self):
        bad = []
        for name in self.FILES:
            for number, line in enumerate(
                    (ROOT / name).read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"""["']quadseven/""", line) or "github.com/quadseven" in line:
                    bad.append("%s:%d" % (name, number))
        self.assertEqual(bad, [])

    def test_the_lane_script_factory_repo_is_a_setting(self):
        text = (ROOT / "lanes" / "run_lane.sh").read_text()
        self.assertNotIn("\nFACTORY_REPO=quadseven/", text)
        self.assertIn("LOOM_FACTORY_REPO", text)


if __name__ == "__main__":
    unittest.main()
