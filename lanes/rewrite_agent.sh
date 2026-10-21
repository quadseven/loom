#!/usr/bin/env bash
# Loom triage rewriter agent: the command bus/rewrite.py runs for
# LOOM_REWRITE_CMD and LOOM_REWRITE_PRIVATE_CMD.
#
#   rewrite_agent.sh <key-ssm> <provider/model[,provider/model...]>   (the prompt on stdin)
#
# For OpenCode entries, reads the provider key from SSM. For subscription
# CLI entries, use - as the key argument. Every entry uses the shared sandbox
# with an emptied environment, scratch HOME and empty working directory.
# OpenCode and Claude tools are denied; Codex tools are read-only. Prints only
# final text and exits non-zero when every entry fails.
#
# Env: LOOM_HOME (default ~/loom; the scratch home goes under its run/),
# LOOM_REWRITE_TIMEOUT (seconds for the agent, default 480, under the
# station's 600), LOOM_NET_TIMEOUT (seconds for the SSM read, default 120),
# LOOM_AWS_REGION, LOOM_REWRITE_OPENCODE (default opencode from PATH), and
# LOOM_REWRITE_CLAUDE_TOKEN_SSM for a Claude subscription entry.
set -uo pipefail

fail() { echo "rewrite_agent: $1" >&2; exit "${2:-1}"; }

KEY_SSM="${1:-}" MODEL_LIST="${2:-}"
[ $# -eq 2 ] || fail "usage: rewrite_agent.sh <key-ssm> <provider/model[,provider/model...]>" 2
[[ "$KEY_SSM" = - || "$KEY_SSM" =~ ^[A-Za-z0-9_./-]+$ ]] || fail "bad SSM parameter name" 2
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
    codex|claude|free) KEY_VARS+=(-) ;;
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

if [ "$KEY_SSM" != - ]; then
  API_KEY="$(ssm_value "$KEY_SSM")"
  [ -n "$API_KEY" ] || fail "no key readable at the SSM parameter $KEY_SSM"
fi

# Subscription CLIs use the same isolated home. Codex's login file is copied
# before the agent starts; Claude can use an OAuth token supplied by the
# installer. No other settings, hooks or plugins from the operator home enter.
if [[ ",${MODEL_LIST}," == *",codex/"* ]] && [ -f "$HOME/.codex/auth.json" ]; then
  mkdir -p "$AGENT_HOME/.codex" || fail "cannot prepare codex home"
  cp "$HOME/.codex/auth.json" "$AGENT_HOME/.codex/auth.json" || fail "cannot copy codex login"
  chmod 600 "$AGENT_HOME/.codex/auth.json"
fi

LANE_EXTRACT='
import json, sys
provider = sys.argv[2]
answer = None
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try: event = json.loads(line)
    except ValueError: continue
    if provider == "codex" and event.get("type") == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message": answer = item.get("text")
    elif provider == "claude" and event.get("type") == "result":
        if event.get("is_error"): sys.exit("the agent reported an error")
        answer = event.get("result")
