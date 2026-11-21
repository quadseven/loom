"""lanes/mac/station.sh run as a process: a local bare repo as the remote, a
checkout made from it, and fake gh and python3 on PATH."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

LANES = Path(__file__).resolve().parent.parent / "lanes"
SCRIPT = LANES / "mac" / "station.sh"

# Fake gh: returns check-run and revert-ticket API responses.
FAKE_GH = """#!/usr/bin/env bash
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
[ "$GH_TOKEN" = ghs_station ] || { echo "gh ran as someone else" >&2; exit 1; }
if [ "${FAKE_FAIL:-}" = 1 ]; then echo 'API rate limit exceeded' >&2; exit 1; fi
case "$*" in
  *loom:revert*) jq -n --arg body "${FAKE_REVERTS:-}" '[{"body":$body,"labels":[]}]' ;;
  *)
    for green in ${FAKE_GREEN:-}; do
      case "$*" in *"$green"*)
        printf '{"check_runs":[{"conclusion":"success","status":"completed"}]}\\n'; exit 0 ;;
      esac
    done
    if [ "${FAKE_CHECK_STATE:-completed}" = in_progress ]; then
      printf '{"check_runs":[{"conclusion":null,"status":"in_progress"}]}\\n'
    else
      printf '{"check_runs":[{"conclusion":"%s","status":"completed"}]}\\n' "${FAKE_CHECK:-success}"
    fi
    ;;
