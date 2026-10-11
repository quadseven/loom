"""lanes/run_lane.sh run as a process: a temp loom home, a local bare repo as
the remote, and fake agent and gh commands on PATH that record their calls."""

import subprocess
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh"

BRIEF = """# Brief: T042-fix-the-thing

Repo: example/repo
Branch: loom/T042-fix-the-thing
Lane: codex
Difficulty: medium

## Ticket

# T042: Fix the thing

## Why
The thing is broken for every guild.

## What
Make the thing work.

## Acceptance criteria
- [ ] The thing works
- [ ] A test covers the thing
- [ ] Nothing else changes

## Size
S: one function.

## Out of scope
- Other things
"""

# Fake gh: $FAKE_GH_EXISTING is the number of a MERGED PR on the branch.
# Like real gh, `pr list` shows it only when asked for all states.
# `pr create` prints a URL. Every call is appended to $FAKE_LOG.
FAKE_GH = """#!/usr/bin/env bash
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_GH_HANG:-}" ]; then exec sleep 30; fi
if [ "$1 $2" = "pr list" ]; then
  case " $* " in *" --state all "*) printf '%s' "${FAKE_GH_EXISTING:-}" ;; esac
fi
if [ "$1 $2" = "pr create" ]; then
  while [ $# -gt 0 ]; do [ "$1" = "--body" ] && printf '%s' "$2" > "$FAKE_BODY"; shift; done
  echo "https://github.com/example/repo/pull/7"
fi
"""

