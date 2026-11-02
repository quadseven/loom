"""Brain duty is shared: any fleet brain takes a brain tick under its own
GitHub identity, and the brain lock, not the token, decides who acts.

A LOOM_BRIEF_AUTHORS login (the Loom Brain App, the operator) acts as
before. A LOOM_TRUSTED_BRIEFERS login (a fleet brain such as Muse or Codex)
runs brain write commands only while it holds the live brain lock taken
under its own login, and what it writes then (journals, handoff markers,
proofs) is read as a brain's."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import brain, lock  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests.test_bus import REPO, FakeGit, stamp  # noqa: E402

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
MUSE = "agent-meta-muse[bot]"
CODEX = "agent-openai-codex-luna[bot]"
ENV = {"LOOM_BRIEF_AUTHORS": "operator loom-brain[bot]",
       "LOOM_TRUSTED_BRIEFERS": "%s %s" % (MUSE, CODEX)}


def env(login):
    return dict(ENV, LOOM_GITHUB_LOGIN=login)


class LockLoginTests(unittest.TestCase):
    def test_the_lock_records_the_takers_login(self):
        g = FakeGit()
        got = lock.attempt(GitHub("tok", transport=g), REPO, "muse", 30, NOW, login=MUSE)
        self.assertTrue(got.acquired)
        self.assertIn("login=%s" % MUSE, g.lock_message().splitlines()[0])
        self.assertEqual(MUSE, lock.read_holder(GitHub("tok", transport=g), REPO).login)

    def test_a_lock_without_a_login_reads_as_none(self):
        g = FakeGit()
        g.seed_lock("lane-machine", stamp(NOW + timedelta(minutes=30)))
        self.assertEqual("", lock.read_holder(GitHub("tok", transport=g), REPO).login)

    def test_a_login_with_a_space_is_refused(self):
        with self.assertRaises(ValueError):
            lock.attempt(GitHub("tok", transport=FakeGit()), REPO, "muse", 30, NOW,
                         login="bad login")


class ActingBrainTests(unittest.TestCase):
    def gh_with_lock(self, holder, login, minutes=30):
        g = FakeGit()
        if holder:
            lock.attempt(GitHub("tok", transport=g), REPO, holder, minutes,
                         NOW - timedelta(minutes=1), login=login)
        return GitHub("tok", transport=g)

    def test_a_loom_login_acts_without_the_lock(self):
        gh = self.gh_with_lock("", "")
        self.assertIsNone(brain.check_acting_brain(gh, REPO, env("loom-brain[bot]"), NOW))

    def test_a_fleet_brain_holding_the_lock_acts(self):
        gh = self.gh_with_lock("muse", MUSE)
        self.assertIsNone(brain.check_acting_brain(gh, REPO, env(MUSE), NOW))

    def test_a_fleet_brain_without_the_lock_is_refused(self):
        gh = self.gh_with_lock("", "")
        with self.assertRaisesRegex(ValueError, "holds the brain lock"):
            brain.check_acting_brain(gh, REPO, env(MUSE), NOW)

    def test_another_brains_lock_does_not_let_a_fleet_brain_act(self):
        gh = self.gh_with_lock("codex", CODEX)
        with self.assertRaisesRegex(ValueError, "codex"):
            brain.check_acting_brain(gh, REPO, env(MUSE), NOW)

    def test_an_expired_lock_does_not_let_a_fleet_brain_act(self):
        gh = self.gh_with_lock("muse", MUSE, minutes=1)
        with self.assertRaises(ValueError):
            brain.check_acting_brain(gh, REPO, env(MUSE), NOW + timedelta(minutes=5))

    def test_a_lock_that_names_no_login_does_not_let_a_fleet_brain_act(self):
        # The holder name alone is not an identity: anyone can type "muse".
        gh = self.gh_with_lock("muse", "")
        with self.assertRaises(ValueError):
            brain.check_acting_brain(gh, REPO, env(MUSE), NOW)

    def test_the_cli_refuses_a_fleet_brains_write_without_the_lock(self):
        g = FakeGit()
        with mock.patch.dict(os.environ, dict(env(MUSE), LOOM_STATUS_REPO=REPO), clear=True), \
                mock.patch.object(brain, "GitHub", lambda *a, **k: GitHub("tok", transport=g)), \
                mock.patch.object(brain, "auth_from_env", lambda: None), \
                mock.patch.dict(brain.COMMANDS, {"close": lambda gh, args: self.fail("ran")}):
            code = brain.main(["close", "%s#5" % REPO, "--reason", "not_planned",
                               "--note", "done elsewhere"])
        self.assertEqual(1, code)

    def test_the_cli_lets_a_lock_holding_fleet_brain_write(self):
        g = FakeGit()
        lock.attempt(GitHub("tok", transport=g), REPO, "muse", 30, datetime.now(timezone.utc),
                     login=MUSE)
        ran = []
        with mock.patch.dict(os.environ, dict(env(MUSE), LOOM_STATUS_REPO=REPO), clear=True), \
                mock.patch.object(brain, "GitHub", lambda *a, **k: GitHub("tok", transport=g)), \
                mock.patch.object(brain, "auth_from_env", lambda: None), \
                mock.patch.dict(brain.COMMANDS, {"close": lambda gh, args: ran.append(1) or 0}):
            code = brain.main(["close", "%s#5" % REPO, "--reason", "not_planned",
                               "--note", "done elsewhere"])
        self.assertEqual((0, [1]), (code, ran))


class BrainArtifactTrustTests(unittest.TestCase):
    """Read trust stays on LOOM_BRIEF_AUTHORS: a briefer's comment is not a
    brain's proof or journal just because the login is a fleet brain. The
    operator lists a fleet brain in LOOM_BRIEF_AUTHORS deliberately."""

    def proven_by(self, login, env=ENV):
        class Comments:
            def paginate(self, path):
                return [{"user": {"login": login}, "body": "proof", "html_url": "u"}]
        record = {"contract": "c"}
        with mock.patch.object(brain.proof, "check_criteria"), \
                mock.patch.object(brain.proof, "read", lambda body: record), \
                mock.patch.object(brain.proof, "contract", lambda body: "c"), \
                mock.patch.object(brain.proof, "check"):
            return brain._check_proven(Comments(), REPO, 5, {"body": ""}, env)

    def test_a_briefers_proof_is_not_a_brains_proof(self):
        with self.assertRaisesRegex(ValueError, "no loom:proof comment"):
            self.proven_by(MUSE)

    def test_a_fleet_brain_listed_as_a_loom_login_proves(self):
        listed = dict(ENV, LOOM_BRIEF_AUTHORS=ENV["LOOM_BRIEF_AUTHORS"] + " " + MUSE)
        self.assertEqual("u", self.proven_by(MUSE, listed))

    def test_status_and_resume_read_only_loom_logins(self):
        self.assertEqual(["operator", "loom-brain[bot]"],
                         brain.require_trusted_authors(ENV, "resume"))

    def test_a_listed_fleet_brain_still_needs_the_lock_to_write(self):
        # Listing a fleet brain for read trust does not let it act unlocked.
        listed = dict(env(MUSE), LOOM_BRIEF_AUTHORS=ENV["LOOM_BRIEF_AUTHORS"] + " " + MUSE)
        g = FakeGit()
        with self.assertRaises(ValueError):
            brain.check_acting_brain(GitHub("tok", transport=g), REPO, listed, NOW)

    def test_the_loom_brain_app_acts_without_a_login_on_the_lock(self):
        g = FakeGit()
        self.assertIsNone(brain.check_acting_brain(GitHub("tok", transport=g), REPO,
                                                   env("loom-brain[bot]"), NOW))

    def test_an_unknown_login_is_left_to_the_other_gates(self):
        g = FakeGit()
        self.assertIsNone(brain.check_acting_brain(GitHub("tok", transport=g), REPO, ENV, NOW))


if __name__ == "__main__":
    unittest.main()
