"""lanes/run_lane.sh run as a process: a temp loom home, a local bare repo as
the remote, and fake agent and gh commands on PATH that record their calls."""

import json
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import sys
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import hunt_plan, pr_repair, text, tickets  # noqa: E402
from tests import sandbox_caps  # noqa: E402
from tests.local_http import LocalHTTPServer  # noqa: E402

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
[ -z "${FAKE_GH_TOKENS:-}" ] || printf '%s\\n' "${GH_TOKEN:-none}" >> "$FAKE_GH_TOKENS"
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
if [ "$1 $2" = "pr comment" ] && [ -n "${FAKE_COMMENT:-}" ]; then
  while [ $# -gt 0 ]; do [ "$1" = "--body" ] && printf '%s' "$2" > "$FAKE_COMMENT"; shift; done
  exit 0
fi
if [ "$1 $2" = "pr create" ]; then
  [ -z "${FAKE_GH_CREATE_FAIL:-}" ] || { echo "pr create blew up" >&2; exit 1; }
  case " $* " in *" --label "*) [ -z "${FAKE_GH_NO_LABELS:-}" ] || { echo "could not add label: 'class:logic' not found" >&2; exit 1; } ;; esac
  while [ $# -gt 0 ]; do [ "$1" = "--body" ] && printf '%s' "$2" > "$FAKE_BODY"; shift; done
  echo "https://github.com/example/repo/pull/7"
fi
# Fake factory incidents: `search issues` prints $FAKE_GH_SEARCH (default
# empty: no open incident), `issue create` writes its --body to
# $FAKE_BODY.issue and prints the new issue's URL, `issue comment`
# succeeds silently.
if [ "$1 $2" = "search issues" ]; then
  printf '%s\n' "${FAKE_GH_SEARCH:-}"
  exit 0
fi
if [ "$1 $2" = "issue create" ]; then
  [ -z "${FAKE_GH_ISSUE_CREATE_FAIL:-}" ] || { echo "issue create blew up" >&2; exit 1; }
  while [ $# -gt 0 ]; do [ "$1" = "--body" ] && printf '%s' "$2" > "$FAKE_BODY.issue"; shift; done
  echo "https://github.com/quadseven/loom/issues/${FAKE_GH_ISSUE_NUMBER:-99}"
  exit 0
fi
if [ "$1 $2" = "issue comment" ]; then
  exit 0
fi
"""

# Fake agent: records the call, then commits a file ($FAKE_AGENT=commit),
# only edits it ($FAKE_AGENT=edit), makes no file change ($FAKE_AGENT=noop),
# makes none but writes $FAKE_SUMMARY to the Intent summary and
# $FAKE_THREAD_ANSWER as an answer to thread PRRT_9 ($FAKE_AGENT=no-change),
# exits non-zero ($FAKE_AGENT=fail), or prints $FAKE_OUT and exits
# $FAKE_EXIT, editing a file first when $FAKE_EDIT is set ($FAKE_AGENT=say).
# $FAKE_AGENT=stray also commits files no brief here names (a hook config,
# a scratch file under .loom/, a top-level test script).
# The file holds $FAKE_AGENT_TEXT (default "fixed"); $FAKE_AGENT_WEAKEN
# rewrites the validation contract.
FAKE_AGENT = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$FAKE_LOG"
[ -z "${FAKE_AGENT_ENV:-}" ] || printf '%s\\n' "${GH_TOKEN:-none}" "${LOOM_GITHUB_APP_ID:-none}" "${GIT_CONFIG_COUNT:-none}" >> "$FAKE_AGENT_ENV"
cat > /dev/null
if [ "${FAKE_AGENT:-commit}" = fail ]; then exit 3; fi
if [ "${FAKE_AGENT:-commit}" = say ]; then
  [ -z "${FAKE_EDIT:-}" ] || echo partial > fix.txt
  printf '%s\\n' "$FAKE_OUT"
  exit "${FAKE_EXIT:-1}"
fi
if [ "${FAKE_AGENT:-commit}" = noop ]; then exit 0; fi
if [ "${FAKE_AGENT:-commit}" = no-change ]; then
  mkdir -p .loom
  [ -z "${FAKE_SUMMARY:-}" ] || printf '%s\\n' "$FAKE_SUMMARY" > .loom/summary.md
  if [ -n "${FAKE_THREAD_ANSWER:-}" ]; then
    mkdir -p .loom-answers
    printf '%s\\n' "$FAKE_THREAD_ANSWER" > .loom-answers/PRRT_9.md
  fi
  exit 0
fi
if [ "${FAKE_AGENT:-commit}" = answer-file ]; then
  mkdir -p .loom
  printf '%s' "${FAKE_ANSWER_TEXT:-The cause is the configured retry limit.}" > .loom/answer.md
  exit 0
fi
if [ "${FAKE_AGENT:-commit}" = private-ip ]; then
  echo "fixed" > fix.txt
  printf 'contact %s\\n' "$(printf 'MTAuMjMuNC41' | base64 -d)" >> fix.txt
  exit 0
fi
if [ "${FAKE_AGENT:-commit}" = private-ref ]; then
  echo "fixed" > fix.txt
  echo "see repo-legacy-private#3 and example/repo#7" >> fix.txt
  exit 0
fi
if [ "${FAKE_AGENT:-commit}" = verify-file ]; then
  printf '{"missing": "the station log"}' > verify.json
  exit 0
fi
if [ "${FAKE_AGENT:-commit}" = limit ]; then
  echo "ERROR: You've hit your usage limit. Try again at 12:21 PM."
  exit 1
fi
if [ "${FAKE_AGENT:-commit}" = early-limit ]; then
  echo "ERROR: rate limit exceeded"
  for i in 1 2 3 4 5 6; do echo "working on step $i"; done
  exit 1
fi
if [ "${FAKE_AGENT:-commit}" = answer ]; then
  mkdir -p .loom-answers
  echo "Not valid: a.py:2 already guards it." > .loom-answers/PRRT_1.md
  ln -s "$FAKE_SECRET_FILE" .loom-answers/PRRT_2.md
  ln "$FAKE_SECRET_FILE" .loom-answers/PRRT_3x.md
  mkfifo .loom-answers/PRRT_4x.md
  echo "scratch" > .loom-answers/notes.txt
fi
[ -f vendor/sub/value.txt ] && echo "agent-saw submodule" >> "$FAKE_LOG"
[ -z "${FAKE_AGENT_WEAKEN:-}" ] || { mkdir -p .loom; echo 'check: true' > .loom/validation.conf; }
if [ -n "${FAKE_REPLIES:-}" ]; then mkdir -p .loom; printf '%s' "$FAKE_REPLIES" > .loom/replies.json; fi
if [ "${FAKE_AGENT:-commit}" = replies-only ]; then exit 0; fi
if [ "${FAKE_AGENT:-commit}" = probes ]; then
  printf '# test\\n' > .test_write
  mkdir -p bin
  printf 'test bin in workspace\\n' > bin/test
  printf 'fixed\\n' > fix.txt
  git add -A
  git -c user.name=t -c user.email=t@example.invalid commit -qm "Fix the thing"
  exit 0
fi
if [ "${FAKE_AGENT:-commit}" = stray ]; then
  mkdir -p .agents .loom
  printf '{"hooks": []}\n' > .agents/hooks.json
  printf 'scratch\nmore\n' > .loom/test.txt
  printf '#!/bin/sh\necho hook\n' > test_hook.sh
  printf 'fixed\n' > fix.txt
  git add -A
  git -c user.name=t -c user.email=t@example.invalid commit -qm "Fix the thing"
  exit 0
fi
if [ "${FAKE_AGENT:-commit}" = binref ]; then
  mkdir -p bin
  printf '#!/bin/sh\\n' > bin/tool
  printf '#!/bin/sh\\n./bin/tool\\n' > run.sh
  printf 'fixed\\n' > fix.txt
  git add -A
  git -c user.name=t -c user.email=t@example.invalid commit -qm "Fix the thing"
  exit 0
fi
printf '%s\\n' "${FAKE_AGENT_TEXT:-fixed}" > fix.txt
if [ "${FAKE_AGENT:-commit}" = edit ]; then exit 0; fi
if [ "${FAKE_AGENT:-commit}" = detached ]; then git checkout -q --detach HEAD; fi
git add fix.txt
git -c user.name=t -c user.email=t@example.invalid commit -qm "Fix the thing"
"""


# Fake opencode: records argv plus what the agent can see (key, HOME, SSH
# agent, GitHub token, a secret planted in the real HOME, and whether the key
# shows in any process's argv), then edits a file. The runner empties the
# agent's environment, so FAKE_LOG does not reach it; it records to TMPDIR,
# which the runner points at the agent's scratch home.
FAKE_OPENCODE = """#!/usr/bin/env bash
[ "${1:-}" = --version ] && { echo "1.18.4"; exit 0; }
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
printf 'tempvars=%s|%s|%s|%s\\n' "$HOME" "$TMPDIR" "$TMP" "$TEMP" >> "$TMPDIR/agent.log"
procs="$(ps -axo args= 2>/dev/null)"
case "$procs" in *"$key"*) argv=key-visible ;; *) argv=clean ;; esac
printf 'opencode %s | keyvar=%s key=%s home=%s ssh=%s ghtoken=%s fakelog=%s secret=%s argv=%s sock=%s osa=%s lo=%s small=%s\\n' "$args" \\
  "$keyvar" "$key" "$HOME" "${SSH_AUTH_SOCK:-none}" "${GH_TOKEN:-none}" "${FAKE_LOG:-none}" \\
  "$secret" "$argv" "$sock" "$osa" "$lo" "${small:-none}" >> "$TMPDIR/agent.log"
if printf '#!/bin/sh\\ntouch "%s/hooked"\\n' "$real_home" > .git/hooks/pre-commit 2>/dev/null \\
   && chmod +x .git/hooks/pre-commit 2>/dev/null; then hook=planted; else hook=denied; fi
printf 'hook=%s\\n' "$hook" >> "$TMPDIR/agent.log"
# Outside the real HOME, the tree and the scratch home: the sandbox refuses it.
if : > "$real_home/../escaped" 2>/dev/null; then out=written; else out=denied; fi
printf 'out=%s\\n' "$out" >> "$TMPDIR/agent.log"
printf 'config=%s\\n' "${OPENCODE_CONFIG_CONTENT:-none}" >> "$TMPDIR/agent.log"
# A brief carrying AGENT_FAIL=1 ends the run with an error, as a dropped
# connection would.
case "$args" in *AGENT_FAIL=1*) exit 3 ;; esac
# AGENT_TIMEOUT=1 ends it as `timeout` does when the run outlasts its limit.
case "$args" in *AGENT_TIMEOUT=1*) exit 124 ;; esac
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
(cd "$HOME" && mkdir -p .local/share/opencode/log)
echo "request failed: HTTP 400" > "$HOME/.local/share/opencode/log/opencode.log"
ln -s "$real_home/.ssh/id_planted" "$HOME/.local/share/opencode/storage/session/leak.json" 2>/dev/null
# A link for a whole directory: .claude -> a directory outside the home.
ln -s "$real_home/loom/run/elsewhere" "$HOME/.claude" 2>/dev/null
# A name with a newline and "..": a line-based reader would see "../outside.json".
(cd "$HOME/.local/share/opencode" && mkdir -p "x"$'\\n'".." && echo decoy > "x"$'\\n'"../outside.json")
echo fixed > fix.txt
"""

# opencode 2.0 starts a real TCP listener so the run checks the sandbox rule.
FAKE_OPENCODE_2 = FAKE_OPENCODE.replace('echo "1.18.4"', 'echo "opencode v2.0.28"').replace(
    'args="$*"; args=', '''if [ "${1:-}" = serve ]; then
  [ "${OPENCODE_PASSWORD:-}" ] || exit 7
  shift
  [ "$1 $3" = "--hostname --port" ] || exit 8
  port="$4"
  echo "serve --hostname $2 --port $port password=set" >> "$TMPDIR/agent.log"
  exec python3 -c 'import socket, sys
s = socket.socket()
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen()
while True:
    client, _ = s.accept()
    client.close()' "$port"
fi
if [ "${1:-}" = run ]; then
  [ "${OPENCODE_PASSWORD:-}" ] || exit 9
  [ "$2" = --server ] || exit 10
  port="${3##*:}"
  if /usr/bin/nc -z -w 1 127.0.0.1 "$port" > /dev/null 2>&1; then
    echo "server=reachable password=set" >> "$TMPDIR/agent.log"
  else
    echo "server=denied" >> "$TMPDIR/agent.log"
    exit 11
  fi
fi
args="$*"; args=''', 1)

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
if [ "${FAKE_FAIL_AGY_SETTINGS_KEEP:-}" = 1 ] && [ "${!#}" = keep ]; then exit 1; fi
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
# The permission rules this run got, and whether it can widen them for the next.
P="$G/antigravity-cli/settings.json"
cp "$P" "$TMPDIR/agy-settings-seen.json"
if echo '{"permissions":{"allow":["read_url(*)"]}}' > "$P" 2>/dev/null; then set=writable; else set=locked; fi
printf 'agy %s | cwd=%s home=%s mem=%s cfg=%s rc=%s set=%s skill=%s skillw=%s\\n' "$args" "$PWD" "$HOME" "$mem" "$cfg" "$rc" "$set" "$skill" "$skillw" >> "$TMPDIR/agy.log"
mkdir -p "$G/antigravity-cli/brain" && echo note > "$G/antigravity-cli/brain/this-run.md"
echo fixed > fix.txt
"""

# A repo's validation contract, as its CI would run it: a pinned interpreter,
# a required submodule, a setup step and a check. check.py needs the
# submodule at its pinned revision, fails on a "broken" fix.txt, and, like a
# formatter, strips trailing spaces from fix.txt.
CONTRACT = """# The same steps CI runs.
interpreter: loom-test-python
submodules: yes
setup: loom-test-python -c 'open(".prepared", "w").write("ok")'
check: test -f .prepared && loom-test-python check.py
"""
CHECK_PY = """import pathlib, sys
assert pathlib.Path("vendor/sub/value.txt").read_text() == "v1\\n", "submodule not at its pin"
fix = pathlib.Path("fix.txt")
if fix.exists():
    text = fix.read_text()
    fix.write_text(text.rstrip() + "\\n")
    if "broken" in text:
        sys.exit("fix.txt is broken")
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
        # The file the fake agents fix: an edit to a tracked file is always
        # in the brief's scope, so a run's commit is never refused for it.
        (seed / "fix.txt").write_text("todo\n")
        git("add", "README", "fix.txt", cwd=seed)
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
        self.scanner = root / "check_private_leaks.py"
        self.scanner.write_text("""#!/usr/bin/env python3
import re, subprocess, sys
args = sys.argv[1:]
mode, value = args[0], args[1]
# The real scanner's private-issue-ref rule: an issue reference to any repo
# but the ones named with --allow-repo-ref may name a private repo.
allowed = [args[i + 1] for i, a in enumerate(args) if a == '--allow-repo-ref']
# As the real scanner builds it: the exemption sits after the optional owner.
exempt = r'(?!(?:%s)\\b)' % '|'.join(map(re.escape, allowed)) if allowed else ''
ref = re.compile(r'(?<![\\w/-])(?:[a-z0-9][a-z0-9._-]*/)?' + exempt + r'[a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9]#\\d+\\b', re.I)
if mode == '--diff':
    text = subprocess.run(['git', 'diff', '-U0', value], capture_output=True,
                          text=True, check=True).stdout
    diff = True
else:
    text = open(value, encoding='utf-8').read()
    diff = False
for number, line in enumerate(text.splitlines(), 1):
    if diff and (line.startswith(('---', '+++', '-'))):
        continue
    if re.search(r'\\b(?:10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}|192\\.168\\.\\d{1,3}\\.\\d{1,3}|172\\.(?:1[6-9]|2[0-9]|3[01])\\.\\d{1,3}\\.\\d{1,3})\\b', line):
        print(f'BLOCKED: private identifier found\\n  {value}:{number} [rfc1918-ip] - a private-range IP address', file=sys.stderr)
        sys.exit(1)
    if ref.search(line):
        print(f'BLOCKED: private identifier found\\n  {value}:{number} [private-issue-ref] - a cross-repo issue reference', file=sys.stderr)
        sys.exit(1)
print('leak guard: clean (shape patterns only)')
""")
        self.scanner.chmod(0o755)

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
            "LOOM_LEAK_SCANNER": str(self.scanner),
            "LOOM_CODEX_MODEL": "gpt-test-default",
            "LOOM_CLAUDE_MODEL": "claude-test-default",
            # The free lanes' usage meters (bus/meter.py) read a closed
            # loopback port, never the real provider, unless a test serves one.
            "LOOM_OPENROUTER_METER_URL": "http://127.0.0.1:9/meter",
            "LOOM_WORKERSAI_METER_URL": "http://127.0.0.1:9/meter",
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
        # The function text goes to bash -c directly: bash 3.2 (macOS, as CI
        # runs it) sources nothing from a process substitution.
        function = re.search(r"^fix_items\(\) \{\n.*?^\}\n", RUNNER.read_text(), re.S | re.M).group(0)
        result = subprocess.run(["bash", "-c", function + 'fix_items "$1"', "bash", str(brief)],
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

    def pushed_files(self, branch="loom/T042-fix-the-thing"):
        clone = Path(self._tmp.name) / "pushed-clone"
        git("clone", "-q", str(self.remote), str(clone))
        out = subprocess.run(["git", "diff", "--name-only", "main",
                              "origin/%s" % branch],
                             cwd=clone, capture_output=True, text=True,
                             check=True)
        return sorted(out.stdout.splitlines())

    def run_log(self, name="T042-fix-the-thing.log"):
        return (self.loom / "logs" / "codex" / name).read_text()

    def test_probe_files_are_dropped_before_the_pr_opens(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="probes")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The pushed branch carries only the real change.
        self.assertEqual(self.pushed_files(), ["fix.txt"])
        # The run log names each file the runner dropped.
        log = self.run_log()
        self.assertIn("dropped sandbox probe file: .test_write", log)
        self.assertIn("dropped sandbox probe file: bin/test", log)
        # The PR Intent section names each file the runner dropped.
        body = self.body.read_text()
        self.assertIn("Dropped sandbox probe files: .test_write, bin/test",
                      body)
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_a_bin_file_the_diff_references_is_kept(self):
        self.drop_brief(text=BRIEF.replace("Make the thing work.",
                                           "Add bin/tool and the run.sh that calls it."))
        result = self.run_lane(FAKE_AGENT="binref")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pushed_files(), ["bin/tool", "fix.txt", "run.sh"])
        self.assertNotIn("dropped sandbox probe file", self.run_log())
        self.assertNotIn("Dropped sandbox probe files", self.body.read_text())
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_a_commit_adding_files_outside_the_brief_is_refused_and_nothing_pushed(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="stray")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: [work] commit refused: it adds files outside the brief's "
                      "paths: .agents/hooks.json, .loom/test.txt, test_hook.sh; nothing pushed",
                      self.run_log())
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.remote)],
                               capture_output=True, text=True, check=True).stdout
        self.assertNotIn("loom/T042-fix-the-thing", heads)
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))

    def test_public_repo_private_ip_diff_is_refused_before_push(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="private-ip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rfc1918-ip", self.run_log())
        self.assertIn("[rfc1918-ip]", self.run_log())
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.remote)],
                               capture_output=True, text=True, check=True).stdout
        self.assertNotIn("loom/T042-fix-the-thing", heads)
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_a_diff_naming_a_private_repo_with_this_repos_name_in_it_is_refused(self):
        # The body's own-repo Refs line is exempt, nothing else: private repos
        # named NAME-legacy-private exist beside public NAME repos.
        self.drop_brief()
        self.run_lane(FAKE_AGENT="private-ref")
        self.assertIn("[private-issue-ref]", self.run_log())
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_private_repo_does_not_run_leak_scan_and_pushes(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="private-ip", FAKE_GH_PRIVATE="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pushed_files(), ["fix.txt"])
        self.assertNotIn("leak guard", self.run_log())
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()))

    def test_files_the_brief_names_commit_and_push_normally(self):
        self.drop_brief(text=BRIEF.replace(
            "Make the thing work.",
            "Make the thing work: add the hook in .agents/hooks.json, its script "
            "test_hook.sh, and a fixture under .loom/."))
        result = self.run_lane(FAKE_AGENT="stray")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pushed_files(),
                         [".agents/hooks.json", ".loom/test.txt", "fix.txt", "test_hook.sh"])
        self.assertNotIn("commit refused", self.run_log())
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_the_agent_is_told_new_files_go_only_where_the_ticket_points(self):
        self.drop_brief()
        self.run_lane()
        self.assertIn("Add new files only where the ticket points", self.log.read_text())

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

    def test_answer_deliverable_posts_comment_and_does_not_push_or_open_pr(self):
        self.drop_brief(text=BRIEF.replace("Difficulty: medium", "Difficulty: medium\nDeliverable: answer"))
        result = self.run_lane(FAKE_AGENT="answer-file")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any(c.startswith("gh issue comment 42") for c in calls), calls)
        self.assertTrue(any("configured retry limit" in c for c in calls if c.startswith("gh issue comment")))
        self.assertTrue(any(c.startswith("gh issue edit 42") and "loom:answered" in c for c in calls))
        self.assertFalse(any(c.startswith("gh pr create") or c.startswith("gh pr comment") for c in calls))
        self.assertFalse(any(c.startswith("git push") for c in calls))
        pushed = subprocess.run(["git", "ls-remote", "--heads", str(self.remote),
                                 "loom/T042-fix-the-thing"], capture_output=True,
                                text=True, check=True).stdout
        self.assertEqual(pushed, "")
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_answer_deliverable_refuses_local_paths_and_closing_keywords(self):
        for answer, reason in (("See /Users/person/private/file", "absolute path"),
                               ("This fixes #42", "closing keyword")):
            with self.subTest(reason=reason):
                self.drop_brief(text=BRIEF.replace("Difficulty: medium", "Difficulty: medium\nDeliverable: answer"))
                result = self.run_lane(FAKE_AGENT="answer-file", FAKE_ANSWER_TEXT=answer)
                self.assertEqual(result.returncode, 0, result.stderr)
                log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
                self.assertIn(reason, log)
                self.assertFalse(any(c.startswith("gh issue comment") for c in self.calls()))
                failed = list((self.inbox / "failed").glob("*.md"))
                self.assertTrue(failed)
                failed[0].rename(self.inbox / failed[0].name)

    def test_verify_deliverable_records_the_result_and_the_model_that_made_it(self):
        self.drop_brief(text=BRIEF.replace("Difficulty: medium", "Difficulty: medium\nDeliverable: verify"))
        result = self.run_lane(FAKE_AGENT="verify-file")
        self.assertEqual(result.returncode, 0, result.stderr)
        logs = self.loom / "logs" / "codex"
        self.assertEqual((logs / "T042-fix-the-thing.verify.json").read_text(),
                         '{"missing": "the station log"}')
        self.assertEqual((logs / "T042-fix-the-thing.verify.model").read_text(), "gpt-test-default\n")
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))

    def test_answer_deliverable_without_file_fails_as_no_answer_written(self):
        brief = BRIEF.replace("Difficulty: medium", "Difficulty: medium\nDeliverable: answer")
        self.drop_brief(text=brief)
        result = self.run_lane(FAKE_AGENT="noop")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertIn("no answer written", log)

    def test_no_deliverable_header_and_no_commit_still_fails(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="noop")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        # Tagged so the factory climbs the ladder at most once for it.
        self.assertIn("failed: [work] agent made no commit and wrote no reason", log)
        self.assertFalse(any(c.startswith("gh issue comment") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_a_summary_that_is_not_a_no_change_reason_is_still_a_failure(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="no-change", FAKE_SUMMARY="Make the thing work")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertIn("agent made no commit and wrote no reason", log)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse((self.loom / "logs" / "codex" / "T042-fix-the-thing.answered").exists())

    def test_no_commit_with_a_nothing_to_change_summary_is_answered_not_failed(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="no-change",
                               FAKE_SUMMARY="nothing to change: the finding no longer holds")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertNotIn("failed:", log)
        self.assertIn("T042-fix-the-thing done, answered: nothing to change", log)
        self.assertTrue((self.loom / "logs" / "codex" / "T042-fix-the-thing.answered").exists())
        calls = self.calls()
        comments = [c for c in calls if c.startswith("gh issue comment 42")]
        self.assertEqual(len(comments), 1, calls)
        self.assertIn("\n> nothing to change: the finding no longer holds\n", self.log.read_text())
        self.assertTrue(any(c.startswith("gh issue edit 42") and "--add-label loom:answered" in c
                            for c in calls), calls)
        self.assertFalse(any(c.startswith("gh pr create") or c.startswith("git push") for c in calls))
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_thread_answers_of_an_answered_run_go_to_the_station(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="no-change",
                               FAKE_THREAD_ANSWER="Does not apply: a.py:2 already guards it.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())
        kept = self.inbox / "answers" / "T042-fix-the-thing" / "PRRT_9.md"
        self.assertEqual(kept.read_text(), "Does not apply: a.py:2 already guards it.\n")
        self.assertIn("> On review thread PRRT_9:\n> Does not apply: a.py:2 already guards it.\n",
                      self.log.read_text())

    def test_thread_answers_of_a_run_that_opens_a_pull_request_are_not_kept(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="answer", FAKE_SECRET_FILE=str(self.log))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())
        self.assertFalse((self.inbox / "answers" / "T042-fix-the-thing").exists())
        self.assertNotIn(".loom-answers/PRRT_1.md", self.pushed_files())

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

    def test_agent_owned_commit_is_validated_and_pushed_without_touching_main(self):
        self.contract_repo()
        self.drop_brief()
        result = self.run_contract_lane(FAKE_AGENT="detached")
        if "sandbox_apply: Operation not permitted" in self.lane_log():
            self.skipTest("the host blocks sandbox-exec validation")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Validation: check exited 0", self.body.read_text())
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())
        pushed = subprocess.run(["git", "--git-dir", str(self.remote), "show-ref", "--heads"],
                                capture_output=True, text=True, check=True).stdout
        heads = dict(line.split(" ", 1)[::-1] for line in pushed.splitlines())
        self.assertIn("refs/heads/loom/T042-fix-the-thing", heads, pushed)
        self.assertNotEqual(heads["refs/heads/loom/T042-fix-the-thing"], heads["refs/heads/main"])

    def test_runner_commit_failure_is_transient(self):
        real_git = shutil.which("git")
        wrapper = self.bin / "git"
        wrapper.write_text("#!/bin/sh\ncase \" $* \" in *commit*) echo runner-commit-failed >&2; exit 1;; esac\n"
                          f"exec {shlex.quote(real_git)} \"$@\"\n")
        wrapper.chmod(0o755)
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="edit")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.run_log()
        self.assertIn("failed: [transient] could not commit the agent's changes", log)
        self.assertNotIn("failed: [work] could not commit the agent's changes", log)

    def test_a_repo_without_the_class_labels_still_gets_its_pr(self):
        # gh pr create fails outright on a label the repo lacks; the label is
        # added after, best effort.
        self.drop_brief()
        result = self.run_lane(FAKE_GH_NO_LABELS="1", FAKE_GH_EDIT_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), result.stderr)
        create = [c for c in self.calls() if c.startswith("gh pr create")]
        self.assertEqual(len(create), 1)
        self.assertNotIn("--label", create[0])
        self.assertTrue(any(c.startswith("gh pr edit") and "--add-label class:" in c for c in self.calls()))

    def test_a_failed_pr_create_is_parked_as_transient(self):
        # The agent's work is done and pushed; only the pull request is missing.
        self.drop_brief()
        result = self.run_lane(FAKE_GH_CREATE_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: [transient] pr create failed",
                      (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text())

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

    def test_the_agent_is_told_to_work_test_first_and_run_the_suite(self):
        self.drop_brief()
        self.run_lane()
        prompt = self.log.read_text()
        self.assertIn("## How to work", prompt)
        self.assertIn("Work test-first", prompt)
        self.assertIn("run the repo's full test suite", prompt)
        self.assertIn("cyclomatic complexity under 15", prompt)

    def test_pr_body_ends_with_refs_for_a_station_brief(self):
        self.drop_brief(text=BRIEF.replace("Lane: codex\n", "Lane: codex\nIssue: example/repo#42\n"))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.body.read_text().rstrip("\n").endswith(
            "lane codex, model gpt-test-default.\nRefs example/repo#42"))

    def test_pr_body_writes_a_word_size_as_the_review_bots_letter(self):
        # Chief accepts only XS, S, M, L: "Size: Medium" held five lane PRs
        # on 2026-10-14. The body the runner opens passes every plan check.
        self.drop_brief(text=BRIEF.replace("S: one function.", "Medium"))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.body.read_text()
        self.assertIn("\nSize: M\n", body)
        self.assertEqual(hunt_plan.problems(body), [])

    def test_pr_body_reads_an_inline_size_line(self):
        # #668: a ticket that states "Size: S" on its own line, not as a
        # ## Size section, still opens a body the review bot accepts.
        self.drop_brief(text=re.sub(r"## Size\n+S: one function\.\n", "Size: s\n", BRIEF))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.body.read_text()
        self.assertIn("\nSize: S\n", body)
        self.assertEqual(hunt_plan.problems(body), [])

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
        # The call log keeps the comment's first line (the full text has its own test).
        self.assertEqual(comment, ["gh pr comment 7 -R example/repo --body Lane codex "
                                   "(gpt-test-default) pushed 1 commit for: Check: lint; "
                                   "Thread PRRT_1 (comment 11) at a.py:3"])
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())

    def test_a_fix_round_comment_says_what_was_pushed_and_what_happens_next(self):
        # 2026-10-20: "Lane claude pushed fixes for: the findings on the
        # ticket" told the reader nothing about a round that merged main.
        self.fix_brief()
        comment = Path(self._tmp.name) / "comment.md"
        self.run_lane(FAKE_GH_EXISTING="7", FAKE_COMMENT=str(comment))
        head = self.remote_head(self.FIX_BRANCH)
        text = comment.read_text()
        self.assertTrue(text.startswith("Lane codex (gpt-test-default) pushed 1 commit for: "
                                        "Check: lint; Thread PRRT_1 (comment 11) at a.py:3\n"), text)
        subject = subprocess.run(["git", "--git-dir", str(self.remote), "log", "-1", "--format=%s",
                                  head], capture_output=True, text=True, check=True).stdout.strip()
        self.assertIn("- `%s` `%s`" % (head[:7], subject), text)
        self.assertIn("CI and review run again on the new head; the factory lands the "
                      "pull request once they pass.", text)

    def test_fix_runner_posts_and_resolves_agent_thread_replies_after_push(self):
        self.fix_brief()
        replies = ('[{"thread":"PRRT_fake","reply":"Addressed","resolve":true},'
                   '{"thread":"PRRT_keep_open","reply":"Still investigating","resolve":false}]')
        result = self.run_lane(FAKE_GH_EXISTING="7", FAKE_REPLIES=replies)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any("addPullRequestReviewThreadReply" in c and "PRRT_fake" in c for c in calls))
        self.assertTrue(any("addPullRequestReviewThreadReply" in c and "PRRT_keep_open" in c for c in calls))
        self.assertTrue(any("resolveReviewThread" in c and "PRRT_fake" in c for c in calls))
        self.assertFalse(any("resolveReviewThread" in c and "PRRT_keep_open" in c for c in calls))
        files = subprocess.run(["git", "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertNotIn(".loom/replies.json", files)

    def test_fix_runner_ignores_invalid_replies_file(self):
        self.fix_brief()
        result = self.run_lane(FAKE_GH_EXISTING="7", FAKE_REPLIES='[{"thread":4}]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("addPullRequestReviewThreadReply" in c or "resolveReviewThread" in c
                             for c in self.calls()))

    def test_reply_only_fix_posts_replies_without_a_commit_or_push(self):
        name, before = self.fix_brief()
        result = self.run_lane(FAKE_GH_EXISTING="7", FAKE_AGENT="replies-only",
                               FAKE_REPLIES='[{"thread":"PRRT_fake","reply":"Not applicable: false positive.","resolve":true}]')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any("addPullRequestReviewThreadReply" in c for c in calls))
        self.assertTrue(any("resolveReviewThread" in c for c in calls))
        self.assertFalse(any(c.startswith("git push") for c in calls))
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)

    def test_a_provider_usage_limit_is_a_limit_not_a_failure(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        result = self.run_lane(FAKE_AGENT="limit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertIn("failed: [limit] agent exited 1: provider limit reached", log)
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("provider limit reached", beat["reason"])
        self.assertAlmostEqual(beat["retry_at"] - time.time(), 3600, delta=30)
        # The next brief waits until the provider limit lifts.
        agents = sum(c.startswith("codex ") for c in self.calls())
        self.drop_brief("T043-next.md", BRIEF.replace("T042", "T043"))
        self.run_lane()
        self.assertEqual(sum(c.startswith("codex ") for c in self.calls()), agents)
        self.assertTrue((self.inbox / "T043-next.md").exists())

    def test_a_limited_ladder_model_falls_back_on_the_next_pass(self):
        models = "gpt-test-high gpt-test-low"
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        first = self.run_lane(LOOM_CODEX_MODELS=models, FAKE_AGENT="say",
                              FAKE_OUT="429 RESOURCE_EXHAUSTED quota resets in 1 hours")
        self.assertEqual(first.returncode, 0, first.stderr)
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertTrue(beat["available"])
        self.assertIn("gpt-test-high", beat["reason"])
        self.assertIn("--model gpt-test-high", "\n".join(self.calls()))
        self.drop_brief("T043-next.md", BRIEF.replace("T042", "T043"))
        second = self.run_lane(LOOM_CODEX_MODELS=models)
        self.assertEqual(second.returncode, 0, second.stderr)
        calls = "\n".join(self.calls())
        self.assertIn("--model gpt-test-low", calls)

    def test_a_ladder_holds_only_when_every_model_is_limited(self):
        models = "gpt-test-high gpt-test-low"
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        self.run_lane(LOOM_CODEX_MODELS=models, FAKE_AGENT="say",
                      FAKE_OUT="429 RESOURCE_EXHAUSTED quota resets in 2 hours")
        self.drop_brief("T043-next.md", BRIEF.replace("T042", "T043"))
        self.run_lane(LOOM_CODEX_MODELS=models, FAKE_AGENT="say",
                      FAKE_OUT="429 RESOURCE_EXHAUSTED quota resets in 3 hours")
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("gpt-test-high until", beat["reason"])
        self.assertIn("gpt-test-low until", beat["reason"])
        self.assertGreater(beat["retry_at"], int(time.time()))

    def test_an_account_quota_limits_every_model_and_takes_the_lane_offline(self):
        # agy, 2026-10-18: "Individual quota reached ... Resets in 67h" marked
        # only the model that ran; the lane stayed available and the station
        # claimed a ticket on it every few minutes for an hour.
        models = "gpt-test-high gpt-test-low"
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        result = self.run_lane(
            LOOM_CODEX_MODELS=models, FAKE_AGENT="say",
            FAKE_OUT="RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
                     "Resets in 67h19m45s.")
        self.assertEqual(result.returncode, 0, result.stderr)
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("account quota reached", beat["reason"])
        limits = (self.loom / "state" / "usage" / "codex.models").read_text()
        self.assertIn("gpt-test-high ", limits)
        self.assertIn("gpt-test-low ", limits)

    def test_a_pass_with_no_brief_stays_unavailable_while_every_model_is_limited(self):
        # agy, 2026-10-19: a quota on all four of its models; each pass with an
        # empty inbox said available, and the station claimed a ticket there
        # every few minutes that failed at once (rounds 10 to 12 of one ticket).
        now = int(time.time())
        (self.loom / "state" / "usage").mkdir(parents=True, exist_ok=True)
        (self.loom / "state" / "usage" / "codex.models").write_text(
            "gpt-test-high %d account-quota\ngpt-test-low %d account-quota\n"
            % (now + 3600, now + 7200))
        result = self.run_lane(LOOM_CODEX_MODELS="gpt-test-high gpt-test-low")
        self.assertEqual(result.returncode, 0, result.stderr)
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertFalse(beat["available"])
        self.assertTrue(beat["reason"].startswith("limit: all models limited: gpt-test-high until"),
                        beat["reason"])
        self.assertEqual(beat["retry_at"], now + 7200)
        # One model off its limit makes the lane available again.
        (self.loom / "state" / "usage" / "codex.models").write_text(
            "gpt-test-high %d account-quota\n" % (now + 3600))
        self.run_lane(LOOM_CODEX_MODELS="gpt-test-high gpt-test-low")
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertTrue(beat["available"])

    def test_ticket_model_is_not_swapped_for_a_ladder_model(self):
        self.drop_brief("T042-fix-the-thing.md",
                        BRIEF.replace("Difficulty: medium", "Difficulty: medium\nModel: gpt-test-high"))
        (self.loom / "state" / "usage").mkdir(parents=True, exist_ok=True)
        (self.loom / "state" / "usage" / "codex.models").write_text(
            "gpt-test-high %d provider-limit\n" % (int(time.time()) + 3600))
        result = self.run_lane(LOOM_CODEX_MODELS="gpt-test-high gpt-test-low")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = "\n".join(self.calls())
        self.assertNotIn("--model gpt-test-low", calls)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_the_heartbeat_names_each_limited_model_until_its_reset(self):
        # The lane stays available for its other model; the station reads
        # limited_models so a ticket pinned to the limited one is not claimed
        # here again before the reset (it was, every pass, on 2026-10-18).
        until = int(time.time()) + 3600
        (self.loom / "state" / "usage").mkdir(parents=True, exist_ok=True)
        (self.loom / "state" / "usage" / "codex.models").write_text(
            "gpt-test-high %d provider-limit\ngpt-test-low %d provider-limit\n"
            % (until, int(time.time()) - 60))
        self.drop_brief("T042-fix-the-thing.md",
                        BRIEF.replace("Difficulty: medium", "Difficulty: medium\nModel: gpt-test-high"))
        result = self.run_lane(LOOM_CODEX_MODELS="gpt-test-high gpt-test-low")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertIn("failed: [limit] limit: requested model gpt-test-high limited until", log)
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertTrue(beat["available"])
        self.assertEqual(beat["limited_models"], {"gpt-test-high": until})

    def test_empty_api_credit_holds_the_lane_for_24_hours(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        before = int(time.time())
        result = self.run_lane(FAKE_AGENT="say", FAKE_OUT="Credit balance is too low")
        self.assertEqual(result.returncode, 0, result.stderr)
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("out of credit", beat["reason"])
        self.assertGreaterEqual(beat["retry_at"], before + 86400)
        self.assertLessEqual(beat["retry_at"], int(time.time()) + 86400)

    def source_functions(self, *names):
        text = RUNNER.read_text()
        found = {n: re.search(r"^%s\(\) \{(\n.*?^\}|[^\n]*\})\n" % n, text, re.S | re.M) for n in names}
        missing = [n for n, m in found.items() if m is None]
        self.assertFalse(missing, "function not found in runner: %s" % ", ".join(missing))
        return "".join(found[n].group(0) for n in names)

    def hold_credit(self, slot, until=4102444800):
        script = self.source_functions("write_beat", "utc", "hold_shared_credit") + (
            'heartbeat() { :; }\nhold_shared_credit "$1"')
        env = {**self.lane_env(), "LOOM_HOME": str(self.loom), "SLOT": slot,
               "LOOM_ROOT": str(RUNNER.parent.parent)}
        return subprocess.run(["bash", "-c", script, "bash", str(until)], env=env,
                              capture_output=True, text=True, check=True)

    def beats(self):
        usage = self.loom / "state" / "usage"
        return {p.stem: json.loads(p.read_text()) for p in usage.glob("*.json")}

    def test_empty_credit_on_one_anthropic_slot_holds_all_eight(self):
        self.hold_credit("anthropic-3")
        beats = self.beats()
        self.assertEqual(sorted(beats), sorted(["anthropic"] + ["anthropic-%d" % n for n in range(2, 9)]))
        for beat in beats.values():
            self.assertFalse(beat["available"])
            self.assertEqual(beat["retry_at"], 4102444800)
            self.assertTrue(beat["reason"].startswith("limit: out of credit"))

    def test_empty_credit_on_a_slot_outside_the_shared_credit_holds_only_itself(self):
        self.hold_credit("codex")
        self.assertEqual(self.beats(), {})
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="Credit balance is too low")
        self.assertEqual(list(self.beats()), ["codex"])

    def test_a_usage_limit_on_one_anthropic_slot_holds_only_that_slot(self):
        # A usage limit (not credit) goes through the same path as codex's: one heartbeat.
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        self.run_lane(FAKE_AGENT="limit")
        self.assertEqual(list(self.beats()), ["codex"])

    def test_a_missing_limit_file_prints_nothing(self):
        script = self.source_functions("limit_held", "utc") + (
            'LIMIT_FILE="$LOOM_HOME/state/usage/x.limit" LIMIT_HOLD=900\nlimit_held; echo "rc=$?"')
        result = subprocess.run(["bash", "-c", script], env={**self.lane_env(), "LOOM_HOME": str(self.loom)},
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "rc=1")
        self.assertEqual(result.stderr, "")

    def test_a_limit_line_before_the_last_lines_of_output_is_still_a_limit(self):
        # loom#934: a provider limit the CLI prints before more output is still
        # a limit, because the runner now classifies from the whole run output;
        # the lane stays held until the reset.
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        self.run_lane(FAKE_AGENT="early-limit")
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertIn("failed: [limit]", log)
        self.assertNotIn("failed: [transient]", log)
        self.assertNotIn("failed: [work]", log)
        beat = json.loads((self.loom / "state" / "usage" / "codex.json").read_text())
        self.assertFalse(beat["available"])
        self.assertGreater(beat["retry_at"], int(time.time()))

    def test_a_usage_limit_followed_by_more_output_is_classed_limit(self):
        # loom#934, grug#1186 r7 on codex-2: the limit line, then three
        # "failed to record rollout items" lines, "tokens used" and the count,
        # five non-blank lines after the limit. The last five lines hold none
        # of it, so the old tail-only check logged [work] agent exited 1. The
        # runner classifies from the whole run output: this is a limit that
        # holds the lane, and the reason shown to people stays the short limit
        # reason rather than the whole output.
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        tail = "\n".join([
            "ERROR: You've hit your usage limit. Try again at Oct 19th, 2026 12:07 AM.",
            "ERROR: You've hit your usage limit. Try again at Oct 19th, 2026 12:07 AM.",
            "failed to record rollout items",
            "failed to record rollout items",
            "failed to record rollout items",
            "tokens used",
            "42,964",
        ])
        self.run_lane(FAKE_AGENT="say", FAKE_OUT=tail, FAKE_EDIT="1", FAKE_EXIT="1")
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertIn("failed: [limit]", log)
        self.assertNotIn("failed: [work]", log)
        self.assertNotIn("failed: [transient]", log)
        reason = self.failure_line()
        self.assertTrue(reason.startswith("[limit] "), reason)
        self.assertNotIn("tokens used", reason)
        self.assertNotIn("42,964", reason)
        beat = self.beat()
        self.assertFalse(beat["available"])
        self.assertGreater(beat["retry_at"], int(time.time()))

    def test_a_timeout_after_editing_keeps_the_edits_on_a_timedout_branch(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="still working", FAKE_EDIT="1", FAKE_EXIT="124")
        reason = self.failure_line()
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.remote)],
                               capture_output=True, text=True, check=True).stdout
        side = [h.split("refs/heads/")[1] for h in heads.splitlines() if "-timedout-" in h]
        self.assertEqual(len(side), 1, heads)
        self.assertTrue(side[0].startswith("loom/T042-fix-the-thing-timedout-"), side)
        self.assertTrue(reason.startswith("[transient] agent exited 124; "), reason)
        self.assertIn("the edits are on branch " + side[0], reason)
        kept = subprocess.run(["git", "--git-dir", str(self.remote), "show", side[0] + ":fix.txt"],
                              capture_output=True, text=True, check=True).stdout
        self.assertEqual(kept, "partial\n")
        self.assertNotIn("refs/heads/loom/T042-fix-the-thing\n", heads)

    def test_a_timeout_that_edited_nothing_pushes_no_side_branch(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="still working", FAKE_EXIT="124")
        self.assertEqual(self.failure_line(), "[transient] agent exited 124")
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.remote)],
                               capture_output=True, text=True, check=True).stdout
        self.assertNotIn("-timedout-", heads)

    def test_a_non_limit_pattern_is_still_read_from_the_short_tail(self):
        # loom#934: the auth and transient (network) patterns still read the
        # short tail, not the whole output. A key-expiry line buried in the
        # agent's output -- not in its last five lines -- does not hold the lane
        # as auth, so a ticket that is about expired keys is not read as one.
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        out = "Error: API key expired\n" + "\n".join(
            "working on step %d" % i for i in range(1, 7))
        self.run_lane(FAKE_AGENT="say", FAKE_OUT=out, FAKE_EDIT="1", FAKE_EXIT="1")
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertNotIn("failed: [auth]", log)
        self.assertIn("failed: [work]", log)
        self.assertTrue(self.beat()["available"])

    def test_a_finding_that_mentions_a_rate_limit_is_not_a_limit(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF + "\nThe client ignores the rate limit header.\n")
        self.run_lane(FAKE_AGENT="fail")
        log = (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()
        self.assertIn("failed: [transient] agent exited 3", log)
        self.assertNotIn("failed: limit", log)

    def test_a_fix_briefs_answers_move_to_the_inbox_and_never_into_the_commit(self):
        name, _ = self.fix_brief()
        secret = Path(self._tmp.name) / "outside-secret"
        secret.write_text("do not post me\n")
        result = self.run_lane(FAKE_AGENT="answer", FAKE_GH_EXISTING="7",
                               FAKE_SECRET_FILE=str(secret))
        self.assertEqual(result.returncode, 0, result.stderr)
        answers = self.inbox / "answers" / name
        # Only the regular file named for a thread; the symlink, the hard link
        # to a file outside the tree and the FIFO are dropped unread.
        self.assertEqual(sorted(os.listdir(answers)), ["PRRT_1.md"])
        self.assertEqual((answers / "PRRT_1.md").read_text(), "Not valid: a.py:2 already guards it.\n")
        files = subprocess.run(["git", "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                                self.remote_head(self.FIX_BRANCH)], capture_output=True, text=True).stdout.split()
        self.assertIn("fix.txt", files)
        self.assertFalse([f for f in files if f.startswith(".loom-answers")])

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

    def test_a_fix_round_that_merges_main_scans_only_what_the_pull_request_adds(self):
        # The default branch may carry text the public repo scan flags in a
        # lane's change (its own issue refs, which its own CI accepts); a fix
        # round that merges it is not refused for them (2026-10-20).
        name, before = self.fix_brief()
        self.advance_main("main.txt", "see other#5\n")
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING)
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("leak scan failed", self.run_log(name + ".log"))
        after = self.remote_head(self.FIX_BRANCH)
        self.assertEqual(self.remote_file(after, "main.txt"), "see other#5\n")
        self.assertIn(before, self.parents(after))
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())

    def test_a_fix_round_that_merges_main_still_scans_the_agents_change(self):
        name, before = self.fix_brief()
        self.advance_main("main.txt", "main moved\n")
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING + "echo 'see other#9' > first.txt\n")
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("public repo leak scan failed", self.run_log(name + ".log"))
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())

    def test_a_fix_round_that_adds_a_stray_file_beside_a_merge_is_refused(self):
        # main.txt came in with the merge, so only the agent's file is named.
        name, before = self.fix_brief()
        self.advance_main("main.txt", "main moved\n")
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING + "echo hook > test_hook.sh\n")
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("commit refused: it adds files outside the brief's paths: "
                      "test_hook.sh; nothing pushed", self.run_log(name + ".log"))
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())

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
        # The remote did not move: this run's own rewrite is a push failure.
        self.assertIn("failed: [transient] push failed",
                      (self.loom / "logs" / "codex" / (name + ".log")).read_text())
        self.assertFalse(any(c.startswith("gh pr comment") for c in self.calls()))

    def test_a_fix_brief_whose_branch_moved_during_the_run_is_parked_as_branch_moved(self):
        name, before = self.fix_brief()
        # Another lane pushes to the pull request's branch while this run's
        # agent works: the push is refused, and that is not this run's failure.
        work = Path(self._tmp.name) / "pr-work"
        self.write_tool(self.bin / "codex", FAKE_AGENT.replace(
            "printf '%s\\n' \"${FAKE_AGENT_TEXT:-fixed}\" > fix.txt\n",
            "(cd %s && echo other > other.txt && git add other.txt &&"
            " git -c user.name=t -c user.email=t@example.invalid commit -qm Other &&"
            " git push -q origin %s)\n"
            "printf '%%s\\n' \"${FAKE_AGENT_TEXT:-fixed}\" > fix.txt\n" % (work, self.FIX_BRANCH), 1))
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        moved = self.remote_head(self.FIX_BRANCH)
        self.assertNotEqual(moved, before)
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        log = (self.loom / "logs" / "codex" / (name + ".log")).read_text()
        self.assertIn("failed: [work] branch moved: %s is at %s on the remote, not %s"
                      % (self.FIX_BRANCH, moved[:12], before[:12]), log)
        self.assertNotIn("failed: push failed", log)
        self.assertFalse(any(c.startswith("gh pr comment") for c in self.calls()))

    def test_a_model_from_another_lanes_family_is_refused(self):
        self.drop_brief(text=BRIEF.replace("Difficulty: medium", "Difficulty: medium\nModel: claude-sonnet-4-5"))
        self.run_lane()
        self.assertFalse(any(c.startswith("codex ") for c in self.calls()))
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())

    def test_opencode_go_model_mismatch_is_transient(self):
        inbox = self.loom / "briefs" / "opencode-go"
        inbox.mkdir(parents=True)
        (inbox / "T042-fix-the-thing.md").write_text(
            BRIEF.replace("Lane: codex", "Lane: opencode-go")
            .replace("Difficulty: medium", "Difficulty: medium\nModel: claude-sonnet-5-5"))
        result = self.run_lane("opencode-go", LOOM_OPENCODE_GO_MODEL="opencode-go/model-free",
                               LOOM_OPENCODE_GO_KEY_SSM="/k/oc",
                               FAKE_SECRET__k_oc="fake-oc-key")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.loom / "logs" / "opencode-go" / "T042-fix-the-thing.log").read_text()
        self.assertIn("failed: [transient] model claude-sonnet-5-5 is not an opencode-go model", log)

    def api_lane(self, lane, extra="", **env):
        if not sandbox_caps.LOCALHOST:
            self.skipTest(sandbox_caps.NO_LOCALHOST)
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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
        self.assertIn("-m openrouter/vendor/model:free --print-logs --log-level ERROR", agent[0])
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
        self.assertIn("out=denied", self.calls())
        self.assertFalse((self.home.parent / "escaped").exists())
        self.assertIn("fds=ok", self.calls())
        self.assertFalse((self.home / "hooked").exists())
        # opencode's helper (title) agent must not fall back to a paid model.
        self.assertIn("small=openrouter/vendor/model:free", agent[0])
        self.assertIn(f"tempvars={self.loom}/run/openrouter-home|{self.loom}/run/openrouter-home/tmp|{self.loom}/run/openrouter-home/tmp|{self.loom}/run/openrouter-home/tmp", self.calls())
        config = next(line.removeprefix("config=") for line in self.calls()
                      if line.startswith("config="))
        self.assertEqual(json.loads(config)["permission"]["external_directory"], "deny")
        self.assertNotIn(f"home={self.home} ", agent[0])
        self.assertIn("lane openrouter, model openrouter/vendor/model:free", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    def test_opencode_temp_paths_and_external_directory_denial_keep_the_sandbox_boundary(self):
        profile_file = str(Path(self._tmp.name) / "sandbox.profile")
        self.write_tool(
            self.bin / "sandbox-exec",
            "#!/bin/sh\n[ \"$1\" = -p ] || exit 91\nprintf '%s' \"$2\" > "
            + shlex.quote(profile_file)
            + "\nshift 2\nexec \"$@\"\n",
        )
        inbox = self.loom / "briefs" / "opencode-go"
        inbox.mkdir(parents=True)
        (inbox / "T042-fix-the-thing.md").write_text(
            BRIEF.replace("Lane: codex", "Lane: opencode-go"))
        result = self.run_lane(
            "opencode-go",
            LOOM_OPENCODE_GO_KEY_SSM="/k/oc",
            FAKE_SECRET__k_oc="fake-oc-key",
            LOOM_OPENCODE_GO_MODEL="opencode-go/model-free",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        home = self.loom / "run" / "opencode-go-home"
        calls = self.calls()
        temp_dir = home / "tmp"
        self.assertIn(f"tempvars={home}|{temp_dir}|{temp_dir}|{temp_dir}", calls)
        config = next(line.removeprefix("config=") for line in calls if line.startswith("config="))
        self.assertEqual(json.loads(config)["permission"]["external_directory"], "deny")
        profile = Path(profile_file).read_text()
        self.assertIn('(deny network-outbound (remote ip "localhost:*"))', profile)
        self.assertNotRegex(profile, r'\(allow network-outbound \(remote ip "localhost:\d+"\)\)')
        # Outbound: every port denied, then the provider's HTTPS port.
        self.assertEqual(re.findall(r'\((allow|deny) network-outbound \(remote ip "\*:([^"]+)"\)\)', profile),
                         [("deny", "*"), ("allow", "443")])
        writes = [line for line in profile.splitlines() if "file-write*" in line]
        scratch = home.resolve()
        self.assertEqual(len(writes), 5)
        self.assertEqual(writes[0], '(deny file-write* (subpath "/"))')
        self.assertTrue(writes[1].startswith('(allow file-write* (literal "/dev/null")'), writes[1])
        self.assertNotIn("subpath", writes[1])
        self.assertEqual(writes[2], f'(deny file-read* file-write* (subpath "{self.home.resolve()}"))')
        allowed = re.fullmatch(
            r'\(allow file-read\* file-write\* \(subpath "(.+)"\) \(subpath "(.+)"\)\)',
            writes[3],
        )
        self.assertIsNotNone(allowed)
        tree, profile_scratch = allowed.groups()
        self.assertTrue(tree.endswith("/worktrees/T042-fix-the-thing"), tree)
        self.assertEqual(profile_scratch, str(scratch))
        self.assertEqual(writes[4], f'(deny file-write* (subpath "{tree}/.git"))')
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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
        self.assertIn("opencode.log", files)
        self.assertIn("HTTP 400", files["opencode.log"].read_text())
        for p in (self.loom / "logs").rglob("*"):
            if p.is_file() and not p.is_symlink():
                self.assertNotIn("private key", p.read_text())
                self.assertNotIn("another lane's file", p.read_text())

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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

    def meter_server(self, body):
        """A fake provider meter on loopback: answers every request with BODY
        and records the request's method, path, headers and body."""
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def answer(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                 "body": self.rfile.read(length).decode() if length else ""})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            do_GET = do_POST = answer

            def log_message(self, format, *args):
                pass

        server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return requests, "http://127.0.0.1:%d/api/v1/key" % server.server_port

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_openrouter_lane_records_its_key_meter_with_the_key_kept_out_of_argv(self):
        # loom#812: Usage said "not exposed yet" for openrouter although
        # GET /api/v1/key gives the key's free-model requests today.
        requests, url = self.meter_server({"data": {
            "label": "lane", "limit": None, "limit_remaining": None, "usage": 0,
            "is_free_tier": True, "free_model_daily_requests": {"used": 12, "limit": 50, "remaining": 38}}})
        inbox, result = self.api_lane(
            "openrouter", LOOM_OPENROUTER_KEY_SSM="/k/or", FAKE_SECRET__k_or="fake-or-key-s3cr3t",
            LOOM_OPENROUTER_MODEL="openrouter/vendor/model:free", LOOM_OPENROUTER_METER_URL=url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(r["method"], r["path"]) for r in requests], [("GET", "/api/v1/key")])
        self.assertEqual(requests[0]["headers"].get("Authorization"), "Bearer fake-or-key-s3cr3t")
        meter = json.loads((self.loom / "state" / "usage" / "openrouter.meter.json").read_text())
        self.assertIsNone(meter["error"])
        self.assertEqual([(w["window"], w["used"], w["limit"]) for w in meter["windows"]], [("daily", 12, 50)])
        self.assertNotIn("fake-or-key-s3cr3t", json.dumps(meter))
        for where in (result.stdout, result.stderr, *self.calls()):
            if where.startswith("opencode "):
                continue  # the agent's own record of the key it was handed
            self.assertNotIn("fake-or-key-s3cr3t", where)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_workersai_lane_records_todays_neurons_for_its_account(self):
        requests, url = self.meter_server({"data": {"viewer": {"accounts": [
            {"aiInferenceAdaptiveGroups": [{"sum": {"totalNeurons": 2500.0}}]}]}}, "errors": None})
        inbox, result = self.api_lane(
            "workersai", LOOM_WORKERSAI_KEY_SSM="/k/cf", FAKE_SECRET__k_cf="fake-cf-token",
            LOOM_WORKERSAI_ACCOUNT_SSM="/k/acct", FAKE_SECRET__k_acct="fakeaccount0123",
            LOOM_WORKERSAI_MODEL="cloudflare-workers-ai/@cf/vendor/model", LOOM_WORKERSAI_METER_URL=url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r["method"] for r in requests], ["POST"])
        self.assertEqual(requests[0]["headers"].get("Authorization"), "Bearer fake-cf-token")
        self.assertEqual(json.loads(requests[0]["body"])["variables"]["account"], "fakeaccount0123")
        meter = json.loads((self.loom / "state" / "usage" / "workersai.meter.json").read_text())
        self.assertEqual([(w["window"], w["used"], w["used_pct"]) for w in meter["windows"]],
                         [("daily", 2500.0, 25.0)])

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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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

    def agy_lane(self, runas_ok=True, model="gemini-3.8-flash-high", cache_link=None, agy_bin=None, **env):
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
        # The operator's sign-in trusted the work folder; an older file also
        # carries a wider rule that no run may keep.
        (lane_home / ".gemini" / "antigravity-cli" / "settings.json").write_text(json.dumps(
            {"trustedWorkspaces": [str(workdir)], "permissions": {"allow": ["read_url(*)"]}, "toolPermission": "always-proceed"}))
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
            LOOM_AGY_BIN=str(agy_bin or agy),
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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
        # Shell commands come from settings rules, never from skipping
        # permissions; no skill expansion. The brief says to run the tests.
        self.assertIn("--disable-slash-commands", agy[0])
        self.assertNotIn("--dangerously-skip-permissions", agy[0])
        self.assertNotIn("shell commands are not available", agy[0])
        self.assertIn("run the repo's tests and lint with your shell tool", agy[0])
        self.assertIn("--model gemini-3.8-flash-high", agy[0])
        self.assertIn(f"cwd={workdir.resolve()}/T042-fix-the-thing", agy[0])
        # Nothing from a previous run reaches this one, and this run cannot
        # plant config or shell startup files for the next.
        self.assertIn("mem=clean", agy[0])
        self.assertIn("cfg=locked", agy[0])
        self.assertIn("rc=locked", agy[0])
        self.assertIn("set=locked", agy[0])
        # Sign-in and install state survive the wipe; conversation lists do not.
        state = workdir.parent / ".gemini" / "antigravity-cli"
        self.assertEqual((state / "installation_id").read_text(), "install-1")
        self.assertTrue((state / "cache" / "onboarding.json").exists())
        self.assertFalse((state / "cache" / "last_conversations.json").exists())
        # The wipe itself, checked on disk: the planted memory is gone.
        self.assertFalse((state / "brain" / "planted.md").exists())
        self.assertIn("lane agy, model gemini-3.8-flash-high", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_agy_lane_rewrites_its_settings_to_allow_commands_and_deny_web_fetching(self):
        inbox, workdir, result, agy = self.agy_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(agy), 1, result.stderr)
        state = workdir.parent / ".gemini" / "antigravity-cli"
        template = json.loads((RUNNER.parent / "agy" / "settings.json").read_text())
        expected = {**template, "trustedWorkspaces": [str(workdir)]}
        # What the run saw: the template's rules, the operator's trusted work
        # folder kept, the older file's wider rule and other keys gone.
        seen = json.loads((workdir.parent / "tmp" / "agy-settings-seen.json").read_text())
        self.assertEqual(seen, expected)
        self.assertIn("command(*)", seen["permissions"]["allow"])
        self.assertIn("read_url(*)", seen["permissions"]["deny"])
        self.assertNotIn("read_url(*)", seen["permissions"]["allow"])
        # agy's own terminal sandbox denied commands the lane's rules allow
        # (infra#5522), so it is off; sandbox-exec stays the boundary.
        self.assertIs(seen["enableTerminalSandbox"], False)
        # The file is restored after the run, whatever it did, and the
        # sandbox kept the run from widening it in the first place.
        self.assertEqual(json.loads((state / "settings.json").read_text()), expected)
        self.assertFalse((state / ".settings.json.new").exists())
        self.assertIn("set=locked", agy[0])
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_lane_fails_when_settings_restore_fails(self):
        self.write_tool(self.bin / "sandbox-exec", "#!/usr/bin/env bash\nshift 2\nexec \"$@\"\n")
        inbox, workdir, result, agy = self.agy_lane(FAKE_FAIL_AGY_SETTINGS_KEEP="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(agy), 1, result.stderr)
        log = (self.loom / "logs" / "agy" / "T042-fix-the-thing.log").read_text()
        self.assertIn("could not restore agy's settings", log)
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_lane_leaves_its_settings_alone_when_the_sandbox_cannot_be_built(self):
        # The sandbox is built before the settings are written, so a run that
        # never starts leaves no rewritten file without its restore.
        lane_home = Path(self._tmp.name) / "loomuser"
        lane_home.mkdir()
        quoted = lane_home / 'a"gy'
        quoted.symlink_to(lane_home / "agy")
        inbox, workdir, result, agy = self.agy_lane(agy_bin=quoted)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(agy, [])
        state = workdir.parent / ".gemini" / "antigravity-cli"
        self.assertEqual(json.loads((state / "settings.json").read_text())["permissions"], {"allow": ["read_url(*)"]})
        self.assertFalse((state / ".settings.json.new").exists())
        log = (self.loom / "logs" / "agy" / "T042-fix-the-thing.log").read_text()
        self.assertIn("refusing sandbox: unsafe path", log)
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_settings_never_write_through_a_link_left_in_their_place(self):
        # A link at the scratch file's name would send the template into
        # whatever it points at; the runner removes it and writes a fresh file.
        victim = Path(self._tmp.name) / "victim.txt"
        victim.write_text("keep")
        state = Path(self._tmp.name) / "loomuser" / ".gemini" / "antigravity-cli"
        state.mkdir(parents=True)
        (state / ".settings.json.new").symlink_to(victim)
        inbox, workdir, result, agy = self.agy_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(victim.read_text(), "keep")
        self.assertIn("command(*)", json.loads((state / "settings.json").read_text())["permissions"]["allow"])

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_agy_lane_adds_default_effort_to_a_bare_claude_model(self):
        _, _, result, agy = self.agy_lane(model="claude-sonnet-5-5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(agy), 1, self.calls())
        self.assertIn("--model claude-sonnet-5-5-high", agy[0])

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_agy_lane_uses_its_configured_effort_for_a_bare_model(self):
        _, _, result, agy = self.agy_lane(model="claude-sonnet-5-5", LOOM_AGY_EFFORT="medium")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(agy), 1, self.calls())
        self.assertIn("--model claude-sonnet-5-5-medium", agy[0])

    @unittest.skipUnless(shutil.which("sandbox-exec"), "the agy lane runs under macOS sandbox-exec")
    def test_agy_lane_refuses_unknown_model_as_transient_before_agent_run(self):
        inbox, _, result, agy = self.agy_lane(model="gpt-6-sol")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(agy, [])
        log = (self.loom / "logs" / "agy" / "T042-fix-the-thing.log").read_text()
        self.assertIn("failed: [transient] model gpt-6-sol is not in the agy catalog", log)
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_zen_lane_runs_a_free_model_sandboxed_without_a_key(self):
        # OpenCode Zen serves its free models with no stored credential.
        inbox, result = self.api_lane("zen", LOOM_ZEN_MODEL="opencode/minimax-m3.1-flash-free")
        self.assertEqual(result.returncode, 0, result.stderr)
        agent = [c for c in self.calls() if c.startswith("opencode ")]
        self.assertEqual(len(agent), 1, self.calls())
        self.assertIn("-m opencode/minimax-m3.1-flash-free", agent[0])
        self.assertIn("keyvar=none", agent[0])
        self.assertIn("secret=denied", agent[0])
        self.assertIn("small=opencode/minimax-m3.1-flash-free", agent[0])
        self.assertNotIn(f"home={self.home} ", agent[0])
        self.assertIn("lane zen, model opencode/minimax-m3.1-flash-free", self.body.read_text())
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
            "zen", LOOM_ZEN_MODEL="opencode/minimax-m3.1-flash-free", FAKE_GH_PRIVATE="true")
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
            "zen", LOOM_ZEN_MODEL="opencode/minimax-m3.1-flash-free", FAKE_GH_API_FAIL="1")
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_opencode_go_other_models_still_take_a_private_repo(self):
        inbox, result = self.api_lane(
            "opencode-go", LOOM_OPENCODE_GO_KEY_SSM="/k/go", FAKE_SECRET__k_go="fake-go-key",
            LOOM_OPENCODE_GO_MODEL="opencode-go/kimi-k2.7-code", FAKE_GH_PRIVATE="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith("opencode ") for c in self.calls()), self.calls())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())

    def slot_run(self, slot, **env):
        inbox, result = self.api_lane(
            slot, LOOM_OPENCODE_GO_KEY_SSM="/k/go", FAKE_SECRET__k_go="fake-go-key",
            LOOM_OPENCODE_GO_MODEL="opencode-go/kimi-k2.7-code",
            LOOM_OPENCODE_GO_3_MODEL="opencode-go/deepseek-v4.1-flash", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())
        return [c for c in self.calls() if c.startswith("opencode ")][-1]

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_slot_with_its_own_model_setting_runs_that_model(self):
        self.assertIn("-m opencode-go/deepseek-v4.1-flash", self.slot_run("opencode-go-3"))

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_slot_without_one_runs_its_lanes_model(self):
        self.assertIn("-m opencode-go/kimi-k2.7-code", self.slot_run("opencode-go-2"))

    def opencode_2(self):
        """A 2.0 binary outside PATH, as one slot's LOOM_<SLOT>_OPENCODE names it."""
        path = Path(self._tmp.name) / "opencode2" / "bin" / "opencode"
        self.write_tool(path, FAKE_OPENCODE_2)
        return str(path)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_slot_on_opencode_2_uses_one_sandboxed_server(self):
        profile_file = Path(self._tmp.name) / "opencode2.profile"
        self.write_tool(self.bin / "sandbox-exec", "#!/bin/sh\nprintf '%s' \"$2\" > "
                        + shlex.quote(str(profile_file)) + "\nexec /usr/bin/sandbox-exec \"$@\"\n")
        run = self.slot_run("opencode-go-3", LOOM_OPENCODE_GO_3_OPENCODE=self.opencode_2())
        port = int(re.search(r"run --server http://127\.0\.0\.1:(\d+)", run).group(1))
        self.assertGreaterEqual(port, 49152)
        self.assertLessEqual(port, 65535)
        self.assertIn(f"serve --hostname 127.0.0.1 --port {port} password=set", self.calls())
        self.assertIn(f"run --server http://127.0.0.1:{port} --auto -m opencode-go/deepseek-v4.1-flash", run)
        self.assertNotIn("--standalone", run)
        self.assertNotIn("run --port", run)
        self.assertIn("server=reachable password=set", self.calls())
        profile = profile_file.read_text()
        self.assertIn('(deny network-outbound (remote ip "localhost:*"))', profile)
        self.assertEqual(re.findall(r'\(allow network-outbound \(remote ip "localhost:(\d+)"\)\)', profile),
                         [str(port)])
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
        self.assertIn("secret=denied", run)
        self.assertIn("lo=denied", run)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_opencode_2_stops_its_server_after_agent_failure(self):
        inbox, result = self.api_lane(
            "opencode-go-3", extra="AGENT_FAIL=1\n",
            LOOM_OPENCODE_GO_KEY_SSM="/k/go", FAKE_SECRET__k_go="fake-go-key",
            LOOM_OPENCODE_GO_3_MODEL="opencode-go/deepseek-v4.1-flash",
            LOOM_OPENCODE_GO_3_OPENCODE=self.opencode_2())
        self.assertEqual(result.returncode, 0, result.stderr)
        run = next(c for c in self.calls() if c.startswith("opencode run --server"))
        port = int(re.search(r"http://127\.0\.0\.1:(\d+)", run).group(1))
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(os.environ.get("LOOM_TEST_OPENCODE2") and shutil.which("sandbox-exec"),
                         "set LOOM_TEST_OPENCODE2 to a real opencode 2 binary on macOS")
    def test_real_opencode_2_reaches_model_step_inside_sandbox(self):
        binary = os.environ["LOOM_TEST_OPENCODE2"]
        self.assertTrue(subprocess.check_output([binary, "--version"], text=True).startswith("opencode v2."))
        inbox, result = self.api_lane(
            "opencode-go-3", LOOM_OPENCODE_GO_KEY_SSM="/k/go",
            FAKE_SECRET__k_go="fake-go-key",
            LOOM_OPENCODE_GO_3_MODEL="opencode-go/nonexistent-model-for-test",
            LOOM_OPENCODE_GO_3_OPENCODE=binary, LOOM_AGENT_TIMEOUT="30")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.loom / "logs/opencode-go-3/T042-fix-the-thing.log").read_text()
        self.assertRegex(log.lower(), r"model.*(not found|unknown|invalid)|(?:not found|unknown|invalid).*model")
        self.assertNotIn("Transport: Was there a typo in the url or port?", log)
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_sibling_slot_keeps_the_default_opencode(self):
        run = self.slot_run("opencode-go-2", LOOM_OPENCODE_GO_3_OPENCODE=self.opencode_2())
        self.assertIn("run -m opencode-go/kimi-k2.7-code", run)
        self.assertNotIn("--standalone", run)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_spark_lane_runs_opencode_against_its_endpoint_and_takes_a_private_repo(self):
        # Visibility is checked for scanning; private repos skip the scan.
        inbox, result = self.api_lane("spark", FAKE_GH_PRIVATE="true", **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("gh api repos/example/repo --jq .private", calls)
        self.assertNotIn("leak guard: clean (shape patterns only)", self.spark_log())
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
        self.assertEqual(provider["models"]["coder-test"]["limit"], {"context": 32768, "output": 4096})
        self.assertIn("lane spark, model spark/coder-test", self.body.read_text())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())
        self.assertTrue(self.spark_beat()["available"])

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_the_spark_2_slot_runs_against_its_own_box(self):
        # The second Spark box (2026-10-14: one sat idle with the coder warm).
        inbox = self.loom / "briefs" / "spark-2"
        inbox.mkdir(parents=True)
        (inbox / "T042-fix-the-thing.md").write_text(BRIEF.replace("Lane: codex", "Lane: spark-2"))
        result = self.run_lane("spark-2", LOOM_SPARK_2_URL="http://other-spark.example.invalid:8000/v1",
                               **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any(c.startswith("curl ") and "http://other-spark.example.invalid:8000/v1/models" in c
                            for c in calls), calls)
        self.assertFalse(any("spark.example.invalid:8000" in c and "other-spark" not in c
                             for c in calls if c.startswith("curl ")), calls)
        config = json.loads(next(c for c in calls if c.startswith("config="))[len("config="):])
        self.assertEqual(config["provider"]["spark"]["options"],
                         {"baseURL": "http://other-spark.example.invalid:8000/v1"})

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_spark_lane_uses_the_window_its_endpoint_serves(self):
        # The first sparkles run thrashed in a fixed 32K window against a
        # server serving 262144; the window is a lane setting now.
        _, result = self.api_lane("spark", LOOM_SPARK_CONTEXT="131072", LOOM_SPARK_OUTPUT="16384",
                                  **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(next(c for c in self.calls() if c.startswith("config="))[len("config="):])
        self.assertEqual(config["provider"]["spark"]["models"]["coder-test"]["limit"],
                         {"context": 131072, "output": 16384})

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_free_provider_slot_is_its_own_lane_with_its_own_window(self):
        # The free lane (infra#5617): one slot per free provider at the
        # spark-gateway's pinned route, declared as provider "free" (never
        # "spark"), with the ~128K window the free models serve beside a
        # Spark window of 262144.
        _, result = self.api_lane("free-ollama",
                                  LOOM_FREE_OLLAMA_URL="http://gateway.example.invalid/free/ollama-cloud/v1",
                                  LOOM_FREE_OLLAMA_MODEL="free/ollama-cloud",
                                  LOOM_FREE_CONTEXT="120000", LOOM_FREE_OUTPUT="16384",
                                  LOOM_SPARK_CONTEXT="262144", LOOM_SPARK_OUTPUT="32768",
                                  **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any(c.startswith("curl ") and "/free/ollama-cloud/v1/models" in c for c in calls), calls)
        config = json.loads(next(c for c in calls if c.startswith("config="))[len("config="):])
        self.assertNotIn("spark", config["provider"])
        self.assertEqual(config["model"], "free/ollama-cloud")
        self.assertEqual(config["provider"]["free"]["options"],
                         {"baseURL": "http://gateway.example.invalid/free/ollama-cloud/v1"})
        self.assertEqual(config["provider"]["free"]["models"]["ollama-cloud"]["limit"],
                         {"context": 120000, "output": 16384})

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_spark_window_that_is_not_a_number_falls_back_to_the_defaults(self):
        _, result = self.api_lane("spark", LOOM_SPARK_CONTEXT='1", "x": "', LOOM_SPARK_OUTPUT="lots",
                                  **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(next(c for c in self.calls() if c.startswith("config="))[len("config="):])
        self.assertEqual(config["provider"]["spark"]["models"]["coder-test"]["limit"],
                         {"context": 32768, "output": 4096})

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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
        self.assertIn("failed: [limit] limit: the spark endpoint refused the connection", self.spark_log())
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_spark_run_cut_off_by_a_503_is_a_limit_not_a_failure(self):
        inbox, result = self.api_lane("spark", extra="AGENT_FAIL=1\n", FAKE_CURL="200 503", **self.SPARK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith("opencode ") for c in self.calls()), self.calls())
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: [limit] limit: the spark endpoint answered 503 (agent exited 3)", self.spark_log())
        beat = self.spark_beat()
        self.assertFalse(beat["available"])
        self.assertTrue(beat["reason"].startswith("limit: "), beat)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_spark_run_that_fails_while_the_endpoint_answers_is_an_ordinary_failure(self):
        inbox, _ = self.api_lane("spark", extra="AGENT_FAIL=1\n", **self.SPARK)
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: [transient] agent exited 3", self.spark_log())
        self.assertNotIn("limit:", self.spark_log())
        self.assertTrue(self.spark_beat()["available"])
        self.assertFalse((self.loom / "state" / "usage" / "spark.limit").exists())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "API lanes run under macOS sandbox-exec")
    def test_spark_lane_refuses_a_model_from_elsewhere(self):
        inbox, _ = self.api_lane("spark", LOOM_SPARK_URL=self.SPARK["LOOM_SPARK_URL"],
                                 LOOM_SPARK_MODEL="opencode/minimax-m3.1-flash-free")
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

    GO_LIMIT = (429, '{"type": "GoUsageLimitError", "limitName": "weekly", '
                     '"message": "Weekly usage limit reached"}')

    def provider(self, *answers, **kw):
        """A fake OpenCode Go chat endpoint on loopback. Answers each POST with
        the next (status, body) of answers (the last repeats) and records the
        request; real curl runs behind a wrapper that records its argv.
        retry_after sends the header a provider sends with a refusal."""
        requests, queue = [], list(answers)
        retry_after = kw.get("retry_after")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requests.append({"path": self.path, "headers": dict(self.headers),
                                 "body": json.loads(body)})
                status, text = queue.pop(0) if len(queue) > 1 else queue[0]
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if retry_after:
                    self.send_header("Retry-After", str(retry_after))
                self.end_headers()
                self.wfile.write(text.encode())

            def log_message(self, format, *args):
                pass

        server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.write_tool(self.bin / "curl", '#!/usr/bin/env bash\nprintf \'curl %s\\n\' "$*" >> "$FAKE_LOG"\n'
                        'exec /usr/bin/curl "$@"\n')
        return requests, {
            "LOOM_OPENCODE_GO_KEY_SSM": "/k/go", "FAKE_SECRET__k_go": "fake-go-key-s3cr3t",
            "LOOM_OPENCODE_GO_MODEL": "opencode-go/kimi-k2.7-code",
            "LOOM_OPENCODE_GO_PROBE_URL": "http://127.0.0.1:%d/zen/go/v1/chat/completions" % server.server_port,
            "LOOM_OPENCODE_GO_PROBE_HEADER": "x-opencode-session"}

    def go_log(self):
        return (self.loom / "logs" / "opencode-go" / "T042-fix-the-thing.log").read_text()

    def assert_key_kept_out(self, result):
        """The key reached the provider's header, never an argv, the lane log or output."""
        for where in (self.go_log(), result.stdout, result.stderr, *self.calls()):
            if where.startswith("opencode "):
                continue  # the agent's own record of the key it was handed
            self.assertNotIn("fake-go-key-s3cr3t", where)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_go_usage_limit_on_one_slot_holds_the_whole_group_until_the_reset(self):
        requests, env = self.provider(self.GO_LIMIT)
        before = int(time.time())
        inbox, result = self.api_lane("opencode-go", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()), self.calls())
        self.assertFalse((self.loom / "worktrees" / "T042-fix-the-thing").exists())
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: [limit] limit: the provider's weekly limit (429 GoUsageLimitError)", self.go_log())
        beat = self.beat("opencode-go")
        self.assertFalse(beat["available"])
        self.assertTrue(beat["reason"].startswith("limit: "), beat)
        self.assertIn("weekly", beat["reason"])
        # A weekly limit with no reset time holds the group until the next weekly
        # reset, never the short default window.
        self.assertTrue(before + 7 * 86400 <= beat["retry_at"] <= int(time.time()) + 7 * 86400)
        self.assertGreater(beat["retry_at"], before + 43200)
        for slot in ("opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4"):
            beat = self.beat(slot)
            self.assertFalse(beat["available"], slot)
            self.assertTrue(beat["reason"].startswith("limit: "), beat)
            self.assertIn("weekly", beat["reason"])
            self.assertEqual(beat["retry_at"], self.beat("opencode-go")["retry_at"], slot)
        # One small request for the run's model, keyed and with a session header.
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/zen/go/v1/chat/completions")
        self.assertEqual(requests[0]["body"]["model"], "kimi-k2.7-code")
        self.assertEqual(requests[0]["body"]["max_tokens"], 1)
        self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer fake-go-key-s3cr3t")
        self.assertTrue(requests[0]["headers"].get("x-opencode-session"))
        self.assert_key_kept_out(result)
        # The next pass asks nothing and keeps saying which limit holds, for every slot.
        (inbox / "T043-next.md").write_text(BRIEF.replace("Lane: codex", "Lane: opencode-go"))
        self.run_lane("opencode-go", **env)
        self.assertEqual(len(requests), 1)
        self.assertTrue((inbox / "T043-next.md").exists())
        for slot in ("opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4"):
            self.assertIn("weekly", self.beat(slot)["reason"])
            self.assertTrue(self.beat(slot)["reason"].startswith("limit: "))

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_go_limit_holds_the_group_for_the_reset_the_provider_gave(self):
        # 2026-10-20: the provider's Retry-After header was never read (the awk
        # that read it wanted IGNORECASE, which the awk on the lane machine has
        # no), so every OpenCode Go limit held the four opencode-go slots for a
        # week: the lane-held invariant fired and the tier's tickets waited days
        # for a lane the provider served again hours later.
        requests, env = self.provider(self.GO_LIMIT, retry_after=1800)
        before = int(time.time())
        inbox, result = self.api_lane("opencode-go", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        beats = {slot: self.beat(slot) for slot in
                 ("opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4")}
        for slot, beat in beats.items():
            self.assertFalse(beat["available"], slot)
            self.assertTrue(beat["reason"].startswith("limit: "), beat)
        until = beats["opencode-go"]["retry_at"]
        self.assertGreaterEqual(until, before + 1800)
        self.assertLessEqual(until, before + 1800 + 300)
        for slot, beat in beats.items():
            self.assertEqual(beat["retry_at"], until, slot)

    def test_a_model_with_a_quote_never_reaches_the_probe_body(self):
        # The probe checks the model itself, not only through the brief loop's allowlist.
        sent = Path(self._tmp.name) / "probe-sent"
        script = self.source_functions("provider_at_limit") + (
            'lane_setting() { case "$1" in PROBE_URL) echo http://127.0.0.1:9/v1/chat ;; esac; }\n'
            'curl() { cat > /dev/null; printf "%s\\n" "$*" >> "$SENT"; printf "\\n200"; }\n'
            'SLOT=opencode-go API_KEY=k PROBE_TIMEOUT=1 MODEL="$1"\nprovider_at_limit; echo "rc=$?"')
        for model, reaches in (('opencode-go/kimi", "max_tokens": 99999, "x": "', False),
                               ("opencode-go/kimi\\", False), ("opencode-go/kimi-k2.7-code", True)):
            sent.unlink(missing_ok=True)
            result = subprocess.run(["bash", "-c", script, "bash", model], env={**self.lane_env(), "SENT": str(sent)},
                                    capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "rc=1", model)
            self.assertEqual(sent.exists(), reaches, model)

    def write_provider_limit(self, slot, limit_name="weekly", retry_after=None):
        usage = self.loom / "state" / "usage"
        usage.mkdir(parents=True)
        (usage / (slot + ".provider-limit.json")).write_text(json.dumps({
            "at": int(time.time()), "limit_name": limit_name,
            "retry_after": retry_after if retry_after is not None else ""}) + "\n")

    # The provider the probe asks: the headers it sent, the body it answered
    # and the status, read from the environment.
    FAKE_CURL_PROVIDER = """#!/usr/bin/env bash
h=""
while [ $# -gt 0 ]; do case "$1" in -D) shift; h="$1";; esac; shift; done
if [ "$FAKE_STATUS" = refused ]; then printf '000'; exit 7; fi
[ -n "$h" ] && printf 'HTTP/1.1 %s\\r\\n%s\\r\\n\\r\\n' "$FAKE_STATUS" "$FAKE_HEADERS" > "$h"
printf '%s\\n%s' "$FAKE_BODY" "$FAKE_STATUS"
"""

    def fake_provider(self, status=200, headers="", body='{"ok": true}'):
        """A curl that answers the probe the way the environment says."""
        self.write_tool(self.bin / "curl", self.FAKE_CURL_PROVIDER)
        return {"FAKE_STATUS": str(status), "FAKE_HEADERS": headers, "FAKE_BODY": body}

    def provider_probe(self, **env):
        """Run the runner's probe in a shell of its own and say what it found."""
        script = self.source_functions("provider_at_limit", "provider_checked") + (
            'lane_setting() { case "$1" in PROBE_URL) echo http://127.0.0.1:9/v1/chat ;; esac; }\n'
            'SLOT=opencode-go API_KEY=k PROBE_TIMEOUT=1 MODEL="opencode-go/kimi-k2.7-code"\n'
            'provider_at_limit; echo "rc=$?"')
        return subprocess.run(["bash", "-c", script, "bash"],
                              env={**self.lane_env(), "LOOM_HOME": str(self.loom), **env},
                              capture_output=True, text=True)

    def test_the_providers_retry_after_is_the_time_the_lane_waits(self):
        # 2026-10-20: the awk that read the provider's Retry-After wanted
        # IGNORECASE, which the awk on the lane machine has no, so the header
        # was never read: every OpenCode Go limit fell back to the window name
        # and held the four opencode-go slots for a week, and the tier's
        # tickets waited days for a lane the provider served again hours later.
        env = self.fake_provider(status=429, headers="Retry-After: 1800",
                                 body='{"type": "GoUsageLimitError", "limitName": "monthly"}')
        before = int(time.time())
        result = self.provider_probe(**env)
        self.assertEqual(result.stdout.strip(), "rc=0", result.stderr)
        row = json.loads((self.loom / "state/usage/opencode-go.provider-limit.json").read_text())
        self.assertEqual(row["limit_name"], "monthly")
        self.assertGreaterEqual(int(row["retry_after"]), before + 1800)
        self.assertLessEqual(int(row["retry_after"]), before + 1800 + 60)

    def provider_lift(self, slot="opencode-go", **env):
        """Run the runner's re-check in a shell of its own and say what it did."""
        script = self.source_functions(
            "provider_at_limit", "provider_checked", "provider_limit_lifted",
            "provider_recheck_due", "lift_provider_limit", "hold_provider_limit",
            "limit_until", "key_group", "write_beat", "limited_models_json", "utc",
            "setting_var", "lane_var", "lane_setting") + (
            'ssm_value() { printf "%s" "$FAKE_KEY"; }\n'
            'LIMIT_RECHECK=1800 GO_LIMIT_ERROR=GoUsageLimitError\n'
            'SLOT=' + slot + ' LANE=opencode-go '
            'BEAT="$LOOM_HOME/state/usage/$SLOT.json"\n'
            'LIMIT_WHY="the provider monthly limit (429 GoUsageLimitError)"\n'
            'provider_limit_lifted; echo "rc=$?"')
        return subprocess.run(["bash", "-c", script, "bash"],
                              env={**self.lane_env(), "LOOM_HOME": str(self.loom),
                                   "SLOT": slot, "LANE": "opencode-go",
                                   "LOOM_OPENCODE_GO_MODEL": "opencode-go/kimi-k2.7-code",
                                   "LOOM_OPENCODE_GO_PROBE_URL": "http://127.0.0.1:9/v1/chat",
                                   "LOOM_OPENCODE_GO_KEY_SSM": "/k/go",
                                   "FAKE_KEY": "fake-go-key", **env},
                              capture_output=True, text=True)

    def hold_the_group(self, checked_at):
        """The group held by a provider limit, as the runner leaves it."""
        usage = self.loom / "state" / "usage"
        usage.mkdir(parents=True)
        until = int(time.time()) + 1800
        for slot in self.GROUP:
            (usage / (slot + ".provider-limit.json")).write_text(json.dumps(
                {"at": int(time.time()), "limit_name": "monthly",
                 "retry_after": str(until), "model": "kimi-k2.7-code"}) + "\n")
            (usage / (slot + ".provider-check")).write_text("%d\n" % checked_at)
            (usage / (slot + ".json")).write_text(json.dumps(
                {"lane": slot, "at": int(time.time()), "available": False,
                 "reason": "limit: the provider monthly limit (429 GoUsageLimitError)",
                 "retry_at": until}) + "\n")

    def test_a_lane_held_by_a_limit_asks_the_provider_again_and_comes_back(self):
        # The provider's allowance refills while the lane sleeps, so the lane
        # asks it again instead of waiting out the whole window (opencode-go,
        # 2026-10-20: the lane sat idle for days and the lane-held invariant
        # fired while the tier's tickets waited).
        self.hold_the_group(int(time.time()) - 1801)
        env = self.fake_provider(status=200)
        result = self.provider_lift(**env)
        self.assertEqual(result.stdout.strip(), "rc=0", result.stderr)
        for slot in self.GROUP:
            beat = json.loads((self.loom / "state/usage" / (slot + ".json")).read_text())
            self.assertTrue(beat["available"], slot)
            self.assertEqual(beat["reason"], "", slot)
        # The record of the refusal stays: it is the last 429 the Usage view shows.
        self.assertTrue((self.loom / "state/usage/opencode-go.provider-limit.json").exists())

    def test_a_lane_asks_the_provider_again_only_after_the_recheck_window(self):
        self.hold_the_group(int(time.time()) - 60)
        env = self.fake_provider(status=200)
        result = self.provider_lift(**env)
        self.assertEqual(result.stdout.strip(), "rc=1", result.stderr)
        beat = json.loads((self.loom / "state/usage/opencode-go.json").read_text())
        self.assertFalse(beat["available"])

    def test_a_provider_that_still_refuses_keeps_the_hold(self):
        self.hold_the_group(int(time.time()) - 1801)
        env = self.fake_provider(status=429, headers="Retry-After: 1800",
                                 body='{"type": "GoUsageLimitError", "limitName": "monthly"}')
        result = self.provider_lift(**env)
        self.assertEqual(result.stdout.strip(), "rc=1", result.stderr)
        for slot in self.GROUP:
            beat = json.loads((self.loom / "state/usage" / (slot + ".json")).read_text())
            self.assertFalse(beat["available"], slot)
            self.assertTrue(beat["reason"].startswith("limit: "), beat)

    def test_a_provider_that_cannot_be_reached_keeps_the_hold(self):
        # A provider that does not answer is not a provider that serves the
        # lane: the hold stands, so the lane does not claim work it cannot run.
        self.hold_the_group(int(time.time()) - 1801)
        env = self.fake_provider(status="refused")
        result = self.provider_lift(**env)
        self.assertEqual(result.stdout.strip(), "rc=1", result.stderr)
        beat = json.loads((self.loom / "state/usage/opencode-go.json").read_text())
        self.assertFalse(beat["available"])

    def test_a_hold_the_agent_cli_recorded_is_not_lifted_by_the_probe(self):
        # The agent CLI's own hold (a model it no longer offers) carries no
        # provider limit: no probe says otherwise, so it stands.
        self.hold_the_group(int(time.time()) - 1801)
        beat = json.loads((self.loom / "state/usage/opencode-go.json").read_text())
        beat["reason"] = "limit: model opencode-go/kimi-k2.7-code not found by the agent CLI"
        (self.loom / "state/usage/opencode-go.json").write_text(json.dumps(beat))
        env = self.fake_provider(status=200)
        result = self.provider_lift(**env)
        self.assertEqual(result.stdout.strip(), "rc=1", result.stderr)
        beat = json.loads((self.loom / "state/usage/opencode-go.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("not found by the agent CLI", beat["reason"])

    def hold_provider_limit_for(self, slot="opencode-go-2", limit_name="weekly", retry_after=None):
        self.write_provider_limit(slot, limit_name, retry_after)
        script = self.source_functions("hold_provider_limit", "limit_until", "key_group",
                                       "write_beat", "utc") + (
            'SLOT=%s LANE=opencode-go LIMIT_WHY="the provider weekly limit (429 GoUsageLimitError)"\n'
            'hold_provider_limit\n' % slot)
        env = {**self.lane_env(), "LOOM_HOME": str(self.loom), "SLOT": slot, "LANE": "opencode-go"}
        return subprocess.run(["bash", "-c", script, "bash"], env=env,
                              capture_output=True, text=True)

    GROUP = ["opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4"]

    def test_a_weekly_limit_on_one_group_slot_holds_every_group_slot_until_the_stated_reset(self):
        # Acceptance: a weekly-limit failure on one lane of a group marks every
        # lane of that group unavailable until the stated reset.
        reset = int(time.time()) + 7200
        self.hold_provider_limit_for("opencode-go-2", limit_name="weekly", retry_after=str(reset))
        beats = {k: v for k, v in self.beats().items() if "." not in k}
        self.assertEqual(sorted(beats), sorted(self.GROUP))
        for slot, beat in beats.items():
            self.assertFalse(beat["available"], slot)
            self.assertTrue(beat["reason"].startswith("limit: "), beat)
            self.assertIn("weekly", beat["reason"])
            self.assertEqual(beat["retry_at"], reset, slot)

    def test_a_weekly_limit_with_no_reset_time_holds_the_group_until_the_next_weekly_reset(self):
        # Acceptance: a weekly-limit error with no reset time holds the group
        # until the next weekly reset, not for the short default.
        before = int(time.time())
        self.hold_provider_limit_for("opencode-go-2", limit_name="weekly", retry_after=None)
        beats = {k: v for k, v in self.beats().items() if "." not in k}
        self.assertEqual(sorted(beats), sorted(self.GROUP))
        now = int(time.time())
        for slot, beat in beats.items():
            self.assertFalse(beat["available"], slot)
            self.assertTrue(beat["reason"].startswith("limit: "), beat)
            self.assertIn("weekly", beat["reason"])
            self.assertTrue(before + 7 * 86400 <= beat["retry_at"] <= now + 7 * 86400, slot)
            self.assertGreater(beat["retry_at"], before + 43200, slot)

    # A sandbox that runs what it is given: the seatbelt itself is another
    # lane's subject (tests above), and this run is about the class an expired
    # key gets and the heartbeat that follows.
    FAKE_SANDBOX = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in -p) shift 2 ;; *) break ;; esac
done
exec "$@"
"""

    # An opencode whose provider refuses the key it was handed: the message the
    # OpenCode Go lanes gave on 2026-10-18. A renewed key runs the work instead.
    FAKE_OPENCODE_EXPIRED = """#!/usr/bin/env bash
[ "${1:-}" = --version ] && { echo "1.18.4"; exit 0; }
if [ "${OPENCODE_API_KEY:-}" = expired-key ]; then
  echo "Error: This API key has expired. Create a new API key in OpenCode Console."
  exit 1
fi
echo fixed > fix.txt
"""

    def go_key_env(self, key="expired-key"):
        return {"LOOM_OPENCODE_GO_KEY_SSM": "/k/go", "FAKE_SECRET__k_go": key,
                "LOOM_OPENCODE_GO_MODEL": "opencode-go/kimi-k2.7-code"}

    def expired_key_lane(self, key="expired-key", **env):
        """The opencode-go lane with a provider that refuses its key."""
        self.write_tool(self.bin / "sandbox-exec", self.FAKE_SANDBOX)
        self.write_tool(self.bin / "opencode", self.FAKE_OPENCODE_EXPIRED)
        return self.api_lane("opencode-go", **self.go_key_env(key), **env)

    def go_brief(self, name="T043-next.md"):
        self.write_tool(self.bin / "sandbox-exec", self.FAKE_SANDBOX)
        self.write_tool(self.bin / "opencode", self.FAKE_OPENCODE_EXPIRED)
        inbox = self.loom / "briefs" / "opencode-go"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / name).write_text(BRIEF.replace("T042-fix-the-thing", name[:-3])
                                  .replace("Lane: codex", "Lane: opencode-go"))
        return inbox

    def test_an_expired_key_is_auth_and_takes_every_lane_that_bills_it_offline(self):
        # Acceptance (loom#744): the expired-key error is [auth], not
        # [transient], and every slot billing that key is unavailable with the
        # reason naming the parameter to renew.
        inbox, result = self.expired_key_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("failed: [auth] agent exited 1: API key expired: renew /k/go", self.go_log())
        self.assertNotIn("transient", self.go_log())
        for slot in self.GROUP:
            beat = self.beat(slot)
            self.assertFalse(beat["available"], slot)
            self.assertEqual(beat["reason"], "API key expired: renew /k/go", slot)
            # No retry time: only a renewed key lifts this hold.
            self.assertNotIn("retry_at", beat, slot)
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertNotIn("expired-key", self.go_log())

    def test_a_lane_held_by_an_expired_key_takes_no_brief_until_the_key_is_renewed(self):
        self.expired_key_lane()
        inbox = self.go_brief()
        self.log.write_text("")
        result = self.run_lane("opencode-go", **self.go_key_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        # No run, and the waiting brief is handed on rather than left to fail.
        self.assertFalse(any(c.startswith("opencode ") for c in self.calls()))
        self.assertFalse((inbox / "T043-next.md").exists())
        self.assertIn("failed: [auth] API key expired: renew /k/go",
                      (self.loom / "logs" / "opencode-go" / "T043-next.log").read_text())
        self.assertFalse(self.beat("opencode-go")["available"])
        # The operator renews the key: the lane is back, and its digest is gone
        # so the next expiry is this key's, not the old one's.
        self.go_brief("T044-next.md")
        result = self.run_lane("opencode-go", **self.go_key_env("a-renewed-key"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((inbox / "done" / "T044-next.md").exists())
        self.assertTrue(self.beat("opencode-go")["available"])
        usage = self.loom / "state" / "usage"
        self.assertFalse((usage / "opencode-go.key-sha").exists())

    def test_an_expired_key_opens_one_needs_operator_ask_naming_the_parameter(self):
        self.expired_key_lane()
        filed = [c for c in self.calls() if c.startswith("gh issue create")]
        self.assertEqual(len(filed), 1, self.calls())
        self.assertIn("--label needs-operator", filed[0])
        self.assertIn("renew /k/go", filed[0])
        # The next lane to hit that key finds the open ask and comments on it,
        # so the operator has one thread to answer, not one per lane.
        script = ('FACTORY_REPO=quadseven/loom KEY_ASK_MARKER="<!-- loom:key-ask v1 -->"\n'
                  'SLOT=opencode-go-2 LOG=/dev/null\n'
                  + self.source_functions("open_issue", "ask_renew_key") +
                  'ask_renew_key "API key expired: renew /k/go"\n')
        result = subprocess.run(["bash", "-c", script, "bash"],
                                env={**self.lane_env(), "FAKE_GH_SEARCH": "7"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith("gh issue comment") for c in self.calls()))
        self.assertEqual(len([c for c in self.calls() if c.startswith("gh issue create")]), 1)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_provider_that_answers_lets_the_run_go_ahead(self):
        requests, env = self.provider((200, '{"choices": [{"message": {"content": "p"}}]}'))
        inbox, result = self.api_lane("opencode-go", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 1)
        self.assertTrue(any(c.startswith("opencode ") for c in self.calls()), self.calls())
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())
        self.assertTrue(self.beat("opencode-go")["available"])
        self.assert_key_kept_out(result)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_429_that_is_not_a_go_usage_limit_lets_the_run_go_ahead(self):
        _, env = self.provider((429, '{"type": "RateLimitError"}'))
        inbox, _ = self.api_lane("opencode-go", **env)
        self.assertTrue((inbox / "done" / "T042-fix-the-thing.md").exists())
        self.assertTrue(self.beat("opencode-go")["available"])

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_timed_out_run_then_a_go_usage_limit_parks_as_a_limit(self):
        requests, env = self.provider((200, "{}"), self.GO_LIMIT)
        before = int(time.time())
        inbox, result = self.api_lane("opencode-go", extra="AGENT_TIMEOUT=1\n", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 2)
        self.assertTrue(any(c.startswith("opencode ") for c in self.calls()), self.calls())
        self.assertTrue((inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("failed: [limit] limit: the provider's weekly limit (429 GoUsageLimitError) "
                      "(agent exited 124)", self.go_log())
        self.assertNotIn("[transient]", self.go_log())
        beat = self.beat("opencode-go")
        self.assertFalse(beat["available"])
        self.assertIn("weekly", beat["reason"])
        # A weekly limit with no reset time holds the group until the next weekly
        # reset, never the short default window.
        self.assertTrue(before + 7 * 86400 <= beat["retry_at"] <= int(time.time()) + 7 * 86400)
        self.assertGreater(beat["retry_at"], before + 43200)
        for slot in ("opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4"):
            self.assertFalse(self.beat(slot)["available"], slot)
            self.assertEqual(self.beat(slot)["retry_at"], beat["retry_at"], slot)
        self.assert_key_kept_out(result)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_timed_out_run_while_the_provider_answers_stays_transient(self):
        requests, env = self.provider((200, "{}"))
        inbox, _ = self.api_lane("opencode-go", extra="AGENT_TIMEOUT=1\n", **env)
        self.assertEqual(len(requests), 2)
        self.assertIn("failed: [transient] agent exited 124", self.go_log())
        self.assertTrue(self.beat("opencode-go")["available"])

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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
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

    def failure_line(self, ticket="T042-fix-the-thing", lane="codex"):
        lines = [l for l in (self.loom / "logs" / lane / (ticket + ".log")).read_text().splitlines()
                 if " failed: " in l]
        self.assertEqual(len(lines), 1, lines)
        return lines[0].split(" failed: ", 1)[1]

    def beat(self, lane="codex"):
        return json.loads((self.loom / "state" / "usage" / (lane + ".json")).read_text())

    # (agent output, exit status, edited the tree, expected class)
    SAMPLES = [
        ("Claude AI usage limit reached|1999999999", 1, False, "limit"),
        ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}', 1, True, "limit"),
        ("Error: You exceeded your current quota, please check your plan", 1, True, "limit"),
        ("ProviderError: HTTP 402 Payment Required", 1, False, "limit"),
        ("Rate limit hit. Please try again in 20 minutes.", 0, False, "limit"),
        ("TypeError: fetch failed (ECONNRESET)", 1, True, "transient"),
        ('API Error: 529 {"type":"overloaded_error"}', 1, True, "transient"),
        ("Error: This API key has expired. Create a new API key in OpenCode Console",
         1, True, "auth"),
        ("AuthenticationError: HTTP 401 Unauthorized", 1, True, "auth"),
        ("panic: something broke before any edit", 1, False, "transient"),
        ("killed", 137, True, "transient"),
        ('level=ERROR message=failed error="ProviderModelNotFoundError: Model not found: '
         'openrouter/vendor/gone:free. Did you mean: vendor/other:free?"\n'
         'Error: {"name": "UnknownError", "message": "Unexpected server error."}', 1, False, "limit"),
        ("FAILED tests/test_x.py::test_y - AssertionError at line 429", 1, True, "work"),
        ("I looked and there is nothing to change.", 0, False, "work"),
    ]

    def test_each_failure_is_classed_from_the_agents_exit_and_output(self):
        for out, status, edited, want in self.SAMPLES:
            with self.subTest(out=out):
                for d in ("logs", "state", "briefs"):
                    shutil.rmtree(self.loom / d, ignore_errors=True)
                self.inbox.mkdir(parents=True)
                self.drop_brief()
                env = {"FAKE_AGENT": "say", "FAKE_OUT": out, "FAKE_EXIT": str(status)}
                if edited:
                    env["FAKE_EDIT"] = "1"
                result = self.run_lane(**env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
                self.assertTrue(self.failure_line().startswith("[%s] " % want), self.failure_line())
                # A limit takes the lane offline; an expired key does too on a
                # lane that reads its key from a parameter, and codex, which
                # reads no key here, keeps its heartbeat as it is.
                self.assertEqual(self.beat()["available"], want != "limit")

    def app_token_cmd(self, script):
        cmd = Path(self._tmp.name) / "app-token"
        self.write_tool(cmd, "#!/usr/bin/env bash\n" + script)
        return str(cmd)

    def test_a_lane_with_its_app_settings_writes_as_the_app_and_the_agent_never_sees_the_token(self):
        # loom#1142: lanes opened every pull request as the operator.
        seen_gh = Path(self._tmp.name) / "gh-tokens"
        seen_agent = Path(self._tmp.name) / "agent-tokens"
        self.drop_brief()
        self.run_lane(LOOM_GITHUB_APP_ID="124",
                      # Installation tokens carry dots and dashes (2026-10-20: 383
                      # characters, ghs_ then a JWT-like body).
                      LOOM_APP_TOKEN_CMD=self.app_token_cmd("echo ghs_lanes.tok-en"),
                      GH_TOKEN="ghp_personal", FAKE_GH_TOKENS=str(seen_gh),
                      FAKE_AGENT_ENV=str(seen_agent))
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()), self.calls())
        self.assertEqual(set(seen_gh.read_text().split()), {"ghs_lanes.tok-en"})
        # Nor the App's settings to mint one, nor git's helper to borrow one.
        self.assertEqual(seen_agent.read_text().split(), ["none", "none", "none"])

    def test_a_lane_whose_app_token_cannot_be_minted_posts_nothing_as_anyone(self):
        self.drop_brief()
        self.run_lane(LOOM_GITHUB_APP_ID="124",
                      LOOM_APP_TOKEN_CMD=self.app_token_cmd("echo 'key not found' >&2; exit 2"),
                      GH_TOKEN="ghp_personal")
        self.assertEqual([c for c in self.calls() if c.startswith(("gh ", "codex "))], [])
        line = self.failure_line()
        self.assertTrue(line.startswith("[transient] "), line)
        self.assertIn("could not mint the lane's GitHub App token", line)

    def test_a_lane_rests_after_three_transient_failures_in_a_row(self):
        # zen, 2026-10-19: a free model answered "Unexpected server error" to
        # every run, and the lane kept claiming tickets for hours.
        for n in range(3):
            name = "T04%d-run" % n
            self.drop_brief(name + ".md", BRIEF.replace("T042-fix-the-thing", name))
            self.run_lane(FAKE_AGENT="say", FAKE_OUT="UnknownError: Unexpected server error",
                          FAKE_EXIT="1")
            beat = self.beat()
            self.assertEqual(beat["available"], n < 2, (n, beat))
        self.assertIn("3 transient failures in a row", beat["reason"])
        self.assertTrue(beat["reason"].startswith("limit: "))
        self.assertGreater(beat["retry_at"], int(time.time()) + 1700)
        # Resting: the next pass runs no agent.
        self.drop_brief("T049-next.md", BRIEF.replace("T042-fix-the-thing", "T049-next"))
        self.log.write_text("")
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="ok")
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.beat()["available"])

    def test_a_model_the_cli_no_longer_offers_rests_the_lane_for_a_day(self):
        # openrouter, 2026-10-20: models.dev marked the lane's free model
        # deprecated, opencode dropped it and printed only "UnknownError:
        # Unexpected server error", and every run failed transient while the
        # lane rested 30 minutes at a time and claimed again.
        self.drop_brief()
        self.run_lane(FAKE_AGENT="say", FAKE_EXIT="1", FAKE_OUT=(
            'level=ERROR message=failed error="ProviderModelNotFoundError: Model not found: '
            'openrouter/vendor/gone:free."\nError: {"name": "UnknownError"}'))
        self.assertTrue(self.failure_line().startswith("[limit] "), self.failure_line())
        self.assertIn("not found by the agent CLI", self.failure_line())
        beat = self.beat()
        self.assertFalse(beat["available"])
        self.assertGreater(beat["retry_at"], int(time.time()) + 86000)

    def test_a_lane_with_no_transient_count_writes_nothing_to_its_service_log(self):
        # 2026-10-20: every pass read the missing count file with the stderr
        # redirect after the input one, so bash printed "No such file or
        # directory" first; each lane's service log held about 1000 such lines.
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="say", FAKE_OUT="ok")
        self.assertNotIn(".transient", result.stderr)
        self.assertNotIn(".transient", result.stdout)

    def test_a_run_that_is_not_transient_clears_the_transient_count(self):
        for n, (out, status) in enumerate((("Unexpected server error", "1"),
                                           ("Unexpected server error", "1"),
                                           ("Rate limit hit. Please try again in 1 minutes.", "1"),
                                           ("Unexpected server error", "1"))):
            name = "T05%d-run" % n
            self.drop_brief(name + ".md", BRIEF.replace("T042-fix-the-thing", name))
            for f in (self.loom / "state" / "usage").glob("codex.json"):
                f.unlink()
            self.run_lane(FAKE_AGENT="say", FAKE_OUT=out, FAKE_EXIT=status)
        self.assertNotIn("transient failures in a row", self.beat().get("reason", ""))

    def test_an_expired_key_on_a_lane_that_reads_no_key_is_auth_but_not_a_hold(self):
        # codex keeps its sign-in in the CLI's own file: there is no parameter
        # whose value the operator could change, so the lane is not held for
        # good, and the operator still gets the ask naming what to renew.
        self.drop_brief()
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="Invalid API key - please run /login")
        self.assertTrue(self.failure_line().startswith("[auth] "), self.failure_line())
        self.assertTrue(self.beat()["available"])
        ask = [c for c in self.calls() if c.startswith("gh issue create")]
        self.assertEqual(len(ask), 1, self.calls())
        self.assertIn("--label needs-operator", ask[0])
        self.assertIn("renew the codex lane sign-in", ask[0])

    def test_a_daily_allowance_with_no_reset_time_holds_the_lane_until_midnight_utc(self):
        # Retried every hour, a spent daily allowance failed a run each time.
        self.drop_brief()
        before = int(time.time())
        self.run_lane(FAKE_AGENT="say", FAKE_OUT=(
            'Too Many Requests: {"errors":[{"message":"AiError: you have used up your '
            'daily free allocation of 10,000 neurons","code":4006}]}'))
        after = int(time.time())
        self.assertTrue(self.failure_line().startswith("[limit] "), self.failure_line())
        beat = self.beat()
        self.assertFalse(beat["available"])
        self.assertIn(beat["retry_at"], {t - t % 86400 + 86400 for t in (before, after)})

    def test_a_limit_holds_the_lane_unavailable_until_the_providers_reset(self):
        reset = int(time.time()) + 7200
        self.drop_brief()
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="Claude AI usage limit reached|%d" % reset)
        self.assertIn("provider limit reached", self.failure_line())
        beat = self.beat()
        self.assertFalse(beat["available"])
        self.assertEqual(beat["retry_at"], reset)
        # Later passes take no brief and read no key until the reset.
        self.drop_brief("T043-next.md", BRIEF.replace("T042-fix-the-thing", "T043-next"))
        self.log.write_text("")
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.inbox / "T043-next.md").exists())
        self.assertFalse(self.beat()["available"])
        self.assertEqual(self.beat()["retry_at"], reset)

    def test_a_limit_without_a_reset_waits_an_hour(self):
        before = int(time.time())
        self.drop_brief()
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="Error: 429 Too Many Requests")
        self.assertTrue(before + 3600 <= self.beat()["retry_at"] <= int(time.time()) + 3600)

    def test_a_limit_with_a_retry_after_waits_that_long(self):
        before = int(time.time())
        self.drop_brief()
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="status 429: retry after 120 seconds")
        self.assertTrue(before + 120 <= self.beat()["retry_at"] <= int(time.time()) + 120)

    def test_a_lane_takes_work_again_once_its_retry_time_passed(self):
        usage = self.loom / "state" / "usage"
        usage.mkdir(parents=True)
        (usage / "codex.json").write_text(json.dumps(
            {"lane": "codex", "at": 1, "available": False, "reason": "limit",
             "retry_at": int(time.time()) - 5}))
        self.drop_brief()
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())
        self.assertTrue(self.beat()["available"])
        self.assertNotIn("retry_at", self.beat())

    def test_a_finished_run_that_mentions_rate_limits_still_opens_its_pr(self):
        self.drop_brief()
        result = self.run_lane(FAKE_AGENT="say", FAKE_EDIT="1", FAKE_EXIT="0",
                               FAKE_OUT="Added a rate limit and a quota check; HTTP 429 now.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists())
        self.assertTrue(self.beat()["available"])

    def test_a_refusal_by_a_guard_is_a_work_failure(self):
        self.drop_brief(text=BRIEF.replace("Repo: example/repo", "Repo: stranger/repo"))
        self.run_lane()
        self.assertTrue(self.failure_line().startswith("[work] owner stranger"))

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

    @unittest.skipUnless(sandbox_caps.PROCESS_TABLE, sandbox_caps.NO_PROCESS_TABLE)
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

    def test_a_check_home_left_by_a_killed_runner_goes_with_the_next_pass(self):
        # loom#643: 17 of these, about 300 MB each, filled run/.
        left = self.loom / "run" / "codex-check" / "Library" / "Caches"
        left.mkdir(parents=True)
        (left / "big").write_text("x" * 1000)
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.loom / "run" / "codex-check").exists())

    def test_a_running_lanes_check_home_is_left_to_it(self):
        left = self.loom / "run" / "codex-check" / "tmp"
        left.mkdir(parents=True)
        lock = self.loom / "run" / "codex.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{os.getpid()}\n")
        result = self.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(left.exists())

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


    # The validation contract (.loom/validation.conf).

    def contract_repo(self, contract=CONTRACT):
        """Give the remote a submodule pinned behind its tip, the contract
        and check.py. Returns the pinned submodule commit."""
        root = Path(self._tmp.name)
        commit = ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm")
        sub = root / "sub-seed"
        git("init", "-q", "-b", "main", str(sub))
        (sub / "value.txt").write_text("v1\n")
        git("add", "value.txt", cwd=sub)
        git(*commit, "v1", cwd=sub)
        pinned = subprocess.run(["git", "rev-parse", "HEAD"], cwd=sub, capture_output=True,
                                text=True, check=True).stdout.strip()
        (sub / "value.txt").write_text("v2\n")
        git(*commit, "v2", "-a", cwd=sub)
        git("clone", "-q", "--bare", str(sub), str(self.remote.parent / "sub.git"))
        work = root / "contract-work"
        git("clone", "-q", str(self.remote), str(work))
        git("-c", "protocol.file.allow=always", "submodule", "add", "-q", "../sub.git", "vendor/sub", cwd=work)
        git("checkout", "-q", pinned, cwd=work / "vendor" / "sub")
        (work / ".loom").mkdir()
        (work / ".loom" / "validation.conf").write_text(contract)
        (work / "check.py").write_text(CHECK_PY)
        git("add", "-A", cwd=work)
        git(*commit, "contract", cwd=work)
        git("push", "-q", "origin", "main", cwd=work)
        return pinned

    def run_contract_lane(self, interpreter=True, **env):
        """A codex pass that may clone file:// submodules; with interpreter,
        loom-test-python is on PATH. The name exists on no machine, so the
        test without it does not depend on the host's Pythons (the CI runner
        has a python3.12 on its PATH)."""
        path = self.lane_env()["PATH"]
        if interpreter:
            self.write_tool(Path(self._tmp.name) / "pybin" / "loom-test-python",
                            f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
            path = f"{Path(self._tmp.name) / 'pybin'}:{path}"
        return self.run_lane(PATH=path, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.file.allow",
                             GIT_CONFIG_VALUE_0="always", **env)

    def records(self):
        path = self.loom / "logs" / "codex" / "T042-fix-the-thing.validation.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def remote_heads(self):
        return subprocess.run(["git", "ls-remote", "--heads", str(self.remote)],
                              capture_output=True, text=True, check=True).stdout

    def lane_log(self):
        return (self.loom / "logs" / "codex" / "T042-fix-the-thing.log").read_text()

    @unittest.skipUnless(shutil.which("sandbox-exec"), "validation runs under macOS sandbox-exec")
    def test_an_unprepared_workspace_is_a_setup_failure_and_the_agent_never_runs(self):
        self.contract_repo()
        self.drop_brief()
        result = self.run_contract_lane(interpreter=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertIn("setup failed: interpreter loom-test-python is not on PATH", self.lane_log())
        self.assertEqual([(r["phase"], r["ok"]) for r in self.records()], [("prepare", False)])

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_prepared_workspace_checks_the_agents_commit_before_the_pr(self):
        pinned = self.contract_repo()
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), self.lane_log())
        # The agent worked in the prepared tree and was told the check.
        self.assertIn("agent-saw submodule", self.calls())
        self.assertIn("## Validation environment", self.log.read_text())
        records = self.records()
        self.assertEqual([(r["phase"], r["exit"], r["ok"]) for r in records],
                         [("prepare", 0, True), ("setup", 0, True), ("baseline", 0, True), ("final", 0, True)])
        self.assertIn(pinned, records[0]["detail"])
        self.assertEqual(records[-1]["command"], "test -f .prepared && loom-test-python check.py")
        head = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "loom/T042-fix-the-thing"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(records[-1]["revision"], head)
        self.assertIn(f"Validation: check exited 0 at {head[:12]}", self.body.read_text())
        # What setup left behind is not committed.
        files = subprocess.run(["git", "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", head],
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertIn("fix.txt", files)
        self.assertNotIn(".prepared", files)
        # The check's scratch home is gone once the run is over (loom#643).
        self.assertFalse((self.loom / "run" / "codex-check").exists())

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_zero_exit_agent_with_a_failing_check_opens_no_pr_and_keeps_its_edits(self):
        self.contract_repo()
        self.drop_brief()
        result = self.run_contract_lane(FAKE_AGENT="edit", FAKE_AGENT_TEXT="broken")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertIn("validation failed: the check exited 1", self.lane_log())
        self.assertFalse(any(r["phase"] == "final" and r["ok"] for r in self.records()))
        self.assertFalse((self.loom / "run" / "codex-check").exists())
        heads = self.remote_heads()
        self.assertNotIn("refs/heads/loom/T042-fix-the-thing\n", heads)
        side = [h.split("refs/heads/")[1] for h in heads.splitlines() if "-unvalidated-" in h]
        self.assertEqual(len(side), 1, heads)
        kept = subprocess.run(["git", "--git-dir", str(self.remote), "show", f"{side[0]}:fix.txt"],
                              capture_output=True, text=True, check=True).stdout
        self.assertEqual(kept, "broken\n")

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_check_that_has_to_reformat_the_agents_files_does_not_pass(self):
        self.contract_repo()
        self.drop_brief()
        result = self.run_contract_lane(FAKE_AGENT_TEXT="fixed   ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))
        self.assertIn("the check changed tracked files", self.lane_log())
        self.assertEqual(self.records()[-1]["exit"], 0)
        self.assertFalse(self.records()[-1]["ok"])

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_the_agent_cannot_weaken_the_contract_it_is_checked_against(self):
        self.contract_repo()
        self.drop_brief()
        result = self.run_contract_lane(FAKE_AGENT="edit", FAKE_AGENT_TEXT="broken", FAKE_AGENT_WEAKEN="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertEqual(self.records()[-1]["command"], "test -f .prepared && loom-test-python check.py")

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_fix_run_checks_against_the_base_branchs_contract_not_the_pull_requests(self):
        self.contract_repo()
        name, _ = self.fix_brief()
        # An earlier run on the pull request's branch weakened the contract.
        work = Path(self._tmp.name) / "pr-work"
        (work / ".loom").mkdir(exist_ok=True)
        (work / ".loom" / "validation.conf").write_text("check: true\n")
        git("add", "-A", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "weaken", cwd=work)
        git("push", "-q", "origin", self.FIX_BRANCH, cwd=work)
        before = self.remote_head(self.FIX_BRANCH)
        result = self.run_contract_lane(FAKE_AGENT_TEXT="broken")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        self.assertEqual(self.remote_head(self.FIX_BRANCH), before)
        path = self.loom / "logs" / "codex" / (name + ".validation.jsonl")
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records[-1]["command"], "test -f .prepared && loom-test-python check.py")
        self.assertFalse(records[-1]["ok"])

    def test_a_contract_with_an_unknown_line_is_a_setup_failure(self):
        self.contract_repo(contract=CONTRACT + "python: 3.12\n")
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        self.assertIn("setup failed: .loom/validation.conf has an unknown line 'python: 3.12'", self.lane_log())

    def test_a_pipe_form_contract_line_is_not_an_unknown_line(self):
        # infra-public's form: one "<kind> | <command>" line per command.
        self.contract_repo(contract="lint | python3 check.py\n")
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertNotIn("unknown line", self.lane_log())

    def test_a_contract_the_parser_rejects_is_a_factory_incident_not_work(self):
        self.contract_repo(contract=CONTRACT + "python: 3.12\n")
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        log = self.lane_log()
        self.assertIn("failed: [factory]", log)
        self.assertNotIn("failed: [work]", log)
        self.assertIn("factory incident quadseven/loom#99", log)
        # The incident names the repo and the exact failing line, in the
        # body it is filed with (not just anywhere in the call log).
        self.assertIn("gh issue create --repo quadseven/loom", self.log.read_text())
        body = Path(f"{self.body}.issue").read_text()
        self.assertIn("Repo: example/repo", body)
        self.assertIn("python: 3.12", body)

    def test_a_repeated_factory_incident_updates_the_open_issue(self):
        self.contract_repo(contract=CONTRACT + "python: 3.12\n")
        self.drop_brief()
        # The fake gh prints $FAKE_GH_SEARCH verbatim for `search issues`;
        # real gh returns the open incident's number through the --jq filter.
        result = self.run_contract_lane(FAKE_GH_SEARCH="7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertIn("factory incident quadseven/loom#7", self.lane_log())
        self.assertFalse(any(c.startswith("gh issue create") for c in self.calls()))
        self.assertTrue(any(c.startswith("gh issue comment") for c in self.calls()))

    def test_a_failed_incident_filing_still_parks_as_factory(self):
        self.contract_repo(contract=CONTRACT + "python: 3.12\n")
        self.drop_brief()
        result = self.run_contract_lane(FAKE_GH_ISSUE_CREATE_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        log = self.lane_log()
        self.assertIn("failed: [factory]", log)
        self.assertIn("factory incident filing failed", log)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_base_check_that_is_already_red_is_a_factory_incident(self):
        contract = ("interpreter: loom-test-python\n"
                    "check: loom-test-python -c 'import sys; sys.exit(2)'\n")
        self.contract_repo(contract=contract)
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        log = self.lane_log()
        self.assertIn("failed: [factory]", log)
        self.assertNotIn("failed: [work]", log)
        self.assertIn("base check exited 2", log)
        self.assertIn("factory incident quadseven/loom#99", log)
        calls = self.log.read_text()
        self.assertIn("Repo: example/repo", calls)
        self.assertIn("exit 2", calls)

    def test_a_base_check_incident_is_recorded_then_closed_when_the_check_passes(self):
        # A stub sandbox: these tests run inside a lane's own check sandbox,
        # where a nested sandbox-exec fails and red-checked loom's base
        # (2026-10-19).
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        red = ("interpreter: loom-test-python\n"
               "check: loom-test-python -c 'import sys; sys.exit(2)'\n")
        self.contract_repo(contract=red)
        self.drop_brief()
        self.run_contract_lane()
        marker = self.loom / "state" / "incidents" / "example_repo-base-check"
        self.assertEqual(marker.read_text().strip(), "99")
        self.assertFalse(any(c.startswith("gh issue close") for c in self.calls()))

    def test_a_passing_base_check_closes_the_repos_open_incident(self):
        # Nothing closed incidents: 16 tickets sat held behind five whose
        # checks passed again for hours (2026-10-19).
        # A stub sandbox: these tests run inside a lane's own check sandbox,
        # where a nested sandbox-exec fails and red-checked loom's base
        # (2026-10-19).
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        self.contract_repo()
        marker = self.loom / "state" / "incidents" / "example_repo-base-check"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("41\n")
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        closes = [c for c in self.calls() if c.startswith("gh issue close")]
        self.assertEqual(len(closes), 1, self.calls())
        self.assertIn("--repo quadseven/loom 41", closes[0])
        self.assertFalse(marker.exists())

    def test_a_green_fix_branch_does_not_close_an_incident(self):
        # A fix round's tree holds the agent's unmerged edits: only the trusted
        # default branch passing may clear an incident.
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        self.contract_repo()
        self.fix_brief()
        marker = self.loom / "state" / "incidents" / "example_repo-base-check"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("41\n")
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("gh issue close") for c in self.calls()))
        self.assertTrue(marker.exists())

    def test_the_default_branch_check_runs_in_a_tree_with_its_own_git_directory(self):
        # A worktree's .git is a file naming the fix tree's .git, which the
        # check sandbox cannot read: loom's metrics tests run git ls-files and
        # failed there, a false base-check incident each fix round (2026-10-19).
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        self.contract_repo(contract=CONTRACT.replace(
            "check: test -f .prepared", "check: test -d .git && git ls-files check.py | grep -q . && test -f .prepared"))
        name, _ = self.fix_brief()
        work = Path(self._tmp.name) / "pr-work"
        (work / "fix.txt").write_text("broken\n")
        git("add", "fix.txt", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "Break check", cwd=work)
        git("push", "-q", "origin", self.FIX_BRANCH, cwd=work)
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("gh issue create") for c in self.calls()),
                         self.run_log(name + ".log"))
        records = [json.loads(line) for line in (self.loom / "logs" / "codex" /
                   (name + ".validation.jsonl")).read_text().splitlines()]
        default = next(r for r in records if r["phase"] == "default-baseline")
        self.assertEqual(default["exit"], 0)
        self.assertFalse(list(self.loom.glob("**/*-default-check")))

    def test_a_conflicting_fix_round_checks_a_default_branch_its_shallow_clone_never_held(self):
        # The fix tree is a shallow clone, so `git clone --local` of it falls
        # back to a plain clone that copies branches but not origin/main.
        # With main past the conflicting fix branch, the default branch
        # commit was missing and every such round failed setup before its
        # agent could resolve the conflict (2026-10-20). No setup
        # step: a conflicting round runs setup only in the default tree.
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        self.contract_repo(contract=CONTRACT.replace(
            "setup: loom-test-python -c 'open(\".prepared\", \"w\").write(\"ok\")'\n", "").replace(
            "test -f .prepared && ", ""))
        name, _ = self.fix_brief()
        self.advance_main("first.txt", "main's version\n")
        self.write_tool(self.bin / "codex", self.AGENT_NOTHING + "printf 'both kept\\n' > first.txt\n")
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.run_log(name + ".log")
        self.assertIn("shallow", log)
        self.assertNotIn("could not check out the default branch", log)
        self.assertIn("agent saw conflicts", self.calls())
        records = [json.loads(line) for line in (self.loom / "logs" / "codex" /
                   (name + ".validation.jsonl")).read_text().splitlines()]
        default = next(r for r in records if r["phase"] == "default-baseline")
        self.assertEqual((default["exit"], default["revision"]), (0, self.remote_head("main")))
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists())

    def test_a_default_branch_checkout_that_still_fails_is_a_factory_incident_not_work(self):
        # A [work] failure here spent a ladder attempt, and counted toward
        # the runaway hold, for a fault in the runner's own clone (2026-10-20).
        real_git = shutil.which("git")
        self.write_tool(self.bin / "git", "#!/bin/sh\ncase \" $* \" in *\" checkout -q --detach \"*) "
                        "echo 'fatal: unable to read tree' >&2; exit 128;; esac\n"
                        f"exec {shlex.quote(real_git)} \"$@\"\n")
        self.contract_repo(contract=CONTRACT.replace(
            "setup: loom-test-python -c 'open(\".prepared\", \"w\").write(\"ok\")'\n", "").replace(
            "test -f .prepared && ", ""))
        name, _ = self.fix_brief()
        self.advance_main("first.txt", "main's version\n")
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        log = self.run_log(name + ".log")
        self.assertIn("failed: [factory] setup failed: could not check out the default branch", log)
        self.assertNotIn("failed: [work]", log)
        self.assertIn("factory incident quadseven/loom#99", log)
        self.assertFalse(list(self.loom.glob("**/*-default-check")))

    def test_a_red_fix_head_on_green_main_reaches_the_agent_without_an_incident(self):
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        self.contract_repo()
        name, _ = self.fix_brief()
        work = Path(self._tmp.name) / "pr-work"
        (work / "fix.txt").write_text("broken\n")
        git("add", "fix.txt", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "Break check", cwd=work)
        git("push", "-q", "origin", self.FIX_BRANCH, cwd=work)
        self.advance_main("main.txt", "new main\n")
        main = self.remote_head("main")
        self.write_tool(self.bin / "codex", FAKE_AGENT.replace(
            "cat > /dev/null", "case \"$*\" in *'On the base it exited 1'*) ;; *) exit 9 ;; esac\n"
            "test -f main.txt || exit 9\n"
            "echo 'agent saw red check on merged main' >> \"$FAKE_LOG\"\n"
            "cat > /dev/null"))
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / (name + ".md")).exists(), self.run_log(name + ".log"))
        self.assertIn("agent saw red check on merged main", self.calls())
        self.assertFalse(any(c.startswith("gh issue create") for c in self.calls()))
        self.assertFalse((self.loom / "state" / "factory_held.json").exists())
        records = [json.loads(line) for line in (self.loom / "logs" / "codex" /
                   (name + ".validation.jsonl")).read_text().splitlines()]
        baseline = next(r for r in records if r["phase"] == "baseline")
        self.assertEqual(baseline["exit"], 1)
        self.assertNotEqual(baseline["revision"], main)
        ancestor = subprocess.run(["git", "--git-dir", str(self.remote), "merge-base",
                                   "--is-ancestor", main, self.FIX_BRANCH])
        self.assertEqual(ancestor.returncode, 0)

    def test_a_red_default_branch_on_a_fix_round_files_a_base_check_incident(self):
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        self.contract_repo()
        name, _ = self.fix_brief()
        self.advance_main("fix.txt", "broken\n")
        main = self.remote_head("main")
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / (name + ".md")).exists())
        self.assertFalse(any(c.startswith("codex ") for c in self.calls()))
        self.assertIn("base check exited 1", self.run_log(name + ".log"))
        self.assertTrue(any(c.startswith("gh issue create") for c in self.calls()))
        records = [json.loads(line) for line in (self.loom / "logs" / "codex" /
                   (name + ".validation.jsonl")).read_text().splitlines()]
        self.assertTrue(any(r["revision"] == main and r["exit"] == 1 for r in records))

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_final_check_the_environment_cannot_run_is_a_factory_incident(self):
        # The baseline passes; the check's binary is gone by the final run.
        contract = ("interpreter: loom-test-python\n"
                    "check: test ! -f fix.txt || loom-test-python-missing\n")
        self.contract_repo(contract=contract)
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        log = self.lane_log()
        self.assertIn("failed: [factory]", log)
        self.assertNotIn("failed: [work]", log)
        self.assertIn("validation environment unavailable: the check exited 127", log)

    def test_a_contract_command_with_a_control_or_non_ascii_byte_is_a_setup_failure(self):
        self.contract_repo(contract=CONTRACT + "setup: true\x1b[2K; echo hidden\n")
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "failed" / "T042-fix-the-thing.md").exists())
        self.assertFalse(any(c.startswith("codex") for c in self.calls()))
        self.assertIn("setup: and check: must be printable ASCII", self.lane_log())

    def test_contract_run_itself_refuses_an_unchecked_command(self):
        # The guard sits at the bash -c call too, not only in load_contract.
        text = RUNNER.read_text()
        funcs = "".join(re.search(r"^%s\(\) \{\n.*?^\}\n" % name, text, re.S | re.M).group(0)
                        for name in ("contract_command_ok", "contract_run"))
        tmp = Path(self._tmp.name)
        marker = tmp / "ran"
        fakes = tmp / "fakebin"
        fakes.mkdir()
        (fakes / "sandbox-exec").write_text('#!/bin/bash\nshift 2\nexec "$@"\n')
        (fakes / "sandbox-exec").chmod(0o755)
        (fakes / "timeout").write_text('#!/bin/bash\nshift\nexec "$@"\n')
        (fakes / "timeout").chmod(0o755)
        script = funcs + 'sandbox_profile() { echo "(version 1)"; }\ncontract_run "$1"\n'
        env = {"PATH": f"{fakes}:/usr/bin:/bin", "LOG": str(tmp / "log"), "TREE": str(tmp),
               "CHECK_HOME": str(tmp), "CHECK_CLI": "bash", "CHECK_TIMEOUT": "30"}
        result = subprocess.run(["bash", "-c", script, "test", f"true\x1b[2K; touch {marker}"],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertFalse(marker.exists())
        result = subprocess.run(["bash", "-c", script, "test", f"touch {marker}\n"],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertFalse(marker.exists())
        result = subprocess.run(["bash", "-c", script, "test", f"touch {marker}"],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())

    def test_contract_run_makes_uv_use_only_its_own_pythons(self):
        # An x86_64 python3 on PATH that the host could not run made every
        # muster check exit 2 on the base (2026-10-16); the check sets
        # UV_MANAGED_PYTHON, and nothing else of the runner's leaks in.
        text = RUNNER.read_text()
        funcs = "".join(re.search(r"^%s\(\) \{\n.*?^\}\n" % name, text, re.S | re.M).group(0)
                        for name in ("contract_command_ok", "contract_run"))
        tmp = Path(self._tmp.name)
        fakes = tmp / "fakebin"
        fakes.mkdir()
        (fakes / "sandbox-exec").write_text('#!/bin/bash\nshift 2\nexec "$@"\n')
        (fakes / "sandbox-exec").chmod(0o755)
        (fakes / "timeout").write_text('#!/bin/bash\nshift\nexec "$@"\n')
        (fakes / "timeout").chmod(0o755)
        script = funcs + 'sandbox_profile() { echo "(version 1)"; }\ncontract_run "$1"\n'
        log = tmp / "log"
        env = {"PATH": f"{fakes}:/usr/bin:/bin", "LOG": str(log), "TREE": str(tmp),
               "CHECK_HOME": str(tmp), "CHECK_CLI": "bash", "CHECK_TIMEOUT": "30",
               "RUNNER_SECRET": "x"}
        result = subprocess.run(["bash", "-c", script, "test",
                                 "echo uv=${UV_MANAGED_PYTHON:-unset} secret=${RUNNER_SECRET:-unset}"],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uv=1 secret=unset", log.read_text())


    def test_contract_run_puts_uv_and_uvx_on_the_checks_path(self):
        # Every infra ticket held on the factory incident: the contract runs
        # `uvx` but the lane machine keeps uv outside the runner's PATH (in
        # ~/.local/bin), so the check exited 127 on the unmodified base.
        # With uvx only in a scratch .local/bin under the scratch home and
        # not on the caller's PATH, a contract check running `uvx --version`
        # exits 0; it fails on main.
        text = RUNNER.read_text()
        funcs = "".join(re.search(r"^%s\(\) \{\n.*?^\}\n" % name, text, re.S | re.M).group(0)
                        for name in ("contract_command_ok", "contract_run"))
        tmp = Path(self._tmp.name)
        localbin = tmp / "home" / ".local" / "bin"
        localbin.mkdir(parents=True)
        (localbin / "uvx").write_text('#!/bin/bash\necho "uvx 0.9.0 (scratch)"\n')
        (localbin / "uvx").chmod(0o755)
        fakes = tmp / "fakebin"
        fakes.mkdir()
        (fakes / "sandbox-exec").write_text('#!/bin/bash\nshift 2\nexec "$@"\n')
        (fakes / "sandbox-exec").chmod(0o755)
        (fakes / "timeout").write_text('#!/bin/bash\nshift\nexec "$@"\n')
        (fakes / "timeout").chmod(0o755)
        script = funcs + 'sandbox_profile() { echo "(version 1)"; }\ncontract_run "$1"\n'
        log = tmp / "log"
        env = {"PATH": f"{fakes}:/usr/bin:/bin", "HOME": str(tmp / "home"), "LOG": str(log),
               "TREE": str(tmp), "CHECK_HOME": str(tmp), "CHECK_CLI": "bash",
               "CHECK_TIMEOUT": "30"}
        result = subprocess.run(["bash", "-c", script, "test", "uvx --version"],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uvx 0.9.0 (scratch)", log.read_text())

    def test_final_validation_removes_an_untracked_agent_venv_before_uv_check(self):
        # The agent commits its change, then leaves an untracked environment
        # whose interpreter is unusable to the final sandbox.
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        contract = "interpreter: loom-test-python\ncheck: test ! -e .venv && test -f fix.txt\n"
        self.contract_repo(contract=contract)
        self.drop_brief(text=BRIEF.replace("Make the thing work.",
                                           "Make the thing work. Leave a .venv for validation."))
        replacement = FAKE_AGENT.replace(
            'git -c user.name=t -c user.email=t@example.invalid commit -qm "Fix the thing"',
            'git -c user.name=t -c user.email=t@example.invalid commit -qm "Fix the thing"\n'
            'echo /.venv >> .git/info/exclude\nmkdir -p .venv/bin\nprintf "blocked" > .venv/bin/python3')
        self.write_tool(self.bin / "codex", replacement)
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), self.lane_log())
        self.assertFalse(self.records()[-1]["ok"] is False, self.lane_log())

    def test_final_validation_classifies_denied_python_interpreter_as_factory(self):
        text = RUNNER.read_text()
        function = re.search(r"^validation_interpreter_denied\(\) \{\n.*?^\}\n",
                             text, re.S | re.M).group(0)
        script = function + 'validation_interpreter_denied "$1" "$2" && echo factory\n'
        result = subprocess.run(["bash", "-c", script, "test", "2",
                                 "Failed to query Python interpreter\\nOperation not permitted"],
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "factory")
        result = subprocess.run(["bash", "-c", script, "test", "1", "ordinary test failure"],
                                capture_output=True, text=True)
        self.assertEqual(result.stdout, "")

    def test_final_interpreter_permission_error_is_a_factory_failure(self):
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        contract = ("interpreter: loom-test-python\n"
                    "check: grep -q todo fix.txt || { echo 'Failed to query Python interpreter'; "
                    "echo 'Operation not permitted'; exit 2; }\n")
        self.contract_repo(contract=contract)
        self.drop_brief()
        result = self.run_contract_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.lane_log()
        self.assertIn("failed: [factory]", log)
        self.assertNotIn("failed: [work]", log)
        self.assertIn("Python interpreter query was denied by the sandbox", log)
        self.assertIn("factory incident quadseven/loom#99", log)

    def test_untracked_venv_cleanup_preserves_tracked_files_under_venv(self):
        text = RUNNER.read_text()
        function = re.search(r"^remove_untracked_venvs\(\) \{\n.*?^\}\n",
                             text, re.S | re.M).group(0)
        tree = Path(self._tmp.name) / "tree"
        tree.mkdir()
        tracked = tree / ".venv" / "tracked.txt"
        tracked.parent.mkdir()
        tracked.write_text("tracked\n")
        git("init", "-q", str(tree))
        git("add", ".venv/tracked.txt", cwd=tree)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "seed", cwd=tree)
        (tree / ".venv" / "untracked.txt").write_text("keep with tracked directory\n")
        untracked = tree / "nested" / ".venv"
        untracked.mkdir(parents=True)
        (untracked / "python").write_text("stub\n")
        script = 'safe_git() { git -C "$TREE" "$@"; }\n' + function + 'remove_untracked_venvs\n'
        result = subprocess.run(["bash", "-c", script], env={"PATH": os.environ["PATH"],
                                 "TREE": str(tree)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(tracked.exists())
        self.assertTrue((tree / ".venv" / "untracked.txt").exists())
        self.assertFalse(untracked.exists())

    def test_the_check_profile_allows_the_uv_files_but_not_their_directory(self):
        # The check profile lets the contract read and execute the runner's
        # uv and uvx while still refusing every other file under the
        # runner's home: the two binaries get file literals, their directory
        # gets no subpath rule.
        text = (Path(__file__).resolve().parent.parent / "lanes" / "agent_sandbox.sh").read_text()
        funcs = re.search(r"^sandbox_profile\(\) \{\n.*?^\}\n", text, re.S | re.M).group(0)
        tmp = Path(self._tmp.name)
        home = tmp / "home"
        tree = tmp / "tree"
        tree.mkdir()
        localbin = home / ".local" / "bin"
        localbin.mkdir(parents=True)
        for name in ("uv", "uvx"):
            (localbin / name).write_text("#!/bin/bash\n")
            (localbin / name).chmod(0o755)
        (localbin / "not-uv").write_text("a secret")
        script = funcs + 'sandbox_profile "$PWD" bash check\n'
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "AGENT_HOME": str(tmp / "agent"),
               "TREE": str(tree), "CHECK_UV_BINS": f"{localbin}/uv {localbin}/uvx"}
        result = subprocess.run(["bash", "-c", script], env=env, cwd=str(tree),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = result.stdout
        resolved_home = home.resolve()
        resolved_localbin = localbin.resolve()
        allow_read = next(line for line in profile.splitlines()
                          if line.startswith("(allow file-read* "))
        self.assertIn('(deny file-read* file-write* (subpath "%s"))' % resolved_home, profile)
        self.assertIn('(literal "%s/uv")' % resolved_localbin, profile)
        self.assertIn('(literal "%s/uvx")' % resolved_localbin, profile)
        self.assertNotIn('(subpath "%s")' % resolved_localbin, allow_read)
        self.assertNotIn('(subpath "%s")' % resolved_home, allow_read)
        self.assertNotIn("not-uv", profile)
        # The same files named for an agent run get no such allowance.
        result = subprocess.run(["bash", "-c", funcs + 'sandbox_profile "$PWD" bash\n'],
                                env=env, cwd=str(tree), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(resolved_localbin), result.stdout)


if __name__ == "__main__":
    unittest.main()
