"""lanes/run_lane.sh run as a process: a temp loom home, a local bare repo as
the remote, and fake agent and gh commands on PATH that record their calls."""

import json
import shutil
import socket
import subprocess
import tempfile
import time
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
# `pr create` prints a URL. `api repos/O/N --jq .private` prints
# $FAKE_GH_PRIVATE (default false). Every call is appended to $FAKE_LOG.
FAKE_GH = """#!/usr/bin/env bash
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_GH_HANG:-}" ]; then exec sleep 30; fi
if [ "$1" = api ]; then
  [ -z "${FAKE_GH_API_FAIL:-}" ] || { echo "HTTP 502" >&2; exit 1; }
  [ "$*" = "api repos/example/repo --jq .private" ] && printf '%s\\n' "${FAKE_GH_PRIVATE:-false}"
  exit 0
fi
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


# Fake opencode: records argv plus what the agent can see (key, HOME, SSH
# agent, GitHub token, a secret planted in the real HOME, and whether the key
# shows in any process's argv), then edits a file. The runner empties the
# agent's environment, so FAKE_LOG does not reach it; it records to TMPDIR,
# which the runner points at the agent's scratch home.
FAKE_OPENCODE = """#!/usr/bin/env bash
args="$*"; args="${args//$'\\n'/ }"
keyvar=none; key=none
for v in OPENROUTER_API_KEY OPENCODE_API_KEY ANTHROPIC_API_KEY ZHIPU_API_KEY CLOUDFLARE_API_TOKEN; do
  if [ -n "${!v:-}" ]; then keyvar="$v"; key="${!v}"; fi
done
real_home="$(cd ../../.. && pwd)"
if cat "$real_home/.ssh/id_planted" > /dev/null 2>&1; then secret=readable; else secret=denied; fi
if /usr/bin/nc -w 1 -U "$real_home/agent.sock" < /dev/null > /dev/null 2>&1; then sock=reachable; else sock=denied; fi
if /usr/bin/osascript -e 'return 1' > /dev/null 2>&1; then osa=ran; else osa=denied; fi
small="$(printf '%s' "${OPENCODE_CONFIG_CONTENT:-}" | sed -nE 's/.*"small_model": *"([^"]+)".*/\\1/p')"
port="$(printf '%s' "$args" | sed -nE 's/.*PROBE_PORT=([0-9]+).*/\\1/p')"
if [ -n "$port" ] && /usr/bin/nc -z -w 1 127.0.0.1 "$port" > /dev/null 2>&1; then lo=reachable; else lo=denied; fi
printf 'account=%s\\n' "${CLOUDFLARE_ACCOUNT_ID:-none}" >> "$TMPDIR/agent.log"
procs="$(ps -axo args= 2>/dev/null)"
case "$procs" in *"$key"*) argv=key-visible ;; *) argv=clean ;; esac
printf 'opencode %s | keyvar=%s key=%s home=%s ssh=%s ghtoken=%s fakelog=%s secret=%s argv=%s sock=%s osa=%s lo=%s small=%s\\n' "$args" \\
  "$keyvar" "$key" "$HOME" "${SSH_AUTH_SOCK:-none}" "${GH_TOKEN:-none}" "${FAKE_LOG:-none}" \\
  "$secret" "$argv" "$sock" "$osa" "$lo" "${small:-none}" >> "$TMPDIR/agent.log"
if printf '#!/bin/sh\\ntouch "%s/hooked"\\n' "$real_home" > .git/hooks/pre-commit 2>/dev/null \\
   && chmod +x .git/hooks/pre-commit 2>/dev/null; then hook=planted; else hook=denied; fi
printf 'hook=%s\\n' "$hook" >> "$TMPDIR/agent.log"
# Real CLIs fstat their stdout and stderr (Bun does on start).
if /usr/bin/perl -e 'exit((stat(STDOUT) && stat(STDERR)) ? 0 : 1)'; then
  fds=ok; else fds=denied; fi
