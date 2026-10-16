#!/usr/bin/env bash
# Loom triage rewriter agent: the command bus/rewrite.py runs for
# LOOM_REWRITE_CMD and LOOM_REWRITE_PRIVATE_CMD.
#
#   rewrite_agent.sh <key-ssm> <provider/model[,provider/model...]>
#   rewrite_agent.sh <codex/model[,claude/model...]>   (the prompt on stdin)
#
# The two-argument form reads a provider key from SSM and runs opencode. The
# one-argument form runs codex or claude with the prompt on stdin. Both use
# the shared sandbox (lanes/agent_sandbox.sh), a scratch HOME and empty work
# directory, and print only the final text. The lane CLIs use read-only or no
# tools. Failures exit non-zero with the reason on stderr.
#
# Env: LOOM_HOME (default ~/loom; the scratch home goes under its run/),
# LOOM_REWRITE_TIMEOUT (seconds for the agent, default 480, under the
# station's 600), LOOM_NET_TIMEOUT (seconds for the SSM read, default 120),
# LOOM_AWS_REGION, LOOM_REWRITE_OPENCODE, LOOM_REWRITE_CODEX and
# LOOM_REWRITE_CLAUDE (CLI binaries, default their names from PATH).
set -uo pipefail

fail() { echo "rewrite_agent: $1" >&2; exit "${2:-1}"; }

KEY_SSM="${1:-}" MODEL_LIST="${2:-}" LANE_CLI=0
if [ $# -eq 1 ]; then MODEL_LIST="$1"; KEY_SSM=""; LANE_CLI=1; fi
[ $# -eq 1 ] || [ $# -eq 2 ] || fail "usage: rewrite_agent.sh <key-ssm> <provider/model[,provider/model...]> or <codex/model[,claude/model...]>" 2
[ -z "$KEY_SSM" ] || [[ "$KEY_SSM" =~ ^[A-Za-z0-9_./-]+$ ]] || fail "bad SSM parameter name" 2
[ -n "$MODEL_LIST" ] || fail "no models supplied" 2
IFS=, read -r -a MODELS <<< "$MODEL_LIST"
KEY_VARS=()
for MODEL in "${MODELS[@]}"; do
  [[ "$MODEL" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._:/@-]+$ ]] || fail "bad model '$MODEL': want provider/model" 2
  case "${MODEL%%/*}" in
    codex|claude) [ "$LANE_CLI" -eq 1 ] || fail "lane CLI entries need the one-argument form" 2; KEY_VARS+=(-) ;;
    opencode|opencode-go) KEY_VARS+=(OPENCODE_API_KEY) ;;
    openrouter) KEY_VARS+=(OPENROUTER_API_KEY) ;;
    zai) KEY_VARS+=(ZHIPU_API_KEY) ;;
    anthropic) KEY_VARS+=(ANTHROPIC_API_KEY) ;;
    *) fail "no key variable known for provider ${MODEL%%/*}" 2 ;;
  esac
done
if [ "$LANE_CLI" -eq 1 ]; then
  for MODEL in "${MODELS[@]}"; do
    case "${MODEL%%/*}" in codex|claude) ;; *) fail "private lane entries must use codex or claude" 2 ;; esac
  done
fi
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
[ "$LANE_CLI" -eq 1 ] || command -v "$BIN" > /dev/null || fail "$BIN not on PATH"
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

if [ "$LANE_CLI" -eq 0 ]; then
  API_KEY="$(ssm_value "$KEY_SSM")"
  [ -n "$API_KEY" ] || fail "no key readable at the SSM parameter $KEY_SSM"
fi

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
    if isinstance(event.get("result"), str) and event["result"].strip():
        text = event["result"]
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
        text = item["text"]
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
  if [ "$LANE_CLI" -eq 1 ]; then
    PROVIDER="${MODEL%%/*}" ID="${MODEL#*/}"
    case "$PROVIDER" in
      codex) BIN="${LOOM_REWRITE_CODEX:-codex}"; set -- exec --json --sandbox read-only --ask-for-approval never --model "$ID" - ;;
      claude) BIN="${LOOM_REWRITE_CLAUDE:-claude}"; set -- -p --model "$ID" --output-format json --tools "" ;;
    esac
    command -v "$BIN" > /dev/null || fail "$BIN not on PATH"
    PROFILE="$(sandbox_profile "$PWD" "$BIN")" || fail "cannot create lane CLI sandbox"
    timeout "$AGENT_TIMEOUT" sandbox-exec -p "$PROFILE" /usr/bin/env -i \
      PATH="$PATH" HOME="$AGENT_HOME" TMPDIR="$AGENT_HOME/tmp" TMP="$AGENT_HOME/tmp" \
      TEMP="$AGENT_HOME/tmp" TERM=dumb LANG="${LANG:-en_US.UTF-8}" "$BIN" "$@" \
      < "$AGENT_STDIN" > "$OUT"
  else
    opencode_run "$KEY_VAR" "${LOOM_REWRITE_OPENCODE:-opencode}" --format json > "$OUT"
  fi
  status=$?
  reason="$(python3 -I -c "$EXTRACT" "$OUT" 2>&1 > "$ANSWER_FILE")"
  extracted=$?
  if [ "$status" -eq 0 ] && [ "$extracted" -eq 0 ]; then
    ANSWER="$(cat "$ANSWER_FILE")" || fail "cannot read the answer"
    if [ -n "$API_KEY" ]; then
      case "$ANSWER" in *"$API_KEY"*) fail "the answer holds the provider key" ;; esac
    fi
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
    why="$([[ "$LANE_CLI" -eq 1 ]] && echo 'lane CLI' || echo opencode) exited $status with no text"
  else
    why="$([[ "$LANE_CLI" -eq 1 ]] && echo 'lane CLI' || echo opencode) exited $status: ${reason:-no usable answer}"
  fi
  REASONS="${REASONS:+$REASONS; }$MODEL: $why"
  echo "rewrite_agent: passed over $MODEL: $why" >&2
done
fail "all models failed: $REASONS"