if not isinstance(answer, str) or not answer.strip(): sys.exit("the agent gave no text")
sys.stdout.write(answer.strip() + "\n")
'

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
  PROVIDER="${MODEL%%/*}" MODEL_ID="${MODEL#*/}"
  AGENT_EXTRA_ENV=""
  [ "$KEY_VAR" = - ] || [ -n "$API_KEY" ] || fail "keyed model $MODEL needs an SSM parameter" 2
  OUT="$RUN_DIR/out" ANSWER_FILE="$RUN_DIR/answer"
  : > "$OUT"
  cd "$AGENT_HOME/work" || fail "cannot enter the scratch working directory"
  case "$PROVIDER" in
    codex)
      # The sandbox denies the keychain (com.apple.SecurityServer), which
      # Codex's TLS check asks for, so every request failed with "workspace
      # routing discovery failed". The system bundle verifies without it.
      AGENT_EXTRA_ENV="SSL_CERT_FILE=/etc/ssl/cert.pem"
      api_agent - codex exec --sandbox read-only --skip-git-repo-check --json \
        --model "$MODEL_ID" - > "$OUT" ;;
    claude)
      if [ -n "${LOOM_REWRITE_CLAUDE_TOKEN_SSM:-}" ]; then
        CLAUDE_CODE_OAUTH_TOKEN="$(ssm_value "$LOOM_REWRITE_CLAUDE_TOKEN_SSM")"
        [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] || fail "no Claude subscription token readable" 2
      fi
      CLAUDE_KEY_VAR=-
      [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || CLAUDE_KEY_VAR=CLAUDE_CODE_OAUTH_TOKEN
      API_KEY="${CLAUDE_CODE_OAUTH_TOKEN:-}" api_agent "$CLAUDE_KEY_VAR" claude -p --model "$MODEL_ID" --output-format stream-json \
        --verbose --tools "" --strict-mcp-config --disable-slash-commands \
        --setting-sources "" < "$AGENT_STDIN" > "$OUT" ;;
    free)
      # A free provider's route on the spark-gateway (LOOM_REWRITE_FREE_URL,
      # for example its free-public/gemini route for Gemma), keyless and
      # declared as opencode provider free, as the free lane declares it. Only
      # the public rewriter names one: bus/rewrite.command_for refuses a
      # private command with a free/ entry, as the provider may train on it.
      command -v "$BIN" > /dev/null || fail "$BIN not on PATH"
      # The gateway cools a free pool for about 5 minutes after a refusal, and
      # every rewrite in a burst then spent its whole free limit before the
      # next model answered (2026-10-21). A failed free entry starts a
      # cooldown (LOOM_REWRITE_FREE_COOLDOWN seconds, default 300) in which
      # later runs pass it over at once.
      COOL_FILE="$LOOM_HOME/state/rewrite-free.cooldown"
      COOL_UNTIL="$(tr -cd '0-9' < "$COOL_FILE" 2>/dev/null)"
      if [ -n "$COOL_UNTIL" ] && [ "$COOL_UNTIL" -gt "$(date +%s)" ]; then
        echo "the free pool is cooling down until $(date -u -r "$COOL_UNTIL" +%FT%TZ 2>/dev/null || echo "$COOL_UNTIL")" > "$OUT"
        false
      elif [[ "${LOOM_REWRITE_FREE_URL:-}" =~ ^https://[A-Za-z0-9.:_-]+(/[A-Za-z0-9._~/-]*)?$ ]]; then
        # A cooled-down free pool answers 503 with a retry-after and opencode
        # keeps retrying, so a free entry gets its own short limit
        # (LOOM_REWRITE_FREE_TIMEOUT, default 90 seconds) before the next
        # model: one hung the rewriter's whole 480 seconds (2026-10-21).
        FREE_TIMEOUT="${LOOM_REWRITE_FREE_TIMEOUT:-90}"
        [[ "$FREE_TIMEOUT" =~ ^[1-9][0-9]*$ ]] && [ "$FREE_TIMEOUT" -lt "$AGENT_TIMEOUT" ] || FREE_TIMEOUT="$AGENT_TIMEOUT"
        BASE_TIMEOUT="$AGENT_TIMEOUT"
        LANE=free SPARK_URL="$LOOM_REWRITE_FREE_URL" AGENT_TIMEOUT="$FREE_TIMEOUT"
        opencode_run - "$BIN" --format json > "$OUT"
        # shellcheck disable=SC2034 # read by the sourced agent_sandbox.sh
        LANE=rewrite SPARK_URL="" AGENT_TIMEOUT="$BASE_TIMEOUT"
      else
        echo "LOOM_REWRITE_FREE_URL is not an https URL" > "$OUT"
        false
      fi ;;
    *)
      command -v "$BIN" > /dev/null || fail "$BIN not on PATH"
      opencode_run "$KEY_VAR" "$BIN" --format json > "$OUT" ;;
  esac
  status=$?
  if [ "$PROVIDER" = free ] && [ -n "${COOL_UNTIL:-}" ] && [ "$COOL_UNTIL" -gt "$(date +%s)" ]; then
    reason="$(head -c 200 "$OUT")"
    REASONS="${REASONS:+$REASONS; }$MODEL: $reason"
    echo "rewrite_agent: passed over $MODEL: $reason" >&2
    continue
  fi
  if [ "$PROVIDER" = codex ] || [ "$PROVIDER" = claude ]; then
    reason="$(python3 -I -c "$LANE_EXTRACT" "$OUT" "$PROVIDER" 2>&1 > "$ANSWER_FILE")"
  else
    reason="$(python3 -I -c "$EXTRACT" "$OUT" 2>&1 > "$ANSWER_FILE")"
  fi
  extracted=$?
  if [ "$status" -eq 0 ] && [ "$extracted" -eq 0 ]; then
    ANSWER="$(cat "$ANSWER_FILE")" || fail "cannot read the answer"
    if [ -n "$API_KEY" ]; then
      case "$ANSWER" in *"$API_KEY"*) fail "the answer holds the provider key" ;; esac
    fi
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
      case "$ANSWER" in *"$CLAUDE_CODE_OAUTH_TOKEN"*) fail "the answer holds the subscription token" ;; esac
    fi
    [ "$PROVIDER" != free ] || rm -f "$COOL_FILE"
    # The station logs which model answered (bus/rewrite.run_agent).
    echo "rewrite_agent: answered by $MODEL" >&2
    printf '%s\n' "$ANSWER"
    exit 0
  fi
  if [ "$extracted" -ne 0 ] && [ -n "$API_KEY" ]; then
    reason="${reason//"$API_KEY"/[key]}"
  fi
  if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    reason="${reason//"$CLAUDE_CODE_OAUTH_TOKEN"/[token]}"
  fi
  lower="$(printf '%s' "$reason" | tr '[:upper:]' '[:lower:]')"
  if [[ "$status" -eq 124 || "$status" -eq 137 ]]; then
    why="timed out"
  elif [[ "$lower" == *"rate limit"* || "$lower" == *"rate-limit"* || "$lower" == *"overload"* || "$lower" == *"too many requests"* ]]; then
    why="rate limited or overloaded"
  elif [[ "$lower" == *"no text"* ]]; then
    why="$PROVIDER exited $status with no text"
  else
    why="$PROVIDER exited $status: ${reason:-no usable answer}"
  fi
  REASONS="${REASONS:+$REASONS; }$MODEL: $why"
  echo "rewrite_agent: passed over $MODEL: $why" >&2
  if [ "$PROVIDER" = free ]; then
    COOLDOWN="${LOOM_REWRITE_FREE_COOLDOWN:-300}"
    [[ "$COOLDOWN" =~ ^[0-9]+$ ]] || COOLDOWN=300
    mkdir -p "$LOOM_HOME/state" && printf '%s\n' "$(( $(date +%s) + COOLDOWN ))" > "$COOL_FILE"
  fi
done
fail "all models failed: $REASONS"