# Fake agent: records the call, then commits a file ($FAKE_AGENT=commit),
# only edits it the way a sandboxed codex must ($FAKE_AGENT=edit), or exits
# non-zero ($FAKE_AGENT=fail).
FAKE_AGENT = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$FAKE_LOG"
cat > /dev/null
if [ "${FAKE_AGENT:-commit}" = fail ]; then exit 3; fi
echo fixed > fix.txt
if [ "${FAKE_AGENT:-commit}" = edit ]; then exit 0; fi
git add fix.txt
git -c user.name=t -c user.email=t@example.invalid commit -qm "Fix the thing"
"""


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class LaneRunnerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.loom = self.home / "loom"
        self.inbox = self.loom / "briefs" / "codex"
        self.inbox.mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.touch()
        self.body = root / "body.md"

        remotes = root / "remotes"
        self.remote = remotes / "example" / "repo.git"
        seed = root / "seed"
        git("init", "-q", "-b", "main", str(seed))
        (seed / "README").write_text("seed\n")
        git("add", "README", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "seed", cwd=seed)
        self.remote.parent.mkdir(parents=True)
        git("clone", "-q", "--bare", str(seed), str(self.remote))
        self.clone_base = f"file://{remotes}/"

        self.write_tool(self.bin / "gh", FAKE_GH)
        self.write_tool(self.bin / "codex", FAKE_AGENT)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def write_tool(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def drop_brief(self, name="T042-fix-the-thing.md", text=BRIEF):
        (self.inbox / name).write_text(text)

    def run_lane(self, lane="codex", **env):
        full = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "LOOM_CLONE_BASE": self.clone_base,
            "FAKE_LOG": str(self.log),
            "FAKE_BODY": str(self.body),
            "LOOM_REPO_OWNERS": "example",
            "LOOM_CODEX_MODEL": "gpt-test-default",
            "LOOM_CLAUDE_MODEL": "claude-test-default",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            **env,
        }
        return subprocess.run(["bash", str(RUNNER), lane], env=full,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_brief_with_an_existing_pr_is_retired_without_running(self):
        self.drop_brief()
        result = self.run_lane(FAKE_GH_EXISTING="852")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        self.assertFalse((self.inbox / "T042-fix-the-thing.md").exists())
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_agent_commit_is_pushed_and_opened_as_a_chief_ready_pr(self):
        self.drop_brief()
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        heads = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "loom/T042-fix-the-thing"],
            capture_output=True, text=True, check=True).stdout
        self.assertIn("refs/heads/loom/T042-fix-the-thing", heads)
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertFalse(any(c.startswith("gh pr merge") for c in self.calls()))
        body = self.body.read_text()
        self.assertIn("## Why\nThe thing is broken for every guild.", body)
        self.assertIn("## Acceptance criteria\n- [ ] The thing works\n- [ ] A test covers the thing\n- [ ] Nothing else changes", body)
        self.assertIn("\nSize: S\n", body)
        self.assertIn("## Out of scope\n- Other things", body)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_uncommitted_agent_edits_are_committed_by_the_runner(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="edit")
        self.assertEqual(result.returncode, 0, result.stderr)
        files = subprocess.run(
            ["git", "--git-dir", str(self.remote), "show", "--name-only", "--format=%s",
             "loom/T042-fix-the-thing"], capture_output=True, text=True).stdout
        self.assertIn("fix.txt", files)
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_brief_base_branch_is_cloned_and_targeted_by_the_pr(self):
        # The remote's default branch is not where work lands: seed one with
        # a different tree and name the real base in the brief.
        work = Path(self._tmp.name) / "work"
        git("clone", "-q", str(self.remote), str(work))
        git("checkout", "-q", "-b", "develop", cwd=work)
        (work / "ONLY_ON_DEVELOP").write_text("x\n")
        git("add", "ONLY_ON_DEVELOP", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "develop", cwd=work)
        git("push", "-q", "origin", "develop", cwd=work)
        self.drop_brief(text=BRIEF.replace("Branch: loom/T042-fix-the-thing",
                                           "Branch: loom/T042-fix-the-thing\nBase: develop"))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        files = subprocess.run(
            ["git", "--git-dir", str(self.remote), "ls-tree", "--name-only",
             "loom/T042-fix-the-thing"], capture_output=True, text=True).stdout.split()
        self.assertIn("ONLY_ON_DEVELOP", files)
        create = [c for c in self.calls() if c.startswith("gh pr create")]
        self.assertEqual(len(create), 1)
        self.assertIn("--base develop", create[0])

    def test_pr_footer_names_the_model_that_ran(self):
        self.drop_brief(text=BRIEF.replace("Difficulty: medium", "Difficulty: medium\nModel: gpt-test-sol"))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        agent = [c for c in self.calls() if c.startswith("codex ")]
        self.assertIn("--model gpt-test-sol", agent[0])
        self.assertIn("lane codex, model gpt-test-sol", self.body.read_text())

    def test_lane_default_model_is_used_and_named_when_brief_has_none(self):
        self.drop_brief()
        self.run_lane()
        agent = [c for c in self.calls() if c.startswith("codex ")]
        self.assertIn("--model gpt-test-default", agent[0])
        self.assertIn("lane codex, model gpt-test-default", self.body.read_text())

    def test_a_model_from_another_lanes_family_is_refused(self):
        self.drop_brief(text=BRIEF.replace("Difficulty: medium", "Difficulty: medium\nModel: claude-sonnet-4-5"))
        self.run_lane()
        self.assertFalse(any(c.startswith("codex ") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_failed_run_is_parked_once_and_the_next_brief_still_runs(self):
        self.drop_brief("T001-broken.md", BRIEF.replace("T042-fix-the-thing", "T001-broken"))
        self.drop_brief("T002-later.md", BRIEF.replace("T042-fix-the-thing", "T002-later"))
        first = self.run_lane(FAKE_AGENT="fail")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((self.inbox / "failed" / "T001-broken.md").exists())
        self.assertTrue((self.inbox / "failed" / "T002-later.md").exists())
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.remote)],
                               capture_output=True, text=True, check=True).stdout
        self.assertNotIn("loom/", heads)
        # A second pass finds an empty inbox: a failure is never re-run.
        self.log.write_text("")
        self.run_lane()
        self.assertEqual(self.calls(), [])

    def test_brief_aimed_at_a_non_loom_branch_is_refused_before_running(self):
        self.drop_brief(text=BRIEF.replace("Branch: loom/T042-fix-the-thing", "Branch: hotfix"))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_brief_for_a_repo_outside_the_allowed_owners_is_refused(self):
        # The agent trusts the cloned repo's own config, so only clone repos
        # the operator owns.
        self.drop_brief(text=BRIEF.replace("Repo: example/repo", "Repo: stranger/repo"))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_an_empty_owner_list_refuses_every_brief(self):
        self.drop_brief()
        result = self.run_lane(LOOM_REPO_OWNERS="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_a_hung_gh_call_leaves_the_brief_for_the_next_pass(self):
        self.drop_brief()
        result = self.run_lane(FAKE_GH_HANG="1", LOOM_NET_TIMEOUT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        self.assertTrue((self.inbox / "T042-fix-the-thing.md").exists())

    def test_claude_lane_finds_the_cli_in_local_bin_under_a_bare_path(self):
        # launchd's PATH has no ~/.local/bin, where the official CLI installs.
        self.write_tool(self.home / ".local" / "bin" / "claude", FAKE_AGENT)
        inbox = self.loom / "briefs" / "claude"
        inbox.mkdir(parents=True)
        (inbox / "T042-fix-the-thing.md").write_text(BRIEF.replace("Lane: codex", "Lane: claude"))
        result = self.run_lane("claude", PATH=f"{self.bin}:/usr/bin:/bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith("claude ") for c in self.calls()), result.stderr)
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists(), result.stderr)


if __name__ == "__main__":
    unittest.main()
