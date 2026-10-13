"""lanes/run_lane.sh run as a process: a temp loom home, a local bare repo as
the remote, and fake agent and gh commands on PATH that record their calls."""

import json
import os
import signal
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import pr_repair, text, tickets  # noqa: E402

RUNNER = Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh"
INSTALL_LOCK = Path(__file__).resolve().parent.parent / "lanes" / "mac" / "lock.sh"

BRIEF = """# Brief: T042-fix-the-thing

Repo: example/repo
Branch: loom/T042-fix-the-thing
Lane: codex
Difficulty: medium

## Ticket

# T042: Fix the thing

## Why
The thing is broken for every user.

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
# $FAKE_GH_PRIVATE (default false). `pr view` prints "$FAKE_GH_PR_STATE
# $FAKE_GH_PR_HEAD" (default OPEN on loom/42-fix-the-thing), or JSON with
# title/body when asked for it. `pr edit --title T --body-file F` records the
# new title in $FAKE_PR_TITLE and copies the body file to $FAKE_PR_BODY.
# Every call is appended to $FAKE_LOG.
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
if [ "$1 $2" = "pr view" ]; then
  case " $* " in
    *" --json title,body "*)
      title=$(cat "${FAKE_GH_PR_TITLE:-/dev/null}" 2>/dev/null || true)
      body=$(cat "${FAKE_GH_PR_BODY:-/dev/null}" 2>/dev/null || true)
      printf '{"title":%s,"body":%s}\n' "$(printf '%s' "$title" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")')" "$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")')"
      ;;
    *)
      printf '%s %s\n' "${FAKE_GH_PR_STATE:-OPEN}" "${FAKE_GH_PR_HEAD:-loom/42-fix-the-thing}"
      ;;
  esac
fi
if [ "$1 $2" = "pr edit" ]; then
  [ -z "${FAKE_GH_EDIT_FAIL:-}" ] || { echo "gh pr edit failed" >&2; exit 1; }
  while [ $# -gt 0 ]; do
    case "$1" in
      --title) printf '%s' "$2" > "${FAKE_PR_TITLE:-/dev/null}"; shift 2 ;;
      --body-file) cp "$2" "${FAKE_PR_BODY:-/dev/null}"; shift 2 ;;
      --body) printf '%s' "$2" > "${FAKE_PR_BODY:-/dev/null}"; shift 2 ;;
      *) shift ;;
    esac
  done
  exit 0
fi
if [ "$1 $2" = "pr create" ]; then
  while [ $# -gt 0 ]; do [ "$1" = "--body" ] && printf '%s' "$2" > "$FAKE_BODY"; shift; done
  echo "https://github.com/example/repo/pull/7"
fi
"""

# Fake agent: records the call, then commits a file ($FAKE_AGENT=commit),
# only edits it the way a sandboxed codex must ($FAKE_AGENT=edit), makes no
# file change at all ($FAKE_AGENT=noop), or exits non-zero ($FAKE_AGENT=fail).
FAKE_AGENT = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$FAKE_LOG"
cat > /dev/null
if [ "${FAKE_AGENT:-commit}" = fail ]; then exit 3; fi
if [ "${FAKE_AGENT:-commit}" = noop ]; then exit 0; fi
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
for v in OPENROUTER_API_KEY OPENCODE_API_KEY ANTHROPIC_API_KEY ZHIPU_API_KEY CLOUDFLARE_API_KEY; do
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
printf 'config=%s\\n' "${OPENCODE_CONFIG_CONTENT:-none}" >> "$TMPDIR/agent.log"
# A brief carrying AGENT_FAIL=1 ends the run with an error, as a dropped
# connection would.
case "$args" in *AGENT_FAIL=1*) exit 3 ;; esac
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


# Fake curl: the spark lane's probe of its endpoint. $FAKE_CURL lists one
# answer per call (an HTTP code, "refused" for a refused connection, curl
# exit 7, or "timeout", curl exit 28); the last answer repeats. Every call is appended to $FAKE_LOG.
FAKE_CURL = """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >> "$FAKE_LOG"
n="$(grep -c '^curl ' "$FAKE_LOG")"
set -- ${FAKE_CURL:-200}
[ "$n" -le "$#" ] || n="$#"
answer="${!n}"
if [ "$answer" = refused ]; then printf '000'; exit 7; fi
if [ "$answer" = timeout ]; then printf '000'; exit 28; fi
printf '%s' "$answer"
"""


# Fake "become the lane user" command: runs its arguments as-is and records
# that it was used. Production uses sudo to the dedicated macOS user.
FAKE_RUNAS = """#!/usr/bin/env bash
printf 'runas %s\\n' "$1" >> "$FAKE_LOG"
exec "$@"
"""

# Fake security: records unlock-keychain calls and whether the password
# matched $FAKE_KC_PW, never the password itself. It splits its stdin line on
# whitespace as security -i does; the runner refuses passwords containing
# whitespace, quotes or backslashes, where the two could disagree.
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
S="$G/config/skills/impeccable/SKILL.md"
if [ -f "$S" ]; then skill="$(head -1 "$S")"; else skill=none; fi
if echo x >> "$S" 2>/dev/null || rm -f "$S" 2>/dev/null; then skillw=writable; else skillw=locked; fi
if echo '{"mcpServers":{}}' > "$G/config/mcp_config.json" 2>/dev/null; then cfg=writable; else cfg=locked; fi
if echo x >> "$HOME/.zshrc" 2>/dev/null; then rc=writable; else rc=locked; fi
printf 'agy %s | cwd=%s home=%s mem=%s cfg=%s rc=%s skill=%s skillw=%s\\n' "$args" "$PWD" "$HOME" "$mem" "$cfg" "$rc" "$skill" "$skillw" >> "$TMPDIR/agy.log"
mkdir -p "$G/antigravity-cli/brain" && echo note > "$G/antigravity-cli/brain/this-run.md"
echo fixed > fix.txt
"""


# Fake claude for the anthropic lane: for each folder a CLI reads skills
# from, records the first line of each skill it sees, and whether the agent
# can change the snapshot through that folder or at its real path.
FAKE_SKILL_CLAUDE = """#!/usr/bin/env bash
for f in .claude/skills .agents/skills .config/opencode/skills; do
  d="$HOME/$f"
  seen="$(for s in "$d"/*/SKILL.md; do [ -f "$s" ] && printf '%s,' "$(head -1 "$s")"; done)"
  if echo x >> "$d/impeccable/SKILL.md" 2>/dev/null || touch "$d/impeccable/new" 2>/dev/null \\
     || rm -f "$d/impeccable/SKILL.md" 2>/dev/null; then w=writable; else w=locked; fi
  real="$(cd "$d" 2>/dev/null && pwd -P)"
  if [ -n "$real" ] && { touch "$real/planted" 2>/dev/null || chmod u+w "$real/impeccable" 2>/dev/null; }; then
    rw=writable; else rw=locked; fi
  printf 'skills %s seen=%s write=%s realwrite=%s\\n' "$f" "${seen:-none}" "$w" "$rw" >> "$TMPDIR/agent.log"