printf 'fds=%s\\n' "$fds" >> "$TMPDIR/agent.log"
# Relative mkdir: BSD mkdir -p on an absolute path calls mkdir on ancestors
# outside the agent's home, which the sandbox refuses.
(cd "$HOME" && mkdir -p .local/share/opencode/storage/session)
echo '{"transcript": "the whole conversation"}' > "$HOME/.local/share/opencode/storage/session/s1.json"
# opencode 1.18 keeps sessions in SQLite.
echo "sqlite session store" > "$HOME/.local/share/opencode/opencode.db"
echo "sqlite wal" > "$HOME/.local/share/opencode/opencode.db-wal"
ln -s "$real_home/.ssh/id_planted" "$HOME/.local/share/opencode/storage/session/leak.json" 2>/dev/null
# A link for a whole directory: .claude -> a directory outside the home.
ln -s "$real_home/loom/run/elsewhere" "$HOME/.claude" 2>/dev/null
# A name with a newline and "..": a line-based reader would see "../outside.json".
(cd "$HOME/.local/share/opencode" && mkdir -p "x"$'\\n'".." && echo decoy > "x"$'\\n'"../outside.json")
echo fixed > fix.txt
"""

# Fake aws: `ssm get-parameter --name N` prints $FAKE_SECRET_<N with / as _>
# or fails like a missing parameter.
FAKE_AWS = """#!/usr/bin/env bash
printf 'aws %s\\n' "$*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do [ "$1" = --name ] && name="$2"; shift; done
var="FAKE_SECRET_$(printf '%s' "$name" | tr '/-' '__')"
val="${!var:-}"
[ -n "$val" ] || { echo "ParameterNotFound" >&2; exit 254; }
printf '%s\\n' "$val"
"""


# Fake "become the lane user" command: runs its arguments as-is and records
# that it was used. Production uses sudo to the dedicated macOS user.
FAKE_RUNAS = """#!/usr/bin/env bash
printf 'runas %s\\n' "$1" >> "$FAKE_LOG"
exec "$@"
"""

# Fake security: records unlock-keychain calls and whether the password
# matched $FAKE_KC_PW, never the password itself.
FAKE_SECURITY = """#!/usr/bin/env bash
# Interactive mode reads one command per line from stdin, so a password
# never appears in argv; record whether argv carried one.
if [ "$1" = -i ]; then IFS= read -r line; set -- $line; via=stdin; else via=argv; fi
if [ "$1" = unlock-keychain ]; then
  if [ "$3" = "$FAKE_KC_PW" ]; then ok=pw-ok; else ok=pw-bad; fi
  printf 'security unlock-keychain %s %s via=%s\\n' "$4" "$ok" "$via" >> "$FAKE_LOG"
  [ "$ok" = pw-ok ] || exit 51