esac
"""
# Fake python3: records that the station pass ran, from which directory.
FAKE_PY = """#!/usr/bin/env bash
printf 'python3 %s in %s\\n' "$*" "$PWD" >> "$FAKE_LOG"
"""


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   cwd=cwd, check=True, capture_output=True)


class StationScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = self.tmp / "seed"
        git("clone", "-q", str(self.remote), str(seed))
        (seed / "a.txt").write_text("one\n")
        git("add", "a.txt", cwd=seed)
        git("commit", "-qm", "one", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.seed = seed
        # The checkout under test holds the script, as the real one does.
        self.checkout = self.tmp / "loom"
        git("clone", "-q", str(self.remote), str(self.checkout))
        (self.checkout / "lanes" / "mac").mkdir(parents=True)
        shutil.copy(SCRIPT, self.checkout / "lanes" / "mac" / "station.sh")
        # The station's GitHub identity (loom#3083), as the real checkout has it.
        shutil.copy(LANES / "github_auth.sh", self.checkout / "lanes" / "github_auth.sh")
        shutil.copy(LANES / "fleet-token", self.checkout / "lanes" / "fleet-token")
        shutil.copytree(LANES / "github-bin", self.checkout / "lanes" / "github-bin")
        (self.checkout / ".git" / "info" / "exclude").write_text("lanes/\n")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("gh", FAKE_GH), ("python3", FAKE_PY)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.mint = self.tmp / "mint"
        self.mint.write_text('#!/usr/bin/env bash\necho mint >> "$FAKE_LOG"\n'
                             '[ -z "${FAKE_NO_TOKEN:-}" ] || exit 2\necho ghs_station\n')
        self.mint.chmod(0o755)

    def push_change(self, text="two\n"):
        (self.seed / "a.txt").write_text(text)
        git("commit", "-qam", "Change a.txt", cwd=self.seed)
        git("push", "-q", "origin", "HEAD:main", cwd=self.seed)
        return subprocess.run(["git", "-C", str(self.seed), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()

    def head(self):
        return subprocess.run(["git", "-C", str(self.checkout), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()

    def run_script(self, **env):
        full = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GH_CONFIG_DIR")}
        full.update(PATH="%s:%s" % (self.bin, os.environ["PATH"]), FAKE_LOG=str(self.log),
                    LOOM_HOME=str(self.tmp / "home"), LOOM_GITHUB_APP_ID="124",
                    LOOM_APP_TOKEN_CMD=str(self.mint), **env)
        return subprocess.run(["bash", str(self.checkout / "lanes" / "mac" / "station.sh")],
                              env=full, capture_output=True, text=True, timeout=60)

    def deployed(self):
        """The deploy record the station's pass ships tickets from, or None."""
        path = self.tmp / "home" / "state" / "station" / "deployed.json"
        return json.loads(path.read_text()) if path.exists() else None

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_green_head_is_deployed_then_the_station_runs(self):
        target = self.push_change()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(), target)
        self.assertIn("station: deployed %s: Change a.txt" % target[:7], result.stdout)
        self.assertEqual(self.calls()[-1], "python3 -m bus station in %s" % self.checkout)
        record = self.deployed()
        self.assertEqual(record["revision"], target)
        self.assertLessEqual(abs(record["at"] - time.time()), 60)

    def test_configured_pin_pass_runs_after_deploy_and_retries_next_pass(self):
        self.push_change()
        settings = {"LOOM_PIN_TARGETS": '[{"repo":"example-owner/deploy","path":"pin.env",'
                    '"revision_var":"REV","sha256_var":"SHA"}]',
                    "LOOM_SELF_REPO": "example-owner/loom"}
        self.run_script(**settings)
        calls = self.calls()
        self.assertTrue(calls[-2].startswith("python3 -m bus.pin"), calls)
        self.assertTrue(calls[-1].startswith("python3 -m bus station"), calls)
        self.run_script(**settings)
        self.assertEqual(sum("python3 -m bus.pin" in call for call in self.calls()), 2)

    def test_a_head_whose_ci_has_not_passed_waits(self):
        before = self.head()
        self.push_change()
        for verdict in ("none", "other"):
            result = self.run_script(FAKE_CHECK=verdict)
            self.assertEqual(self.head(), before)
            self.assertNotIn("deployed", result.stdout)
        # A deploy that did not happen records nothing, so nothing ships.
        self.assertIsNone(self.deployed())
        self.assertTrue(self.calls()[-1].startswith("python3 -m bus station"))

    def test_a_failed_check_read_says_why_deploy_did_not_happen(self):
        before = self.head()
        self.push_change()
        result = self.run_script(FAKE_FAIL="1")
        self.assertEqual(self.head(), before)
        self.assertIn("not deploying: the CI check could not be read", result.stdout)
        self.assertIn("API rate limit exceeded", result.stdout)

    def test_a_pending_head_deploys_the_newest_commit_that_passed(self):
        # 2026-10-18: merges landed faster than CI cleared them, and waiting
        # for the head alone deployed nothing for over an hour.
        older = self.push_change("two\n")
        newest = self.push_change("three\n")
        result = self.run_script(FAKE_CHECK_STATE="in_progress", FAKE_GREEN=older)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(), older)
        self.assertIn("deploying %s, the newest commit unittest passed on, while %s waits"
                      % (older[:12], newest[:12]), result.stdout)
        self.assertEqual(self.deployed()["revision"], older)

    def test_with_no_older_commit_passed_nothing_deploys(self):
        before = self.head()
        self.push_change("two\n")
        self.push_change("three\n")
        result = self.run_script(FAKE_CHECK_STATE="in_progress")
        self.assertEqual(self.head(), before)
        self.assertNotIn("deploy", result.stdout.replace("not deploying", ""))
        self.assertIsNone(self.deployed())

    def test_a_pending_check_says_which_state_is_waiting(self):
        before = self.head()
        self.push_change()
        result = self.run_script(FAKE_CHECK_STATE="in_progress")
        self.assertEqual(self.head(), before)
        self.assertIn("waiting: unittest is in_progress on", result.stdout)

    def test_a_head_that_brings_in_a_commit_a_revert_names_waits(self):
        before = self.head()
        bad = self.push_change()
        self.push_change("three\n")
        reverts = "## What\n- Start from the default branch and run `git revert --no-edit %s`." % bad
        result = self.run_script(FAKE_REVERTS=reverts)
        self.assertEqual(self.head(), before)
        self.assertIn("not deploying: %s is named by a revert ticket" % bad[:12], result.stdout)
        self.assertIsNone(self.deployed())
        self.assertTrue(self.calls()[-1].startswith("python3 -m bus station"))

    def test_a_named_commit_already_deployed_does_not_hold_the_next_one(self):
        bad = self.push_change()
        self.run_script()
        self.assertEqual(self.head(), bad)
        target = self.push_change("three\n")
        result = self.run_script(FAKE_REVERTS="git revert --no-edit %s" % bad)
        self.assertEqual(self.head(), target, result.stdout)

    def test_local_changes_are_never_overwritten(self):
        before = self.head()
        self.push_change()
        (self.checkout / "a.txt").write_text("edited here\n")
        result = self.run_script()
        self.assertEqual(self.head(), before)
        self.assertEqual((self.checkout / "a.txt").read_text(), "edited here\n")
        self.assertIn("not deploying: the checkout has local changes", result.stdout)

    def test_a_diverged_checkout_is_not_moved(self):
        self.push_change()
        (self.checkout / "b.txt").write_text("local\n")
        git("add", "b.txt", cwd=self.checkout)
        git("commit", "-qm", "local", cwd=self.checkout)
        local = self.head()
        result = self.run_script()
        self.assertEqual(self.head(), local)
        self.assertIn("not a fast-forward", result.stdout)

    def test_another_branch_or_deploy_off_is_left_alone(self):
        before = self.head()
        self.push_change()
        self.run_script(LOOM_DEPLOY="0")
        self.assertEqual(self.head(), before)
        git("checkout", "-q", "-b", "work", cwd=self.checkout)
        self.run_script()
        self.assertEqual(self.head(), before)
        self.assertFalse([c for c in self.calls() if c.startswith("gh ")])

    def test_a_pass_whose_app_token_cannot_be_minted_calls_no_github(self):
        # loom#3083: gh fell back to the operator's stored login.
        before = self.head()
        self.push_change()
        result = self.run_script(FAKE_NO_TOKEN="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not mint the station's GitHub App token", result.stdout)
        self.assertEqual(self.calls(), ["mint"])
        self.assertEqual(self.head(), before)

    def test_a_pass_with_an_ambient_gh_token_stops_before_github(self):
        self.push_change()
        result = self.run_script(GH_TOKEN="gho_person")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambient GH_TOKEN", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_pass_reads_github_through_the_wrapper_with_an_empty_config(self):
        self.write_env_probe()
        self.push_change()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = [c for c in self.calls() if c.startswith("probe ")]
        self.assertTrue(probe, self.calls())
        token, real, entries = probe[0].split()[1:]
        self.assertEqual((token, entries), ("ghs_station", "0"))
        self.assertEqual(real, str(self.bin / "gh"))

    def write_env_probe(self):
        body = (self.bin / "gh").read_text().replace(
            "#!/usr/bin/env bash\n", '#!/usr/bin/env bash\nprintf "probe %s %s %s\\n" '
            '"$GH_TOKEN" "$LOOM_REAL_GH" "$(ls -A "$GH_CONFIG_DIR" | wc -l | tr -d " ")" '
            '>> "$FAKE_LOG"\n', 1)
        (self.bin / "gh").write_text(body)


if __name__ == "__main__":
    unittest.main()
