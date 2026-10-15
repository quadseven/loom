#!/usr/bin/env bash
# Loom triage rewriter agent: the command bus/rewrite.py runs for
# LOOM_REWRITE_CMD and LOOM_REWRITE_PRIVATE_CMD.
#
#   rewrite_agent.sh <key-ssm> <provider/model[,provider/model...]>   (the prompt on stdin)
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

KEY_SSM="${1:-}" MODEL_LIST="${2:-}"
[ $# -eq 2 ] || fail "usage: rewrite_agent.sh <key-ssm> <provider/model[,provider/model...]>" 2
[[ "$KEY_SSM" =~ ^[A-Za-z0-9_./-]+$ ]] || fail "bad SSM parameter name" 2
[ -n "$MODEL_LIST" ] || fail "no models supplied" 2
IFS=, read -r -a MODELS <<< "$MODEL_LIST"
KEY_VARS=()
for MODEL in "${MODELS[@]}"; do
  [[ "$MODEL" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._:/@-]+$ ]] || fail "bad model '$MODEL': want provider/model" 2
  case "${MODEL%%/*}" in
    opencode|opencode-go) KEY_VARS+=(OPENCODE_API_KEY) ;;
    openrouter) KEY_VARS+=(OPENROUTER_API_KEY) ;;
    zai) KEY_VARS+=(ZHIPU_API_KEY) ;;
    anthropic) KEY_VARS+=(ANTHROPIC_API_KEY) ;;
    *) fail "no key variable known for provider ${MODEL%%/*}" 2 ;;
  esac
done
MODEL="${MODELS[0]}" KEY_VAR="${KEY_VARS[0]}"

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

# JSON events, one per line, so final text is told apart from tool output.
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
REASONS=""
for i in "${!MODELS[@]}"; do
  MODEL="${MODELS[$i]}" KEY_VAR="${KEY_VARS[$i]}"
  OUT="$RUN_DIR/out" ANSWER_FILE="$RUN_DIR/answer"
  : > "$OUT"
  cd "$AGENT_HOME/work" || fail "cannot enter the scratch working directory"
  opencode_run "$KEY_VAR" "$BIN" --format json > "$OUT"
  status=$?
  reason="$(python3 -I -c "$EXTRACT" "$OUT" 2>&1 > "$ANSWER_FILE")"
  extracted=$?
  if [ "$status" -eq 0 ] && [ "$extracted" -eq 0 ]; then
    ANSWER="$(cat "$ANSWER_FILE")" || fail "cannot read the answer"
    case "$ANSWER" in *"$API_KEY"*) fail "the answer holds the provider key" ;; esac
    printf '%s\n' "$ANSWER"
    exit 0
  fi
  [ "$extracted" -eq 0 ] || reason="${reason//"$API_KEY"/[key]}"
  lower="$(printf '%s' "$reason" | tr '[:upper:]' '[:lower:]')"
  if [[ "$status" -eq 124 || "$status" -eq 137 ]]; then
    why="timed out"
  elif [[ "$lower" == *"rate limit"* || "$lower" == *"rate-limit"* || "$lower" == *"overload"* || "$lower" == *"too many requests"* ]]; then
    why="rate limited or overloaded"
  elif [[ "$lower" == *"no text"* ]]; then
    why="opencode exited $status with no text"
  else
    why="opencode exited $status: ${reason:-no usable answer}"
  fi
  REASONS="${REASONS:+$REASONS; }$MODEL: $why"
  echo "rewrite_agent: passed over $MODEL: $why" >&2
done
fail "all models failed: $REASONS"