fi
"""

# Fake agy: records its flags and working dir, then edits a file.
FAKE_AGY = """#!/usr/bin/env bash
args="$*"; args="${args//$'\\n'/ }"
G="$HOME/.gemini"
# What a previous run left in agy's memory, and whether this run can plant
# config that would steer the next one.
if [ -e "$G/antigravity-cli/brain/planted.md" ]; then mem=leftover; else mem=clean; fi
if echo '{"mcpServers":{}}' > "$G/config/mcp_config.json" 2>/dev/null; then cfg=writable; else cfg=locked; fi
if echo x >> "$HOME/.zshrc" 2>/dev/null; then rc=writable; else rc=locked; fi
printf 'agy %s | cwd=%s home=%s mem=%s cfg=%s rc=%s\\n' "$args" "$PWD" "$HOME" "$mem" "$cfg" "$rc" >> "$TMPDIR/agy.log"
mkdir -p "$G/antigravity-cli/brain" && echo note > "$G/antigravity-cli/brain/this-run.md"
echo fixed > fix.txt
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
        self.write_tool(self.bin / "opencode", FAKE_OPENCODE)
        self.write_tool(self.bin / "aws", FAKE_AWS)

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
        extra = []
        for agent in sorted((self.loom / "run").glob("*-home/tmp/agent.log")):
            extra += agent.read_text().splitlines()
        return self.log.read_text().splitlines() + extra

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

    def api_lane(self, lane, **env):
        planted = self.home / ".ssh" / "id_planted"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("private key\n")
        # A listening Unix socket in the real HOME stands in for an SSH agent.
        agent_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        agent_sock.bind(str(self.home / "agent.sock"))
        agent_sock.listen(1)
        self.addCleanup(agent_sock.close)
        inbox = self.loom / "briefs" / lane
        inbox.mkdir(parents=True, exist_ok=True)
        # A local TCP service (the transcript viewer, say) the agent must not reach.
        self.local_service = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.local_service.bind(("127.0.0.1", 0))
        self.local_service.listen(1)
        self.addCleanup(self.local_service.close)
        port = self.local_service.getsockname()[1]
        (inbox / "T042-fix-the-thing.md").write_text(
            BRIEF.replace("Lane: codex", f"Lane: {lane}") + f"\nPROBE_PORT={port}\n")
        result = self.run_lane(lane, SSH_AUTH_SOCK="/nonexistent/agent.sock", GH_TOKEN="fake-token-not-real", **env)
        return inbox, result

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_api_lane_agent_sees_only_its_key_and_a_scratch_home(self):
        inbox, result = self.api_lane(
            "openrouter",
            LOOM_OPENROUTER_KEY_SSM="/k/or",
            FAKE_SECRET__k_or="fake-or-key",
            LOOM_OPENROUTER_MODEL="openrouter/vendor/model:free",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        agent = [c for c in self.calls() if c.startswith("opencode ")]
        self.assertEqual(len(agent), 1, self.calls())
        self.assertIn("-m openrouter/vendor/model:free", agent[0])
        self.assertIn("keyvar=OPENROUTER_API_KEY key=fake-or-key", agent[0])
        self.assertIn("ssh=none", agent[0])
        self.assertIn("ghtoken=none", agent[0])
        self.assertIn("fakelog=none", agent[0])
        self.assertIn("secret=denied", agent[0])
        self.assertIn("argv=clean", agent[0])
        self.assertIn("sock=denied", agent[0])
        self.assertIn("osa=denied", agent[0])
        self.assertIn("lo=denied", agent[0])
        # A hook planted in the tree's .git would run when the runner commits.
        self.assertIn("hook=denied", self.calls())
        self.assertIn("fds=ok", self.calls())
        self.assertFalse((self.home / "hooked").exists())
        # opencode's helper (title) agent must not fall back to a paid model.
        self.assertIn("small=openrouter/vendor/model:free", agent[0])
        self.assertNotIn(f"home={self.home} ", agent[0])
        self.assertIn("lane openrouter, model openrouter/vendor/model:free", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_links_left_in_the_scratch_home_cannot_redirect_runner_writes(self):
        # A previous agent could leave its config path as a link into the
        # real HOME; the runner, which is not sandboxed, must not follow it.
        victim = self.home / ".zshrc"
        victim.write_text("original\n")
        cfg = self.loom / "run" / "openrouter-home" / ".config" / "opencode" / "opencode.json"
        cfg.parent.mkdir(parents=True)
        cfg.symlink_to(victim)
        self.api_lane(
            "openrouter",
            LOOM_OPENROUTER_KEY_SSM="/k/or",
            FAKE_SECRET__k_or="fake-or-key",
            LOOM_OPENROUTER_MODEL="openrouter/vendor/model:free",
        )
        self.assertEqual(victim.read_text(), "original\n")
        self.assertTrue(any("small=openrouter/vendor/model:free" in c for c in self.calls()))

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_api_lane_transcripts_are_archived_without_following_links(self):
        # The scratch home is wiped before the next run; the transcript must
        # survive it, and a link the agent left must not pull in a secret.
        # A file just outside the agent's home stands in for another lane's.
        outside = self.loom / "run" / "outside.json"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("another lane's file\n")
        projects = self.loom / "run" / "elsewhere" / "projects"
        projects.mkdir(parents=True)
        (projects / "other.json").write_text("another lane's file\n")
        self.api_lane(
            "openrouter",
            LOOM_OPENROUTER_KEY_SSM="/k/or",
            FAKE_SECRET__k_or="fake-or-key",
            LOOM_OPENROUTER_MODEL="openrouter/vendor/model:free",
        )
        archive = self.loom / "logs" / "openrouter" / "T042-fix-the-thing"
        files = {p.name: p for p in archive.rglob("*") if p.is_file() and not p.is_symlink()}
        self.assertIn("s1.json", files)
        self.assertIn("the whole conversation", files["s1.json"].read_text())
        self.assertIn("opencode.db", files)
        self.assertIn("opencode.db-wal", files)
        for p in (self.loom / "logs").rglob("*"):
            if p.is_file() and not p.is_symlink():
                self.assertNotIn("private key", p.read_text())
                self.assertNotIn("another lane's file", p.read_text())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_workersai_lane_gets_its_token_and_account_id(self):
        inbox, result = self.api_lane(
            "workersai",
            LOOM_WORKERSAI_KEY_SSM="/k/cf",
            FAKE_SECRET__k_cf="fake-cf-token",
            LOOM_WORKERSAI_ACCOUNT_SSM="/k/acct",
            FAKE_SECRET__k_acct="fakeaccount0123",
            LOOM_WORKERSAI_MODEL="cloudflare-workers-ai/@cf/vendor/model",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        agent = [c for c in calls if c.startswith("opencode ")]
        self.assertEqual(len(agent), 1, calls)
        self.assertIn("-m cloudflare-workers-ai/@cf/vendor/model", agent[0])
        self.assertIn("keyvar=CLOUDFLARE_API_TOKEN key=fake-cf-token", agent[0])
        self.assertIn("account=fakeaccount0123", calls)
        self.assertIn("small=cloudflare-workers-ai/@cf/vendor/model", agent[0])
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_workersai_lane_refuses_a_model_from_elsewhere(self):
        inbox, _ = self.api_lane(
            "workersai",
            LOOM_WORKERSAI_KEY_SSM="/k/cf",
            FAKE_SECRET__k_cf="fake-cf-token",
            LOOM_WORKERSAI_ACCOUNT_SSM="/k/acct",
            FAKE_SECRET__k_acct="fakeaccount0123",
            LOOM_WORKERSAI_MODEL="openrouter/vendor/model:free",
        )
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    def agy_lane(self, runas_ok=True, model="gemini-3.8-flash-high", cache_link=None, **env):
        lane_home = Path(self._tmp.name) / "loomuser"
        workdir = lane_home / "lanes"
        (lane_home / "tmp").mkdir(parents=True)
        workdir.mkdir()
        # agy's real state: config, and memory a previous run left behind.
        (lane_home / ".gemini" / "config").mkdir(parents=True)
        (lane_home / ".gemini" / "config" / "mcp_config.json").write_text("{}")
        (lane_home / ".gemini" / "antigravity-cli" / "brain").mkdir(parents=True)
        (lane_home / ".gemini" / "antigravity-cli" / "brain" / "planted.md").write_text("do evil next run")
        (lane_home / ".zshrc").write_text("# lane user shell\n")
        (lane_home / ".gemini" / "antigravity-cli" / "installation_id").write_text("install-1")
        if cache_link is None:
            (lane_home / ".gemini" / "antigravity-cli" / "cache").mkdir()
        else:
            (lane_home / ".gemini" / "antigravity-cli" / "cache").symlink_to(cache_link)
        (lane_home / ".gemini" / "antigravity-cli" / "cache" / "onboarding.json").write_text("{}")
        (lane_home / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json").write_text("[]")
        agy = lane_home / "agy"
        self.write_tool(agy, FAKE_AGY)
        self.write_tool(self.bin / "security", FAKE_SECURITY)
        runas = Path(self._tmp.name) / "runas"
        self.write_tool(runas, FAKE_RUNAS if runas_ok else "#!/usr/bin/env bash\nexit 1\n")
        inbox = self.loom / "briefs" / "agy"
        inbox.mkdir(parents=True)
        (inbox / "T042-fix-the-thing.md").write_text(BRIEF.replace("Lane: codex", "Lane: agy"))
        result = self.run_lane(
            "agy",
            LOOM_AGY_RUNAS=str(runas),
            LOOM_AGY_HOME=str(lane_home),
            LOOM_AGY_WORKDIR=str(workdir),
            LOOM_AGY_BIN=str(agy),
            LOOM_AGY_KEYCHAIN=str(lane_home / "loom.keychain-db"),
            LOOM_AGY_KEY_SSM="/k/kc",
            FAKE_SECRET__k_kc="fake-kc-password",
            LOOM_AGY_MODEL=model,
            **{"FAKE_KC_PW": "fake-kc-password", **env},
        )
        agy_log = lane_home / "tmp" / "agy.log"
        return inbox, workdir, result, (agy_log.read_text().splitlines() if agy_log.exists() else [])

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_forget_never_deletes_through_a_link_an_earlier_run_left(self):
        # A previous agent swapped agy's cache directory for a link to the
        # keychain folder; clearing the cache must not empty the keychains.
        victim = Path(self._tmp.name) / "keychains"
        victim.mkdir()
        (victim / "loom.keychain-db").write_text("keychain")
        inbox, workdir, result, agy = self.agy_lane(cache_link=victim)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((victim / "loom.keychain-db").read_text(), "keychain")
        self.assertFalse((workdir.parent / ".gemini" / "antigravity-cli" / "cache").is_symlink())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_lane_unlocks_its_keychain_then_runs_as_the_lane_user(self):
        inbox, workdir, result, agy = self.agy_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        unlocks = [i for i, c in enumerate(calls) if c.startswith("security unlock-keychain")]
        self.assertEqual(len(unlocks), 1, calls)
        self.assertIn("loom.keychain-db pw-ok via=stdin", calls[unlocks[0]])
        self.assertEqual(len(agy), 1, (calls, result.stderr))
        self.assertIn("-p # Brief: T042-fix-the-thing", agy[0])
        self.assertIn("--mode accept-edits", agy[0])
        self.assertIn("--model gemini-3.8-flash-high", agy[0])
        self.assertIn(f"cwd={workdir.resolve()}/T042-fix-the-thing", agy[0])
        # Nothing from a previous run reaches this one, and this run cannot
        # plant config or shell startup files for the next.
        self.assertIn("mem=clean", agy[0])
        self.assertIn("cfg=locked", agy[0])
        self.assertIn("rc=locked", agy[0])
        # Sign-in and install state survive the wipe; conversation lists do not.
        state = workdir.parent / ".gemini" / "antigravity-cli"
        self.assertEqual((state / "installation_id").read_text(), "install-1")
        self.assertTrue((state / "cache" / "onboarding.json").exists())
        self.assertFalse((state / "cache" / "last_conversations.json").exists())
        self.assertIn("lane agy, model gemini-3.8-flash-high", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_agy_lane_is_unavailable_when_its_keychain_will_not_unlock(self):
        inbox, _, result, agy = self.agy_lane(FAKE_KC_PW="some-other-password")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(agy, [])
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "agy.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("unlock", beat["reason"])

    def test_agy_lane_is_unavailable_when_it_cannot_become_the_lane_user(self):
        inbox, _, result, agy = self.agy_lane(runas_ok=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(agy, [])
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "agy.json").read_text())
        self.assertFalse(beat["available"])

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_lane_refuses_a_model_outside_its_catalog(self):
        inbox, _, _, agy = self.agy_lane(model="openrouter/vendor/model:free")
        self.assertEqual(agy, [])
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_lane_takes_public_repos_only(self):
        inbox, _, _, agy = self.agy_lane(FAKE_GH_PRIVATE="true")
        self.assertEqual(agy, [])
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_openrouter_lane_refuses_a_paid_model(self):
        inbox, _ = self.api_lane(
            "openrouter",
            LOOM_OPENROUTER_KEY_SSM="/k/or",
            FAKE_SECRET__k_or="fake-or-key",
            LOOM_OPENROUTER_MODEL="openrouter/vendor/model",
        )
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_zen_lane_runs_a_free_model_sandboxed_without_a_key(self):
        # OpenCode Zen serves its free models with no stored credential.
        inbox, result = self.api_lane("zen", LOOM_ZEN_MODEL="opencode/space-bunny-free")
        self.assertEqual(result.returncode, 0, result.stderr)
        agent = [c for c in self.calls() if c.startswith("opencode ")]
        self.assertEqual(len(agent), 1, self.calls())
        self.assertIn("-m opencode/space-bunny-free", agent[0])
        self.assertIn("keyvar=none", agent[0])
        self.assertIn("secret=denied", agent[0])
        self.assertIn("small=opencode/space-bunny-free", agent[0])
        self.assertNotIn(f"home={self.home} ", agent[0])
        self.assertIn("lane zen, model opencode/space-bunny-free", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_zen_lane_refuses_a_paid_model(self):
        inbox, _ = self.api_lane("zen", LOOM_ZEN_MODEL="opencode/claude-opus-5-5")
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_zen_lane_refuses_a_private_repo_before_cloning(self):
        # Zen's free providers may train on prompts: public repos only.
        inbox, result = self.api_lane(
            "zen", LOOM_ZEN_MODEL="opencode/space-bunny-free", FAKE_GH_PRIVATE="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gh api repos/example/repo --jq .private", self.calls())
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertFalse((self.loom / "worktrees" / "T042-fix-the-thing").exists())
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        log = (self.loom / "logs" / "zen" / "T042-fix-the-thing.log").read_text()
        self.assertIn("private", log)

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_a_failed_privacy_lookup_leaves_the_brief_for_the_next_pass(self):
        inbox, result = self.api_lane(
            "zen", LOOM_ZEN_MODEL="opencode/space-bunny-free", FAKE_GH_API_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_opencode_go_muse_spark_contributor_refuses_a_private_repo(self):
        # Muse Spark Contributor trains Meta's models; the rest of Go keeps 0-30 days.
        inbox, _ = self.api_lane(
            "opencode-go", LOOM_OPENCODE_GO_KEY_SSM="/k/go", FAKE_SECRET__k_go="fake-go-key",
            LOOM_OPENCODE_GO_MODEL="opencode-go/muse-spark-1.3-contributor", FAKE_GH_PRIVATE="true")
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_opencode_go_other_models_still_take_a_private_repo(self):
        inbox, result = self.api_lane(
            "opencode-go", LOOM_OPENCODE_GO_KEY_SSM="/k/go", FAKE_SECRET__k_go="fake-go-key",
            LOOM_OPENCODE_GO_MODEL="opencode-go/kimi-k2.7-code", FAKE_GH_PRIVATE="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith("opencode ") for c in self.calls()), self.calls())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_zai_lane_hands_its_key_to_a_free_flash_model(self):
        inbox, result = self.api_lane(
            "zai", LOOM_ZAI_KEY_SSM="/k/zai", FAKE_SECRET__k_zai="fake-zai-key",
            LOOM_ZAI_MODEL="zai/glm-4.7-flash")
        self.assertEqual(result.returncode, 0, result.stderr)
        agent = [c for c in self.calls() if c.startswith("opencode ")]
        self.assertEqual(len(agent), 1, self.calls())
        self.assertIn("-m zai/glm-4.7-flash", agent[0])
        self.assertIn("keyvar=ZHIPU_API_KEY key=fake-zai-key", agent[0])
        self.assertIn("argv=clean", agent[0])
        self.assertIn("secret=denied", agent[0])
        self.assertIn("small=zai/glm-4.7-flash", agent[0])
        self.assertIn("lane zai, model zai/glm-4.7-flash", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_zai_lane_refuses_a_paid_model(self):
        # Z.ai's GLM-5.3 Flash is billed per token; only 4.7 and 4.5 Flash are free.
        inbox, _ = self.api_lane(
            "zai", LOOM_ZAI_KEY_SSM="/k/zai", FAKE_SECRET__k_zai="fake-zai-key",
            LOOM_ZAI_MODEL="zai/glm-5.3-flash")
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_zai_lane_refuses_a_private_repo(self):
        inbox, _ = self.api_lane(
            "zai", LOOM_ZAI_KEY_SSM="/k/zai", FAKE_SECRET__k_zai="fake-zai-key",
            LOOM_ZAI_MODEL="zai/glm-4.7-flash", FAKE_GH_PRIVATE="true")
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_zai_lane_without_a_key_reports_unavailable(self):
        inbox, result = self.api_lane("zai", LOOM_ZAI_MODEL="zai/glm-4.7-flash")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "zai.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("LOOM_ZAI_KEY_SSM", beat["reason"])

    def test_lane_without_its_credential_reports_unavailable_and_keeps_its_briefs(self):
        inbox, result = self.api_lane(
            "anthropic", LOOM_ANTHROPIC_KEY_SSM="/k/missing", LOOM_ANTHROPIC_MODEL="claude-test"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith(("opencode ", "claude ")) for c in self.calls()))
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "anthropic.json").read_text())
        self.assertFalse(beat["available"])

    def test_a_malformed_timeout_reports_unavailable_instead_of_running(self):
        self.drop_brief()
        result = self.run_lane(LOOM_AGENT_TIMEOUT="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertFalse(beat["available"])

    def test_a_ticket_name_with_unsafe_characters_is_parked(self):
        self.drop_brief(name='T042-bad"name.md')
        self.run_lane()
        self.assertFalse(any(c.startswith("codex ") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / 'T042-bad"name.md').exists())

    def test_a_pass_writes_a_fresh_available_heartbeat(self):
        before = int(time.time())
        self.run_lane()
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertTrue(beat["available"])
        self.assertEqual(beat["lane"], "codex")
        self.assertGreaterEqual(beat["at"], before)

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