done
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
        self.write_tool(self.bin / "curl", FAKE_CURL)

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
        return subprocess.run(["bash", str(RUNNER), lane], env=self.lane_env(**env),
                              capture_output=True, text=True, timeout=60)

    def lane_env(self, **env):
        return {
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

    def calls(self):
        extra = []
        for agent in sorted((self.loom / "run").glob("*-home/tmp/agent.log")):
            extra += agent.read_text().splitlines()
        return self.log.read_text().splitlines() + extra

    def test_fix_items_ignores_dispositions_before_a_finding(self):
        brief = Path(self._tmp.name) / "fix-items.md"
        brief.write_text("## Fix\nFixed\n### Thread F1 (comment 1) at test.py:10\n"
                         "Fixed\n### Thread F2 (comment 2) at test.py:20\n"
                         "Fixing: example/repo#12\n")
        script = 'source <(sed -n "/^fix_items() {/,/^}/p" "$1"); fix_items "$2"'
        result = subprocess.run(["bash", "-c", script, "bash", str(RUNNER), str(brief)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(),
                         "Thread F1 (comment 1) at test.py:10 [fixed]; "
                         "Thread F2 (comment 2) at test.py:20 [example/repo#12]")

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
        self.assertIn("## Why\nThe thing is broken for every user.", body)
        self.assertIn("## Acceptance criteria\n- [ ] The thing works\n- [ ] A test covers the thing\n- [ ] Nothing else changes", body)
        self.assertIn("\nSize: S\n", body)
        self.assertIn("## Out of scope\n- Other things", body)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_a_body_the_runner_wrote_is_current_to_the_pr_repair_helper(self):
        # The body comes from run_lane.sh::pr_body, not from pr_repair's own
        # renderer, so a drift between the two shows up as a spurious repair.
        self.drop_brief()
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        done = self.inbox / "done" / "T042-fix-the-thing.md"
        title = pr_repair._brief_title(done.read_text())
        repaired = pr_repair.reconcile(str(done), title or "", self.body.read_text(),
                                       "codex", "gpt-test-default", "T042-fix-the-thing")
        self.assertEqual(repaired["reason"], "")
        self.assertFalse(repaired["changed"], repaired)
        self.assertEqual(repaired["old_hash"], repaired["new_hash"])

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

    def test_pr_body_ends_with_refs_for_a_station_brief(self):
        self.drop_brief(text=BRIEF.replace("Lane: codex\n", "Lane: codex\nIssue: example/repo#42\n"))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.body.read_text().rstrip("\n").endswith(
            "lane codex, model gpt-test-default.\nRefs example/repo#42"))

    def test_pr_body_has_no_refs_without_an_issue_line(self):
        self.drop_brief()
        self.run_lane()
        self.assertNotIn("Refs ", self.body.read_text())

    def test_a_brief_rendered_by_the_station_is_accepted_and_refs_its_issue(self):
        issue = {"number": 42, "title": "Fix the thing",
                 "body": "Difficulty: medium\n\n## Why\nBroken.\n\n## What\nFix it.\n"}
        name = tickets.ticket_name("example/repo", 42, 1)
        self.drop_brief(name + ".md", tickets.render_brief("example/repo", issue, "codex", 1))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())
        heads = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "loom/42-fix-the-thing"],
            capture_output=True, text=True, check=True).stdout
        self.assertIn("refs/heads/loom/42-fix-the-thing", heads)
        self.assertIn("\nRefs example/repo#42", self.body.read_text())

    FIX_BRANCH = "loom/42-fix-the-thing"

    def fix_brief(self):
        """A station fix brief for PR 7, whose branch is already on the remote
        with the lane's first commit."""
        work = Path(self._tmp.name) / "pr-work"
        git("clone", "-q", str(self.remote), str(work))
        git("checkout", "-q", "-b", self.FIX_BRANCH, cwd=work)
        (work / "first.txt").write_text("first try\n")
        git("add", "first.txt", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "First try", cwd=work)
        git("push", "-q", "origin", self.FIX_BRANCH, cwd=work)
        issue = {"number": 42, "title": "Fix the thing",
                 "body": "Difficulty: medium\n\n## Why\nBroken.\n\n## What\nFix it.\n"}
        fix = tickets.parse_fix("example/repo", (
            "<!-- loom:fix v1 -->\nPull request: example/repo#7, branch %s\n\n"
            "### Check: lint\n\n    E501 line too long\n\n"
            "### Thread PRRT_1 (comment 11) at a.py:3\n\n    reviewer: rename this\n"
            % self.FIX_BRANCH))
        name = tickets.ticket_name("example/repo", 42, 2)
        self.drop_brief(name + ".md", tickets.render_brief("example/repo", issue, "codex", 2, fix))
        return name, self.remote_head(self.FIX_BRANCH)

    def remote_head(self, branch):
        return subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", branch],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_a_fix_brief_pushes_onto_the_pull_requests_branch_without_a_new_pr(self):
        name, before = self.fix_brief()
        # The branch already has a PR: an ordinary brief would be retired.
        result = self.run_lane(FAKE_GH_EXISTING="7")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any(c.startswith("codex ") for c in calls))
        self.assertFalse(any(c.startswith("gh pr create") for c in calls))
        after = self.remote_head(self.FIX_BRANCH)
        parent = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", after + "^"],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(parent, before)  # one fast-forward commit on top
        files = subprocess.run(["git", "--git-dir", str(self.remote), "ls-tree", "--name-only",
                                after], capture_output=True, text=True).stdout.split()
        self.assertIn("first.txt", files)
        self.assertIn("fix.txt", files)
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.remote)],
                               capture_output=True, text=True, check=True).stdout
        self.assertNotIn("-r2", heads)
        comment = [c for c in calls if c.startswith("gh pr comment")]
        self.assertEqual(comment, ["gh pr comment 7 -R example/repo --body Lane codex pushed "
                                   "fixes for: Check: lint; Thread PRRT_1 (comment 11) at a.py:3"])
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())

    def test_a_fix_brief_repairs_stale_pr_text_without_a_code_commit(self):
        # The branch already has one commit and an open PR. The ticket body
        # was corrected after the PR was opened, so the generated PR sections
        # are stale. The agent makes no code change: only the PR text is
        # repaired.
        work = Path(self._tmp.name) / "pr-work"
        git("clone", "-q", str(self.remote), str(work))
        git("checkout", "-q", "-b", self.FIX_BRANCH, cwd=work)
        (work / "first.txt").write_text("first try\n")
        git("add", "first.txt", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "First try", cwd=work)
        git("push", "-q", "origin", self.FIX_BRANCH, cwd=work)

        old_issue = {"number": 42, "title": "Fix the thing",
                     "body": ("Difficulty: medium\n\n## Why\nBroken.\n\n## What\nFix it.\n\n"
                              "## Acceptance criteria\n- [ ] The thing works\n\n"
                              "## Size\nS\n\n"
                              "## Out of scope\n- Nothing else\n")}
        corrected_issue = {"number": 42, "title": "Fix the thing",
                           "body": ("Difficulty: medium\n\n## Why\nBroken.\n\n## What\nFix it.\n\n"
                                    "## Acceptance criteria\n- [ ] The thing works\n"
                                    "- [ ] A test covers the thing\n\n"
                                    "## Size\nS\n\n"
                                    "## Out of scope\n- Nothing else\n")}
        fix = tickets.parse_fix("example/repo", (
            "<!-- loom:fix v1 -->\nPull request: example/repo#7, branch %s\n\n"
            "### Check: lint\n\n    E501 line too long\n" % self.FIX_BRANCH))
        name = tickets.ticket_name("example/repo", 42, 2)
        self.drop_brief(name + ".md",
                        tickets.render_brief("example/repo", corrected_issue, "codex", 2, fix))

        old_brief = tickets.render_brief("example/repo", old_issue, "codex", 2)
        old_body = "A reviewer note at the top.\n\n" + pr_repair._render_generated(
            pr_repair._normalise(old_brief), "codex", "gpt-test-default", name)

        pr_title_file = Path(self._tmp.name) / "pr_title"
        pr_body_file = Path(self._tmp.name) / "pr_body"
        new_title_file = Path(self._tmp.name) / "new_title"
        new_body_file = Path(self._tmp.name) / "new_body"
        pr_title_file.write_text("Fix the thing")
        pr_body_file.write_text(old_body)

        before = self.remote_head(self.FIX_BRANCH)
        result = self.run_lane(
            FAKE_AGENT="noop",
            FAKE_GH_EXISTING="7",
            FAKE_GH_PR_TITLE=str(pr_title_file),
            FAKE_GH_PR_BODY=str(pr_body_file),
            FAKE_PR_TITLE=str(new_title_file),
            FAKE_PR_BODY=str(new_body_file),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)  # no code commit
        calls = self.calls()
        log = (self.loom / "logs" / "codex" / (name + ".log")).read_text()
        self.assertFalse(any(c.startswith("gh pr create") for c in calls))
        self.assertIn("repaired PR text", log)
        new_body = new_body_file.read_text()
        self.assertIn("A reviewer note at the top.", new_body)
        self.assertIn("- [ ] A test covers the thing", new_body)
        self.assertIn("Loom ticket %s, lane codex, model gpt-test-default." % name, new_body)
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())

    def test_a_fix_brief_refuses_invalid_pr_text_with_a_reason(self):
        # The agent makes no code change and the corrected ticket text fails
        # validation; the repair is refused and the reason is logged.
        work = Path(self._tmp.name) / "pr-work"
        git("clone", "-q", str(self.remote), str(work))
        git("checkout", "-q", "-b", self.FIX_BRANCH, cwd=work)
        (work / "first.txt").write_text("first try\n")
        git("add", "first.txt", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "First try", cwd=work)
        git("push", "-q", "origin", self.FIX_BRANCH, cwd=work)

        bad_issue = {"number": 42, "title": "Fix the thing",
                     "body": ("Difficulty: medium\n\n## Why\nBroken, ask @" + text.REVIEW_BOT_SHORT_NAME + ".\n\n## What\nFix it.\n\n"
                              "## Acceptance criteria\n- [ ] The thing works\n\n"
                              "## Size\nS\n\n"
                              "## Out of scope\n- Nothing else\n")}
        fix = tickets.parse_fix("example/repo", (
            "<!-- loom:fix v1 -->\nPull request: example/repo#7, branch %s\n\n"
            "### Check: lint\n\n    E501 line too long\n" % self.FIX_BRANCH))
        name = tickets.ticket_name("example/repo", 42, 2)
        self.drop_brief(name + ".md",
                        tickets.render_brief("example/repo", bad_issue, "codex", 2, fix))

        old_issue = {"number": 42, "title": "Fix the thing",
                     "body": ("Difficulty: medium\n\n## Why\nBroken.\n\n## What\nFix it.\n\n"
                              "## Acceptance criteria\n- [ ] The thing works\n\n"
                              "## Size\nS\n\n"
                              "## Out of scope\n- Nothing else\n")}
        old_brief = tickets.render_brief("example/repo", old_issue, "codex", 2)
        old_body = pr_repair._render_generated(
            pr_repair._normalise(old_brief), "codex", "gpt-test-default", name)

        pr_title_file = Path(self._tmp.name) / "pr_title"
        pr_body_file = Path(self._tmp.name) / "pr_body"
        new_title_file = Path(self._tmp.name) / "new_title"
        new_body_file = Path(self._tmp.name) / "new_body"
        pr_title_file.write_text("Fix the thing")
        pr_body_file.write_text(old_body)

        result = self.run_lane(
            FAKE_AGENT="noop",
            FAKE_GH_EXISTING="7",
            FAKE_GH_PR_TITLE=str(pr_title_file),
            FAKE_GH_PR_BODY=str(pr_body_file),
            FAKE_PR_TITLE=str(new_title_file),
            FAKE_PR_BODY=str(new_body_file),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.loom / "logs" / "codex" / (name + ".log")).read_text()
        self.assertIn("proposed text refused", log)
        self.assertIn("review bot", log)
        self.assertIn("PR text repair refused", log)
        self.assertNotIn("PR text is current", log)
        self.assertFalse(new_body_file.exists())
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())

    def test_a_fix_brief_whose_pr_text_repair_helper_fails_is_parked(self):
        # The agent makes no code change and the gh pr edit call fails; the
        # brief is parked rather than treated as "text is current".
        work = Path(self._tmp.name) / "pr-work"
        git("clone", "-q", str(self.remote), str(work))
        git("checkout", "-q", "-b", self.FIX_BRANCH, cwd=work)
        (work / "first.txt").write_text("first try\n")
        git("add", "first.txt", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "First try", cwd=work)
        git("push", "-q", "origin", self.FIX_BRANCH, cwd=work)

        corrected_issue = {"number": 42, "title": "Fix the thing",
                           "body": ("Difficulty: medium\n\n## Why\nBroken.\n\n## What\nFix it.\n\n"
                                    "## Acceptance criteria\n- [ ] The thing works\n"
                                    "- [ ] A test covers the thing\n\n"
                                    "## Size\nS\n\n"
                                    "## Out of scope\n- Nothing else\n")}
        fix = tickets.parse_fix("example/repo", (
            "<!-- loom:fix v1 -->\nPull request: example/repo#7, branch %s\n\n"
            "### Check: lint\n\n    E501 line too long\n" % self.FIX_BRANCH))
        name = tickets.ticket_name("example/repo", 42, 2)
        self.drop_brief(name + ".md",
                        tickets.render_brief("example/repo", corrected_issue, "codex", 2, fix))

        old_issue = {"number": 42, "title": "Fix the thing",
                     "body": ("Difficulty: medium\n\n## Why\nBroken.\n\n## What\nFix it.\n\n"
                              "## Acceptance criteria\n- [ ] The thing works\n\n"
                              "## Size\nS\n\n"
                              "## Out of scope\n- Nothing else\n")}
        old_brief = tickets.render_brief("example/repo", old_issue, "codex", 2)
        old_body = pr_repair._render_generated(
            pr_repair._normalise(old_brief), "codex", "gpt-test-default", name)

        pr_title_file = Path(self._tmp.name) / "pr_title"
        pr_body_file = Path(self._tmp.name) / "pr_body"
        new_title_file = Path(self._tmp.name) / "new_title"
        new_body_file = Path(self._tmp.name) / "new_body"
        pr_title_file.write_text("Fix the thing")
        pr_body_file.write_text(old_body)

        before = self.remote_head(self.FIX_BRANCH)
        result = self.run_lane(
            FAKE_AGENT="noop",
            FAKE_GH_EXISTING="7",
            FAKE_GH_PR_TITLE=str(pr_title_file),
            FAKE_GH_PR_BODY=str(pr_body_file),
            FAKE_PR_TITLE=str(new_title_file),
            FAKE_PR_BODY=str(new_body_file),
            FAKE_GH_EDIT_FAIL="1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)  # no code commit
        log = (self.loom / "logs" / "codex" / (name + ".log")).read_text()
        self.assertIn("PR text repair failed", log)
        self.assertNotIn("PR text is current", log)
        self.assertFalse(new_body_file.exists())
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())

    def advance_main(self, name, text):
        """A commit on the remote's default branch after the PR branch left it."""
        work = Path(self._tmp.name) / "main-work"
        git("clone", "-q", str(self.remote), str(work))
        (work / name).write_text(text)
        git("add", "-A", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "Main moves", cwd=work)
        git("push", "-q", "origin", "main", cwd=work)

    def remote_file(self, ref, name):
        return subprocess.run(["git", "--git-dir", str(self.remote), "show", "%s:%s" % (ref, name)],
                              capture_output=True, text=True).stdout

    def parents(self, ref):
        return subprocess.run(["git", "--git-dir", str(self.remote), "rev-list", "--parents", "-n", "1", ref],
                              capture_output=True, text=True, check=True).stdout.split()[1:]

    # The agent sees the brief as its last argument; these record what it saw.
    AGENT_NOTHING = """#!/usr/bin/env bash
case "$*" in *"conflicts in: first.txt"*) echo "agent saw conflicts" >> "$FAKE_LOG" ;; esac
cat > /dev/null
"""

    def test_a_fix_brief_merges_a_moved_default_branch_even_with_no_agent_change(self):
        name, before = self.fix_brief()
        self.advance_main("main.txt", "main moved\n")
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING)
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.remote_head(self.FIX_BRANCH)
        self.assertEqual(self.remote_file(after, "main.txt"), "main moved\n")
        self.assertEqual(self.remote_file(after, "first.txt"), "first try\n")
        self.assertEqual(len(self.parents(after)), 2)
        self.assertIn(before, self.parents(after))
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())

    def test_a_conflict_goes_to_the_agent_and_the_runner_completes_the_merge(self):
        name, before = self.fix_brief()
        self.advance_main("first.txt", "main's version\n")
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING + "printf 'both kept\\n' > first.txt\n")
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agent saw conflicts", self.calls())
        after = self.remote_head(self.FIX_BRANCH)
        self.assertEqual(self.remote_file(after, "first.txt"), "both kept\n")
        self.assertEqual(len(self.parents(after)), 2)
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())

    def test_a_conflict_in_a_path_with_a_space_is_found_whole(self):
        name, before = self.fix_brief()
        work = Path(self._tmp.name) / "space-work"
        git("clone", "-q", "-b", self.FIX_BRANCH, str(self.remote), str(work))
        (work / "my notes.txt").write_text("branch side\n")
        git("add", "-A", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "notes", cwd=work)
        git("push", "-q", "origin", self.FIX_BRANCH, cwd=work)
        self.advance_main("my notes.txt", "main side\n")
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING)
        self.run_lane()
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        self.assertIn("conflict markers left in my notes.txt",
                      (self.loom / "logs" / "codex" / (name + ".log")).read_text())

    def test_a_merge_base_past_the_fetch_depth_is_found_by_unshallowing(self):
        name, before = self.fix_brief()
        work = Path(self._tmp.name) / "deep-work"
        git("clone", "-q", str(self.remote), str(work))
        for i in range(210):
            git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "commit", "-q", "--allow-empty", "-m", "main %d" % i, cwd=work)
        (work / "deep.txt").write_text("deep\n")
        git("add", "-A", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "deep", cwd=work)
        git("push", "-q", "origin", "main", cwd=work)
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING)
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.remote_head(self.FIX_BRANCH)
        self.assertEqual(self.remote_file(after, "deep.txt"), "deep\n")
        self.assertIn(before, self.parents(after))
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())

    def test_conflict_markers_left_by_the_agent_park_the_brief(self):
        name, before = self.fix_brief()
        self.advance_main("first.txt", "main's version\n")
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING)
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        self.assertIn("conflict markers left in first.txt",
                      (self.loom / "logs" / "codex" / (name + ".log")).read_text())

    def test_a_fix_brief_whose_pull_request_closed_is_parked_unrun(self):
        name, before = self.fix_brief()
        result = self.run_lane(FAKE_GH_PR_STATE="MERGED")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("codex ") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        self.assertIn("pull request #7 is no longer open",
                      (self.loom / "logs" / "codex" / (name + ".log")).read_text())
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)

    def test_a_fix_brief_never_forces_over_a_branch_that_moved(self):
        name, before = self.fix_brief()
        # The fake agent rewrites the branch from its parent instead of
        # building on it: the push must be refused, not forced.
        self.write_tool(self.bin / "codex", FAKE_AGENT.replace(
            "git add fix.txt", "git reset -q --hard HEAD^ && echo fixed > fix.txt && git add fix.txt"))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        self.assertFalse(any(c.startswith("gh pr comment") for c in self.calls()))

    def test_a_model_from_another_lanes_family_is_refused(self):
        self.drop_brief(text=BRIEF.replace("Difficulty: medium", "Difficulty: medium\nModel: claude-sonnet-4-5"))
        self.run_lane()
        self.assertFalse(any(c.startswith("codex ") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def api_lane(self, lane, extra="", **env):
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
            BRIEF.replace("Lane: codex", f"Lane: {lane}") + f"\nPROBE_PORT={port}\n{extra}")
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
        self.assertIn("keyvar=CLOUDFLARE_API_KEY key=fake-cf-token", agent[0])
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

    def operator_skills(self):
        """The operator's skill folders: links into a skills checkout, as the
        operator's skill sync leaves them, plus a folder that is not a skill."""
        checkout = self.home / "skills-checkout"
        for name in ("impeccable", "tdd"):
            (checkout / name).mkdir(parents=True)
            (checkout / name / "SKILL.md").write_text(f"{name} v1\n")
        (checkout / "notes").mkdir()
        (checkout / "notes" / "README").write_text("not a skill\n")
        for folder, names in ((".agents", ("impeccable", "tdd", "notes")), (".claude", ("impeccable",))):
            skills = self.home / folder / "skills"
            skills.mkdir(parents=True)
            for name in names:
                (skills / name).symlink_to(checkout / name)
        return checkout

    def anthropic_skill_run(self, name="T042-fix-the-thing.md"):
        self.write_tool(self.bin / "claude", FAKE_SKILL_CLAUDE)
        inbox = self.loom / "briefs" / "anthropic"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / name).write_text(BRIEF.replace("Lane: codex", "Lane: anthropic"))
        result = self.run_lane("anthropic", LOOM_ANTHROPIC_KEY_SSM="/k/an",
                               FAKE_SECRET__k_an="fake-an-key", LOOM_ANTHROPIC_MODEL="claude-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        return inbox, [c for c in self.calls() if c.startswith("skills ")]

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_api_lane_sees_the_skill_snapshot_read_only_in_each_tools_folder(self):
        checkout = self.operator_skills()
        inbox, seen = self.anthropic_skill_run()
        self.assertEqual(len(seen), 3, self.calls())
        for folder, line in zip((".claude/skills", ".agents/skills", ".config/opencode/skills"), seen, strict=True):
            self.assertIn(f"skills {folder} ", line)
            self.assertIn("impeccable v1,", line)
            self.assertIn("tdd v1,", line)
            self.assertIn("write=locked", line)
            self.assertIn("realwrite=locked", line)
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())
        # The snapshot holds copies (links followed), skills only, unchanged.
        snapshot = self.loom / "skills"
        self.assertEqual(sorted(p.name for p in snapshot.iterdir()), ["impeccable", "tdd"])
        self.assertFalse((snapshot / "impeccable").is_symlink())
        self.assertEqual((snapshot / "impeccable" / "SKILL.md").read_text(), "impeccable v1\n")
        self.assertFalse((snapshot / "planted").exists())
        self.assertEqual((checkout / "impeccable" / "SKILL.md").read_text(), "impeccable v1\n")
        # Read-only all through, the top folder included.
        for p in (snapshot, *snapshot.rglob("*")):
            self.assertFalse(p.stat().st_mode & 0o222, p)

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_the_skill_snapshot_refreshes_at_most_hourly_and_a_failed_refresh_keeps_it(self):
        checkout = self.operator_skills()
        self.anthropic_skill_run()
        snapshot = self.loom / "skills" / "impeccable" / "SKILL.md"
        (checkout / "impeccable" / "SKILL.md").write_text("impeccable v2\n")
        # Within the hour: no refresh.
        self.anthropic_skill_run("T043-again.md")
        self.assertEqual(snapshot.read_text(), "impeccable v1\n")
        # An hour later, a link with no target makes the copy fail.
        (checkout / "tdd" / "broken").symlink_to(self.home / "nowhere")
        stamp = self.loom / "run" / "skills.stamp"
        old = time.time() - 2 * 3600
        os.utime(stamp, (old, old))
        _, seen = self.anthropic_skill_run("T044-once-more.md")
        self.assertEqual(snapshot.read_text(), "impeccable v1\n")
        self.assertIn("impeccable v1,", seen[-1])
        log = (self.loom / "logs" / "skills.log").read_text().splitlines()
        self.assertEqual(len(log), 1, log)
        self.assertIn("skill refresh failed", log[0])
        self.assertFalse((self.loom / "skills.next").exists())
        # Fixed source, an hour later again: the new version arrives.
        (checkout / "tdd" / "broken").unlink()
        os.utime(stamp, (old, old))
        self.anthropic_skill_run("T045-last.md")
        self.assertEqual(snapshot.read_text(), "impeccable v2\n")

    def agy_lane(self, runas_ok=True, model="gemini-3.8-flash-high", cache_link=None, **env):
        lane_home = Path(self._tmp.name) / "loomuser"
        workdir = lane_home / "lanes"
        (lane_home / "tmp").mkdir(parents=True)
        workdir.mkdir()
        # agy's real state: config, and memory a previous run left behind.
        (lane_home / ".gemini" / "config").mkdir(parents=True, exist_ok=True)
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
            LOOM_AGY_MODEL=model,
            **{"FAKE_KC_PW": "fake-kc-password", "FAKE_SECRET__k_kc": "fake-kc-password", **env},
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
        # Edits only: no shell commands (the lane user's keychain hands agy's
        # token to /usr/bin/security without a prompt), no skill expansion.
        self.assertIn("--disable-slash-commands", agy[0])
        self.assertNotIn("--dangerously-skip-permissions", agy[0])
        self.assertIn("shell commands are not available", agy[0])
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
        # The wipe itself, checked on disk: the planted memory is gone.
        self.assertFalse((state / "brain" / "planted.md").exists())
        self.assertIn("lane agy, model gemini-3.8-flash-high", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_lane_user_gets_the_skills_where_agy_reads_them_read_only(self):
        self.operator_skills()
        # A copy an earlier run left, read-only, is replaced, not merged.
        stale = Path(self._tmp.name) / "loomuser" / ".gemini" / "config" / "skills" / "gone"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("gone\n")
        stale.chmod(0o555)
        self.addCleanup(lambda: stale.exists() and stale.chmod(0o755))
        inbox, workdir, result, agy = self.agy_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(agy), 1, self.calls())
        self.assertIn("skill=impeccable v1", agy[0])
        self.assertIn("skillw=locked", agy[0])
        copy = workdir.parent / ".gemini" / "config" / "skills"
        self.assertEqual(sorted(p.name for p in copy.iterdir()), ["impeccable", "tdd"])
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_agy_lane_is_unavailable_when_its_keychain_will_not_unlock(self):
        inbox, _, result, agy = self.agy_lane(FAKE_KC_PW="some-other-password")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(agy, [])
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "agy.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("unlock", beat["reason"])

    def test_agy_lane_refuses_a_keychain_password_security_would_split(self):
        pw = "two words"
        inbox, _, result, agy = self.agy_lane(FAKE_KC_PW=pw, FAKE_SECRET__k_kc=pw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(agy, [])
        self.assertFalse(any(c.startswith("security") for c in self.calls()))
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "agy.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("security -i would split", beat["reason"])
        self.assertNotIn(pw, beat["reason"])

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

    SPARK = {"LOOM_SPARK_URL": "http://spark.example.invalid:8000/v1",
             "LOOM_SPARK_MODEL": "spark/coder-test"}

    def spark_beat(self):
        return json.loads((self.loom / "state" / "usage" / "spark.json").read_text())

    def spark_log(self):
        return (self.loom / "logs" / "spark" / "T042-fix-the-thing.log").read_text()

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_spark_lane_runs_opencode_against_its_endpoint_and_takes_a_private_repo(self):
        # The local coder trains on nothing: no privacy lookup, private is fine.
        inbox, result = self.api_lane("spark", FAKE_GH_PRIVATE="true", **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertFalse(any(c.startswith("gh api ") for c in calls), calls)
        self.assertTrue(any(c.startswith("curl ") and "http://spark.example.invalid:8000/v1/models" in c
                            for c in calls), calls)
        agent = [c for c in calls if c.startswith("opencode ")]
        self.assertEqual(len(agent), 1, calls)
        self.assertIn("-m spark/coder-test", agent[0])
        self.assertIn("keyvar=none", agent[0])
        self.assertIn("secret=denied", agent[0])
        self.assertIn("small=spark/coder-test", agent[0])
        config = json.loads(next(c for c in calls if c.startswith("config="))[len("config="):])
        self.assertEqual(config["model"], "spark/coder-test")
        provider = config["provider"]["spark"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(provider["options"], {"baseURL": "http://spark.example.invalid:8000/v1"})
        self.assertEqual(list(provider["models"]), ["coder-test"])
        self.assertIn("lane spark, model spark/coder-test", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())
        self.assertTrue(self.spark_beat()["available"])

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_spark_lane_takes_the_brief_model_line(self):
        brief = BRIEF.replace("Lane: codex", "Lane: spark\nModel: spark/other-coder")
        inbox = self.loom / "briefs" / "spark"
        inbox.mkdir(parents=True)
        (inbox / "T042-fix-the-thing.md").write_text(brief)
        self.run_lane("spark", **self.SPARK)
        agent = [c for c in self.calls() if c.startswith("opencode ")]
        self.assertEqual(len(agent), 1, self.calls())
        self.assertIn("-m spark/other-coder", agent[0])

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_a_refused_spark_endpoint_is_a_limit_that_hands_the_brief_on(self):
        inbox, result = self.api_lane("spark", FAKE_CURL="refused", **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertFalse((self.loom / "worktrees" / "T042-fix-the-thing").exists())
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: limit: the spark endpoint refused the connection", self.spark_log())
        beat = self.spark_beat()
        self.assertFalse(beat["available"])
        self.assertTrue(beat["reason"].startswith("limit: "), beat)
        # The limit holds for 15 minutes, without asking the endpoint again.
        ends = int((self.loom / "state" / "usage" / "spark.limit").read_text().split()[0])
        self.assertAlmostEqual(ends - time.time(), 900, delta=30)
        probes = sum(c.startswith("curl ") for c in self.calls())
        (inbox / "T043-next.md").write_text(BRIEF.replace("Lane: codex", "Lane: spark"))
        self.run_lane("spark", FAKE_CURL="200", **self.SPARK)
        self.assertEqual(sum(c.startswith("curl ") for c in self.calls()), probes)
        self.assertFalse(self.spark_beat()["available"])
        self.assertTrue((inbox / "failed" / "T043-next.md").exists())
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_a_spark_run_cut_off_by_a_503_is_a_limit_not_a_failure(self):
        inbox, result = self.api_lane("spark", extra="AGENT_FAIL=1\n", FAKE_CURL="200 503", **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith("opencode ") for c in self.calls()), self.calls())
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: limit: the spark endpoint answered 503 (agent exited 3)", self.spark_log())
        beat = self.spark_beat()
        self.assertFalse(beat["available"])
        self.assertTrue(beat["reason"].startswith("limit: "), beat)

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_a_spark_run_that_fails_while_the_endpoint_answers_is_an_ordinary_failure(self):
        inbox, _ = self.api_lane("spark", extra="AGENT_FAIL=1\n", **self.SPARK)
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: agent exited 3", self.spark_log())
        self.assertNotIn("limit:", self.spark_log())
        self.assertTrue(self.spark_beat()["available"])
        self.assertFalse((self.loom / "state" / "usage" / "spark.limit").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_spark_lane_refuses_a_model_from_elsewhere(self):
        inbox, _ = self.api_lane("spark", LOOM_SPARK_URL=self.SPARK["LOOM_SPARK_URL"],
                                 LOOM_SPARK_MODEL="opencode/space-bunny-free")
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_spark_lane_without_its_url_reports_unavailable_and_keeps_its_briefs(self):
        inbox, result = self.api_lane("spark", LOOM_SPARK_MODEL="spark/coder-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = self.spark_beat()
        self.assertFalse(beat["available"])
        self.assertIn("LOOM_SPARK_URL", beat["reason"])
        self.assertFalse(beat["reason"].startswith("limit"))

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_an_unreachable_spark_endpoint_is_not_a_limit_and_keeps_its_briefs(self):
        inbox, _ = self.api_lane("spark", FAKE_CURL="timeout", **self.SPARK)
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        self.assertFalse(self.spark_beat()["available"])
        self.assertFalse(self.spark_beat()["reason"].startswith("limit"))

    def test_lane_without_its_credential_reports_unavailable_and_keeps_its_briefs(self):
        inbox, result = self.api_lane(
            "anthropic", LOOM_ANTHROPIC_KEY_SSM="/k/missing", LOOM_ANTHROPIC_MODEL="claude-test"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith(("opencode ", "claude ")) for c in self.calls()))
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "anthropic.json").read_text())
        self.assertFalse(beat["available"])

    def test_a_slot_reads_its_lanes_settings_and_keeps_its_own_inbox(self):
        inbox, result = self.api_lane(
            "anthropic-2", LOOM_ANTHROPIC_KEY_SSM="/k/missing", LOOM_ANTHROPIC_MODEL="claude-test"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "anthropic-2.json").read_text())
        self.assertEqual(beat["lane"], "anthropic-2")
        self.assertFalse(beat["available"])
        self.assertNotIn("unknown lane", beat["reason"])  # mapped to the anthropic lane
        self.assertFalse((self.loom / "state" / "usage" / "anthropic.json").exists())

    # The anthropic lane's claude: records its argv in TMPDIR (its environment
    # is emptied), edits the tree, writes a warning to stderr, and prints
    # what it is given on stdout (the CLI's JSON result, or nothing).
    def fake_claude(self, stdout, code=0):
        self.write_tool(self.bin / "claude", """#!/usr/bin/env bash
args="$*"; args="${args//$'\\n'/ }"
printf 'claude %%s\\n' "$args" >> "$TMPDIR/agent.log"
echo fixed > fix.txt
echo "a warning on stderr" >&2
printf '%%s' %s
exit %d
""" % (shlex.quote(stdout), code))

    def anthropic_run(self, slot="anthropic", **env):
        return self.api_lane(slot, **{"LOOM_ANTHROPIC_KEY_SSM": "/k/an",
                                      "FAKE_SECRET__k_an": "fake-an-key",
                                      "LOOM_ANTHROPIC_MODEL": "claude-test", **env})

    def spend_lines(self):
        path = self.loom / "state" / "usage" / "spend.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_an_anthropic_run_records_its_cost_from_the_json_result(self):
        self.fake_claude(json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "All done: the thing works.", "total_cost_usd": 0.4321,
            "usage": {"input_tokens": 12, "output_tokens": 345,
                      "cache_creation_input_tokens": 600, "cache_read_input_tokens": 7000},
        }) + "\n")
        inbox, result = self.anthropic_run("anthropic-3")
        self.assertEqual(result.returncode, 0, result.stderr)
        agent = [c for c in self.calls() if c.startswith("claude ")]
        self.assertEqual(len(agent), 1, self.calls())
        self.assertIn("-p --output-format json --model claude-test", agent[0])
        [line] = self.spend_lines()
        self.assertEqual(line["cost_usd"], 0.4321)
        self.assertEqual((line["slot"], line["ticket"], line["model"]),
                         ("anthropic-3", "T042-fix-the-thing", "claude-test"))
        self.assertEqual((line["input_tokens"], line["output_tokens"],
                          line["cache_creation_input_tokens"], line["cache_read_input_tokens"]),
                         (12, 345, 600, 7000))
        self.assertGreater(line["at"], time.time() - 120)
        # The lane log keeps the human-readable result, not the raw JSON.
        log = (self.loom / "logs" / "anthropic-3" / "T042-fix-the-thing.log").read_text()
        self.assertIn("All done: the thing works.", log)
        self.assertIn("a warning on stderr", log)
        self.assertNotIn("total_cost_usd", log)
        self.assertEqual(list((self.loom / "logs" / "anthropic-3").glob("*.result")), [])
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_an_anthropic_run_without_a_result_records_unknown_cost_not_zero(self):
        self.fake_claude("killed before the result\n", code=3)
        inbox, result = self.anthropic_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        [line] = self.spend_lines()
        self.assertIsNone(line["cost_usd"])
        self.assertEqual(line["ticket"], "T042-fix-the-thing")
        log = (self.loom / "logs" / "anthropic" / "T042-fix-the-thing.log").read_text()
        self.assertIn("killed before the result", log)
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_anthropic_credit_under_the_floor_makes_the_slot_unavailable(self):
        self.fake_claude("")
        usage = self.loom / "state" / "usage"
        usage.mkdir(parents=True)
        (usage / "spend.jsonl").write_text(
            json.dumps({"at": 1000, "slot": "anthropic", "cost_usd": 50.0}) + "\n"
            + json.dumps({"at": int(time.time()) - 60, "slot": "anthropic-2", "cost_usd": 6.5}) + "\n")
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        inbox, result = self.anthropic_run("anthropic-2", LOOM_ANTHROPIC_BUDGET_USD="10",
                                           LOOM_ANTHROPIC_BUDGET_SINCE=since)
        self.assertEqual(result.returncode, 0, result.stderr)
        beat = json.loads((usage / "anthropic-2.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("remaining $3.50 is under the floor of $5.00", beat["reason"])
        self.assertFalse(any(c.startswith(("claude ", "aws ")) for c in self.calls()))
        self.assertTrue((inbox / "T042-fix-the-thing.md").exists())
        # Above the floor (a lower floor here) the slot is available again.
        self.run_lane("anthropic-2", LOOM_ANTHROPIC_BUDGET_USD="10",
                      LOOM_ANTHROPIC_BUDGET_SINCE=since, LOOM_ANTHROPIC_FLOOR_USD="2",
                      LOOM_ANTHROPIC_KEY_SSM="/k/missing")
        beat = json.loads((usage / "anthropic-2.json").read_text())
        self.assertNotIn("floor", beat["reason"])

    def test_a_codex_slot_runs_its_brief_with_codex(self):
        slot = self.loom / "briefs" / "codex-2"
        slot.mkdir(parents=True, exist_ok=True)
        (slot / "T042-fix-the-thing.md").write_text(BRIEF)
        result = self.run_lane("codex-2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith("codex ") for c in self.calls()))
        self.assertTrue((slot / "done" / "T042-fix-the-thing.md").exists())
        beat = json.loads((self.loom / "state" / "usage" / "codex-2.json").read_text())
        self.assertEqual(beat["lane"], "codex-2")

    def test_a_bad_lane_name_is_refused(self):
        for name in ("anthropic-", "../codex", "codex-2/x", "Codex"):
            result = self.run_lane(name)
            self.assertEqual(result.returncode, 2, name)
        self.assertEqual(self.calls(), [])

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

    def test_a_recorded_pid_that_is_not_this_lanes_runner_is_left_alone(self):
        # The recorded pid may have been recycled by an unrelated process.
        self.drop_brief()
        lock = self.loom / "run" / "codex.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text("99999999\n")
        bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
        (lock / "pgid").write_text(f"{bystander.pid}\n")
        try:
            result = self.run_lane()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(bystander.poll())
            self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())
        finally:
            bystander.kill()
            bystander.wait()

    def test_an_agent_orphaned_by_a_killed_runner_is_stopped_by_the_next_pass(self):
        # What a reinstall did: launchd's bootout kills the runner's process
        # group mid-brief, and the agent (under timeout, in a group of its own)
        # lives on. The next pass must stop it before running anything.
        self.drop_brief()
        agent_pid = Path(self._tmp.name) / "agent.pid"
        second = Path(self._tmp.name) / "second-run"
        self.write_tool(self.bin / "real-codex", FAKE_AGENT)
        self.write_tool(self.bin / "codex",
                        "#!/bin/bash\n"
                        f"if [ -e '{second}' ]; then exec \"{self.bin / 'real-codex'}\" \"$@\"; fi\n"
                        f"echo $$ > '{agent_pid}'\n"
                        "exec sleep 30\n")
        runner = subprocess.Popen(["bash", str(RUNNER), "codex"], env=self.lane_env(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        orphan = None
        try:
            for _ in range(100):
                if agent_pid.exists() and agent_pid.read_text().strip():
                    break
                time.sleep(0.1)
            orphan = int(agent_pid.read_text())
            os.killpg(runner.pid, signal.SIGKILL)
            runner.wait(timeout=5)
            time.sleep(0.3)
            os.kill(orphan, 0)  # the agent outlived its runner
            second.write_text("")
            result = self.run_lane()
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.assertRaises(ProcessLookupError):
                for _ in range(30):
                    os.kill(orphan, 0)
                    time.sleep(0.1)
            self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())
        finally:
            if orphan:
                try:
                    os.kill(orphan, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_stale_lock_without_a_live_group_is_reclaimed(self):
        self.drop_brief()
        lock = self.loom / "run" / "codex.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text("99999999\n")
        (lock / "pgid").write_text("99999999\n")
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_wait_job_gone_counts_a_job_that_vanishes_at_the_last_look_as_gone(self):
        # A fake launchctl that finds the job 50 times, then not.
        fake = self.bin / "launchctl"
        count = Path(self._tmp.name) / "prints"
        fake.write_text("#!/bin/bash\n"
                        f"n=$(cat '{count}' 2>/dev/null || echo 0); n=$((n+1)); echo $n > '{count}'\n"
                        "[ $n -le 50 ]\n")
        fake.chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; sleep() { :; }; wait_job_gone gui/1/x', "test", str(INSTALL_LOCK)],
            env={"PATH": f"{self.bin}:/usr/bin:/bin"}, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_busy_check_recognizes_a_live_runner_pid(self):
        lock = Path(self._tmp.name) / "lane.lock"
        lock.mkdir()
        (lock / "pid").write_text(f"{os.getpid()}\n")
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; runner_busy "$2"', "test",
             str(INSTALL_LOCK), str(lock)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        (lock / "pid").write_text("99999999\n")
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; runner_busy "$2"', "test",
             str(INSTALL_LOCK), str(lock)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
