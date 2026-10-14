#!/usr/bin/env bash
# Loom triage rewriter agent: the command bus/rewrite.py runs for
# LOOM_REWRITE_CMD and LOOM_REWRITE_PRIVATE_CMD.
#
#   rewrite_agent.sh <key-ssm> <provider/model>   (the prompt on stdin)
#
# Reads the provider key from the SSM parameter <key-ssm> and runs
# `opencode run` on <provider/model> with the prompt on its stdin, in the
# opencode lanes' sandbox (lanes/agent_sandbox.sh): an emptied environment,
# a scratch HOME made for this run and removed after it, an empty working
# directory, and the key exported to the agent only. Every opencode tool is
# denied: the answer is text. Prints only the agent's final text and exits
# non-zero on any failure, with the reason on stderr. The key is never
# printed.
#
# Env: LOOM_HOME (default ~/loom; the scratch home goes under its run/),
# LOOM_REWRITE_TIMEOUT (seconds for the agent, default 480, under the
# station's 600), LOOM_NET_TIMEOUT (seconds for the SSM read, default 120),
# LOOM_AWS_REGION, LOOM_REWRITE_OPENCODE (the opencode binary, default
# opencode from PATH).
set -uo pipefail

fail() { echo "rewrite_agent: $1" >&2; exit "${2:-1}"; }

KEY_SSM="${1:-}" MODEL="${2:-}"
[ $# -eq 2 ] || fail "usage: rewrite_agent.sh <key-ssm> <provider/model>" 2
[[ "$KEY_SSM" =~ ^[A-Za-z0-9_./-]+$ ]] || fail "bad SSM parameter name" 2
[[ "$MODEL" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._:/@-]+$ ]] || fail "bad model '$MODEL': want provider/model" 2
# The variable each provider's key travels in, as the lanes pass it.
case "${MODEL%%/*}" in
  opencode|opencode-go) KEY_VAR=OPENCODE_API_KEY ;;
  openrouter) KEY_VAR=OPENROUTER_API_KEY ;;
  zai) KEY_VAR=ZHIPU_API_KEY ;;
  anthropic) KEY_VAR=ANTHROPIC_API_KEY ;;
  *) fail "no key variable known for provider ${MODEL%%/*}" 2 ;;
esac

LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
LOOM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lanes/agent_sandbox.sh
. "$LOOM_ROOT/lanes/agent_sandbox.sh"

# What agent_sandbox.sh reads. No lane is agy or spark here, and the
# rewriter gets no skill snapshot.
LANE=rewrite SKILLS="" AGENT_EXTRA_ENV="" API_KEY=""
AGENT_TIMEOUT="${LOOM_REWRITE_TIMEOUT:-480}"
NET_TIMEOUT="${LOOM_NET_TIMEOUT:-120}"
OPENCODE_PERMISSION='{"*": "deny"}'
BIN="${LOOM_REWRITE_OPENCODE:-opencode}"
command -v "$BIN" > /dev/null || fail "$BIN not on PATH"
command -v sandbox-exec > /dev/null || fail "sandbox-exec not available"

# The run's own files (prompt, output, answer) sit beside the scratch home,
# where the sandbox denies the agent: the wrapper is not sandboxed, and
# writes nothing where the agent could have left a link.
mkdir -p "$LOOM_HOME/run" || fail "cannot create $LOOM_HOME/run"
RUN_DIR="$(mktemp -d "$LOOM_HOME/run/rewrite.XXXXXX")" || fail "cannot create a run directory"
# rm -rf removes links the agent left without following them.
trap 'rm -rf "$RUN_DIR"' EXIT
AGENT_HOME="$RUN_DIR/home"
mkdir -p "$AGENT_HOME/tmp" "$AGENT_HOME/work" || fail "cannot create the scratch home"
AGENT_STDIN="$RUN_DIR/prompt"
cat > "$AGENT_STDIN" || fail "cannot read the prompt"
[ -s "$AGENT_STDIN" ] || fail "no prompt on stdin"

API_KEY="$(ssm_value "$KEY_SSM")"
[ -n "$API_KEY" ] || fail "no key readable at the SSM parameter $KEY_SSM"

# JSON events, one per line, so the final text is told apart from anything
# else opencode or the sandbox writes (api_agent merges stderr into stdout).
cd "$AGENT_HOME/work" || fail "cannot enter the scratch working directory"
OUT="$RUN_DIR/out"
opencode_run "$KEY_VAR" "$BIN" --format json > "$OUT"
status=$?

# The last text part, or the reason there is none. opencode can exit 0
# after a provider error, so an error event fails the run too.
# shellcheck disable=SC2016 # python source, not shell
EXTRACT='
import json, sys
text, errors = None, []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if not isinstance(event, dict):
        continue
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if event.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip():
        text = part["text"]
    elif event.get("type") == "error":
        err = event.get("error") if isinstance(event.get("error"), dict) else {}
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        errors.append(str(data.get("message") or err.get("name") or "unknown error"))
if errors:
    sys.exit("the agent reported an error: " + "; ".join(errors)[:500])
if text is None:
    sys.exit("the agent gave no text")
sys.stdout.write(text.strip() + "\n")
'
# -I: the working directory is the agent's, and python would import from it.
reason="$(python3 -I -c "$EXTRACT" "$OUT" 2>&1 > "$RUN_DIR/answer")" || {
  [ "$status" -eq 0 ] || reason="opencode exited $status; $reason"
  # shellcheck disable=SC2031 # API_KEY is this shell's
  fail "${reason//"$API_KEY"/[key]}"
}
[ "$status" -eq 0 ] || fail "opencode exited $status"
ANSWER="$(cat "$RUN_DIR/answer")" || fail "cannot read the answer"
# An answer holding the key goes nowhere. (A test in bash, not grep: the key
# stays out of every argv.)
case "$ANSWER" in *"$API_KEY"*) fail "the answer holds the provider key" ;; esac
printf '%s\n' "$ANSWER"
