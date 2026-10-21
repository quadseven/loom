#!/usr/bin/env bash
# Install the lanes (and the station) as launchd agents on the login machine.
#
#   lanes/mac/install.sh [lane ...]
#     default: codex claude openrouter opencode-go anthropic zen zai workersai agy spark,
#     plus the extra slots anthropic-2 to anthropic-8, codex-2, opencode-go-2
#     opencode-go-3 (which runs LOOM_OPENCODE_GO_3_MODEL) and opencode-go-4
#     (LOOM_OPENCODE_GO_4_MODEL, Muse Spark Contributor: public repos only)
#
# API lanes read their provider key from SSM at run time. Name each
# parameter in the environment when installing (LOOM_OPENROUTER_KEY_SSM,
# LOOM_OPENCODE_GO_KEY_SSM, LOOM_ANTHROPIC_KEY_SSM, LOOM_ZAI_KEY_SSM, optional
# LOOM_AWS_REGION; zen needs no key); they are written into the local plists
# only, never into this repo. A lane whose key is missing reports itself
# unavailable and receives no work. The spark lane needs no key: set
# LOOM_SPARK_URL (your vLLM server's OpenAI-compatible base URL, e.g.
# http://your-spark.internal:8000/v1) and LOOM_SPARK_MODEL (spark/<model id>);
# without LOOM_SPARK_URL it reports itself unavailable like a lane with no key.
#
# Each agent runs lanes/run_lane.sh from THIS checkout once a minute, so the
# code that runs is the code in git. No KeepAlive: a pass that exits is done,
# and the next tick starts a fresh one. Re-running replaces the agent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$REPO_ROOT/lanes/mac/lock.sh"
LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
# Lane default models, named in every PR. codex: the CLI's own configured
# default; claude: the current Opus. Override either in the environment.
LOOM_CODEX_MODEL="${LOOM_CODEX_MODEL:-$(sed -nE 's/^[[:space:]]*model[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$HOME/.codex/config.toml" 2>/dev/null | head -1)}"
if [ -z "${LOOM_REWRITE_PRIVATE_CMD:-}" ] && [ -n "$LOOM_CODEX_MODEL" ]; then
  LOOM_REWRITE_PRIVATE_CMD="lanes/rewrite_agent.sh - codex/$LOOM_CODEX_MODEL"
fi
LOOM_CLAUDE_MODEL="${LOOM_CLAUDE_MODEL:-claude-opus-5-5}"
LOOM_MUSE_MODEL="${LOOM_MUSE_MODEL:-muse-spark-1.3}"
LOOM_OPENROUTER_MODEL="${LOOM_OPENROUTER_MODEL:-openrouter/cohere/north-mini-code:free}"
LOOM_OPENCODE_GO_MODEL="${LOOM_OPENCODE_GO_MODEL:-opencode-go/kimi-k2.7-code}"
LOOM_OPENCODE_GO_3_MODEL="${LOOM_OPENCODE_GO_3_MODEL:-opencode-go/deepseek-v4.1-flash}"
LOOM_OPENCODE_GO_4_MODEL="${LOOM_OPENCODE_GO_4_MODEL:-opencode-go/muse-spark-1.3-contributor}"
# OpenCode Go's chat endpoint, asked for its usage limit before each run
# (opencode run retries a limit silently until the run times out).
LOOM_OPENCODE_GO_PROBE_URL="${LOOM_OPENCODE_GO_PROBE_URL:-https://opencode.ai/zen/go/v1/chat/completions}"
LOOM_OPENCODE_GO_PROBE_HEADER="${LOOM_OPENCODE_GO_PROBE_HEADER:-x-opencode-session}"
LOOM_ANTHROPIC_MODEL="${LOOM_ANTHROPIC_MODEL:-claude-sonnet-5-5}"
LOOM_ZEN_MODEL="${LOOM_ZEN_MODEL:-opencode/minimax-m3.1-flash-free}"
LOOM_ZAI_MODEL="${LOOM_ZAI_MODEL:-zai/glm-4.7-flash}"
LOOM_WORKERSAI_MODEL="${LOOM_WORKERSAI_MODEL:-cloudflare-workers-ai/@cf/qwen/qwen3.8-27b}"
LOOM_AGY_MODEL="${LOOM_AGY_MODEL:-gemini-3.8-flash-high}"
LOOM_AGY_EFFORT="${LOOM_AGY_EFFORT:-high}"
LOOM_SPARK_URL="${LOOM_SPARK_URL:-}"
# The other spark slots' boxes (spark-2, spark-3, spark-4); empty uses LOOM_SPARK_URL.
LOOM_SPARK_2_URL="${LOOM_SPARK_2_URL:-}"
LOOM_SPARK_3_URL="${LOOM_SPARK_3_URL:-}"
LOOM_SPARK_4_URL="${LOOM_SPARK_4_URL:-}"
# The free lane: one slot per provider in the spark-gateway's free pool
# (infra#5617), not Sparks. Each URL is that provider's pinned free route
# (/free/<provider>/v1 for free-ollama and free-gemma, /free-public/<provider>/v1
# for the rest); each model names its provider for the dashboard. The free
# models serve about 128K, so the lane's window is smaller than a Spark's.
LOOM_FREE_GATEWAY_URL="${LOOM_FREE_GATEWAY_URL:-}"
LOOM_FREE_OLLAMA_URL="${LOOM_FREE_OLLAMA_URL:-}"
LOOM_FREE_OLLAMA_MODEL="${LOOM_FREE_OLLAMA_MODEL:-free/ollama-cloud}"
LOOM_FREE_GEMMA_URL="${LOOM_FREE_GEMMA_URL:-}"
LOOM_FREE_GEMMA_MODEL="${LOOM_FREE_GEMMA_MODEL:-free/gemini}"
LOOM_FREE_NVIDIA_URL="${LOOM_FREE_NVIDIA_URL:-}"
LOOM_FREE_NVIDIA_MODEL="${LOOM_FREE_NVIDIA_MODEL:-free/nvidia-nim}"
LOOM_FREE_KILO_URL="${LOOM_FREE_KILO_URL:-}"
LOOM_FREE_KILO_MODEL="${LOOM_FREE_KILO_MODEL:-free/kilo}"
LOOM_FREE_GROQ_URL="${LOOM_FREE_GROQ_URL:-}"
LOOM_FREE_GROQ_MODEL="${LOOM_FREE_GROQ_MODEL:-free/groq}"
LOOM_FREE_CONTEXT="${LOOM_FREE_CONTEXT:-120000}"
LOOM_FREE_OUTPUT="${LOOM_FREE_OUTPUT:-16384}"
LOOM_SPARK_MODEL="${LOOM_SPARK_MODEL:-}"
# The context and output window the spark endpoint serves (lanes/agent_sandbox.sh).
LOOM_SPARK_CONTEXT="${LOOM_SPARK_CONTEXT:-32768}"
LOOM_SPARK_OUTPUT="${LOOM_SPARK_OUTPUT:-4096}"
export LOOM_CODEX_MODEL LOOM_CLAUDE_MODEL LOOM_OPENROUTER_MODEL LOOM_OPENCODE_GO_MODEL LOOM_OPENCODE_GO_3_MODEL LOOM_OPENCODE_GO_4_MODEL \
  LOOM_OPENCODE_GO_PROBE_URL LOOM_OPENCODE_GO_PROBE_HEADER LOOM_ANTHROPIC_MODEL \
  LOOM_ZEN_MODEL LOOM_ZAI_MODEL LOOM_WORKERSAI_MODEL LOOM_AGY_MODEL LOOM_AGY_EFFORT LOOM_SPARK_URL LOOM_SPARK_2_URL LOOM_SPARK_3_URL LOOM_SPARK_4_URL LOOM_SPARK_MODEL \
  LOOM_FREE_GATEWAY_URL LOOM_FREE_OLLAMA_URL LOOM_FREE_OLLAMA_MODEL LOOM_FREE_GEMMA_URL LOOM_FREE_GEMMA_MODEL LOOM_FREE_NVIDIA_URL LOOM_FREE_NVIDIA_MODEL LOOM_FREE_KILO_URL LOOM_FREE_KILO_MODEL LOOM_FREE_GROQ_URL LOOM_FREE_GROQ_MODEL LOOM_FREE_CONTEXT LOOM_FREE_OUTPUT \
  LOOM_SPARK_CONTEXT LOOM_SPARK_OUTPUT
# Briefs may only name repos of these owners. Default: this checkout's owner.
LOOM_REPO_OWNERS="${LOOM_REPO_OWNERS:-$(git -C "$REPO_ROOT" remote get-url origin | sed -E 's#^.*[:/]([^/]+)/[^/]+$#\1#')}"
# Checked before anything is installed.
# The status repo (the brain's loom-lock/brain branch and the loom:lanes
# issue; default this checkout's origin) and every LOOM_REPOS repo must belong
# to one of LOOM_REPO_OWNERS, or a second operator's Loom takes the first
# operator's brain lock and writes to their lanes issue (docs/LANES.md, Two
# Looms on shared repos). Owners compare case-insensitively, as the station does.
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
OWNER_LIST=" $(lower "$LOOM_REPO_OWNERS" | tr ',\t\n' '   ') "
owned() { [ -n "$1" ] && [[ "$OWNER_LIST" == *" $(lower "$1") "* ]]; }
read -r -a WATCHED <<<"$(printf '%s' "${LOOM_REPOS:-}" | tr ',\t\n' '   ')" || true
if [ "${#WATCHED[@]}" -gt 0 ]; then
  STATUS_REPO="${LOOM_STATUS_REPO:-}"
  if [ -n "$STATUS_REPO" ]; then
    STATUS_FROM="LOOM_STATUS_REPO is $STATUS_REPO"
  else
    STATUS_REPO="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null |
      sed -E 's#\.git$##; s#^.*[:/]([^/]+/[^/]+)$#\1#' || true)"
    STATUS_FROM="LOOM_STATUS_REPO is unset, so the status repo is this checkout's origin (${STATUS_REPO:-none})"
  fi
  owned "${STATUS_REPO%%/*}" ||
    { echo "$STATUS_FROM, whose owner is not in LOOM_REPO_OWNERS ($LOOM_REPO_OWNERS): set LOOM_STATUS_REPO to a repo of your own (docs/LANES.md, Two Looms on shared repos)" >&2; exit 1; }
  for R in "${WATCHED[@]}"; do
    owned "${R%%/*}" ||
      { echo "LOOM_REPOS names $R, whose owner is not in LOOM_REPO_OWNERS ($LOOM_REPO_OWNERS)" >&2; exit 1; }
  done
fi
BRAIN_INTERVAL="${LOOM_BRAIN_INTERVAL:-1800}"
[[ "$BRAIN_INTERVAL" =~ ^[0-9]+$ ]] && [ "$BRAIN_INTERVAL" -ge 60 ] ||
  { echo "LOOM_BRAIN_INTERVAL must be a number of seconds, at least 60" >&2; exit 1; }
STATION_INTERVAL="${LOOM_STATION_INTERVAL:-60}"
[[ "$STATION_INTERVAL" =~ ^[0-9]+$ ]] && [ "$STATION_INTERVAL" -ge 60 ] ||
  { echo "LOOM_STATION_INTERVAL must be a number of seconds, at least 60" >&2; exit 1; }
# A GitHub App needs all of its settings or none: with only some set, its
# writer would quietly fall back to the personal token (bus/github.py
# AppToken.from_env), so refuse and name what is missing. Each writer has its
# own App (loom#1142): the station LOOM_GITHUB_APP_*, the lanes
# LOOM_LANES_GITHUB_APP_* and the brain LOOM_BRAIN_GITHUB_APP_*. A key file
# must exist and be readable by its owner only (mode 600 or narrower), like
# an ssh key.
check_app() {
  local p="LOOM_${1}GITHUB_APP_" id inst file ssm mode
  id="${p}ID" inst="${p}INSTALLATION_ID" file="${p}KEY_FILE" ssm="${p}KEY_SSM"
  [ -n "${!id:-}${!inst:-}${!file:-}${!ssm:-}" ] || return 0
  [ -n "${!id:-}" ] ||
    { echo "GitHub App half set up: $id is missing (set all App settings or none)" >&2; exit 1; }
  [ -n "${!inst:-}" ] ||
    { echo "GitHub App half set up: $inst is missing (set all App settings or none)" >&2; exit 1; }
  [ -n "${!file:-}${!ssm:-}" ] ||
    { echo "GitHub App half set up: $file or $ssm is missing (set all App settings or none)" >&2; exit 1; }
  [ -n "${!file:-}" ] || return 0
  [ -f "${!file}" ] && [ -r "${!file}" ] ||
    { echo "$file ${!file} is not a file this user can read" >&2; exit 1; }
  # GNU stat first: BSD stat rejects -c, while GNU stat accepts -f with another meaning.
  mode="$(stat -c %a "${!file}" 2>/dev/null || stat -f %Lp "${!file}" 2>/dev/null)" || mode=""
  [[ "$mode" =~ ^[0-7]+$ ]] ||
    { echo "$file ${!file}: could not read its mode with stat" >&2; exit 1; }
  [ $((8#$mode & 8#077)) -eq 0 ] ||
    { echo "$file ${!file} has mode $mode; it must be 600 (chmod 600 ${!file})" >&2; exit 1; }
}
check_app ""
check_app LANES_
check_app BRAIN_
DASHBOARD_PORT="${LOOM_DASHBOARD_PORT:-8080}"
DASHBOARD_HOST="${LOOM_DASHBOARD_HOST:-}"
if [ "${LOOM_DASHBOARD:-}" = 1 ]; then
  [[ "$DASHBOARD_PORT" =~ ^[0-9]+$ ]] ||
    { echo "LOOM_DASHBOARD_PORT must be a port number" >&2; exit 1; }
  # Empty (every interface) or one IPv4 address, each part 0-255 with no
  # leading zero (Python's ipaddress, which the dashboard checks with, refuses one).
  if [ -n "$DASHBOARD_HOST" ]; then
    ok=0
    if [[ "$DASHBOARD_HOST" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
      ok=1
      for part in "${BASH_REMATCH[@]:1}"; do
        case "$part" in 0?*) ok=0 ;; *) [ "$part" -le 255 ] || ok=0 ;; esac
      done
    fi
    [ "$ok" = 1 ] ||
      { echo "LOOM_DASHBOARD_HOST must be empty or one IPv4 address" >&2; exit 1; }
  fi
fi
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$LOOM_HOME/logs"

LANES=("$@")
[ "${#LANES[@]}" -gt 0 ] || LANES=(codex claude openrouter opencode-go anthropic zen zai workersai agy spark
  anthropic-2 anthropic-3 anthropic-4 anthropic-5 anthropic-6 anthropic-7 anthropic-8
  codex-2 opencode-go-2 opencode-go-3 opencode-go-4 spark-2 spark-3 spark-4
  free-ollama free-gemma free-nvidia free-kilo free-groq)

# PATH puts the official CLI install dirs (~/.local/bin for claude,
# ~/.opencode/bin for opencode) ahead of package-manager copies, which can lag
# behind and break (a Homebrew opencode crashed on start, 2026-10-11). The
# station's PATH holds ~/.opencode/bin too, for lanes/rewrite_agent.sh.

# XML-escape a value for a plist <string>.
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
X_REPO="$(xml "$REPO_ROOT")" X_LOOM="$(xml "$LOOM_HOME")" X_HOME="$(xml "$HOME")"

# Every LOOM_* setting in the environment, as plist entries, less the names
# given as arguments (the ones a plist writes itself: a key twice is ambiguous).
loom_env() {
  local skip=" $* "
  env | LC_ALL=C sort | while IFS='=' read -r k v; do
    case "$k" in
      LOOM_HOME|LOOM_REPO_OWNERS|LOOM_EVIDENCE_CMDS) continue ;;
      # Each job gets its own writer's App from app_env, never another's.
      LOOM_GITHUB_APP_*|LOOM_LANES_GITHUB_APP_*|LOOM_BRAIN_GITHUB_APP_*) continue ;;
      LOOM_*) ;; *) continue ;;
    esac
    case "$skip" in *" $k "*) continue ;; esac
    v="$(xml "$v")"
    printf '    <key>%s</key><string>%s</string>\n' "$k" "$v"
  done
}

# One writer's App settings as the job's LOOM_GITHUB_APP_*: $1 is "" for the
# station's, LANES_ or BRAIN_. The brain keeps the station's App until it has
# its own, so a reinstall never moves it back to the personal token.
app_env() {
  local p="LOOM_${1}GITHUB_APP_" n v
  [ "$1" = BRAIN_ ] && [ -z "${LOOM_BRAIN_GITHUB_APP_ID:-}" ] && p=LOOM_GITHUB_APP_
  for n in ID INSTALLATION_ID KEY_SSM KEY_FILE; do
    v="${p}$n"
    [ -n "${!v:-}" ] || continue
    printf '    <key>LOOM_GITHUB_APP_%s</key><string>%s</string>\n' "$n" "$(xml "${!v}")"
  done
}

# Each lane's model settings (LOOM_<LANE>_MODEL and LOOM_<LANE>_MODELS), for
# the station: its factory reads what a lane ran from them when a claim had
# no Model: line and no runner footer names it (bus/brain.py lane_model).
model_env() {
  env | LC_ALL=C sort | while IFS='=' read -r k v; do
    case "$k" in LOOM_BRAIN_MODEL) continue ;; LOOM_*_MODEL|LOOM_*_MODELS) ;; *) continue ;; esac
    printf '    <key>%s</key><string>%s</string>\n' "$k" "$(xml "$v")"
  done
}

for L in "${LANES[@]}"; do
  LABEL="com.loom.$L"
  if runner_busy "$LOOM_HOME/run/$L.lock"; then
    echo "skipped $LABEL: runner busy, re-run the installer later"
    continue
  fi
  PLIST="$AGENTS/$LABEL.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$X_REPO/lanes/run_lane.sh</string>
    <string>$L</string>
  </array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$X_LOOM/logs/$L-service.log</string>
  <key>StandardErrorPath</key><string>$X_LOOM/logs/$L-service.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$X_HOME/.local/bin:$X_HOME/.opencode/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$X_HOME</string>
    <key>LOOM_HOME</key><string>$X_LOOM</string>
    <key>LOOM_REPO_OWNERS</key><string>$(xml "$LOOM_REPO_OWNERS")</string>
$(loom_env)
$(app_env LANES_)
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  wait_job_gone "gui/$(id -u)/$LABEL"
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $LABEL -> $X_REPO/lanes/run_lane.sh $L"
done

# The station: one pass every LOOM_STATION_INTERVAL seconds (default 60) that
# turns briefed ticket issues into local briefs and reports outcomes
# (docs/BRAIN.md). It reads LOOM_REPOS,
# LOOM_BRIEF_AUTHORS and LOOM_REPO_OWNERS from the installing environment;
# with LOOM_REPOS empty it stays idle. LOOM_TRUSTED_BRIEFERS (default empty)
# names another operator's logins whose loom:ticket briefs it builds without
# adopting their claims (docs/LANES.md, Trusted briefers). It also reads LOOM_STATUS_REPO (default:
# this checkout's origin), LOOM_DISCORD_WEBHOOK_SSM, LOOM_DISCORD_MENTION,
# LOOM_DIGEST_HOUR, LOOM_REPORT_DIGEST_TIMES (local times of the report digest,
# default 10:00,21:00; empty sends each report), LOOM_AWS_REGION, LOOM_RETRY_MAX (automatic retries per
# ticket, default 2), and LOOM_DISCORD_CHANNEL_ID,
# LOOM_DISCORD_BOT_TOKEN_SSM and LOOM_DISCORD_DEFAULT_GOAL (talking to the
# brain from Discord, docs/LANES.md), and LOOM_ANTHROPIC_BUDGET_USD,
# LOOM_ANTHROPIC_BUDGET_SINCE, LOOM_ANTHROPIC_BUDGET_EXPIRES and
# LOOM_ANTHROPIC_FLOOR_USD (the anthropic spend line; the lanes get them with
# every other LOOM_* setting), and LOOM_NO_TEST_REPOS (repos the factory
# lands code changes in without a test, docs/LANES.md), and LOOM_TRANSCRIPT_URL
# and LOOM_TRANSCRIPT_TOKEN_SSM (where finished transcripts are uploaded and
# the SSM parameter holding its bearer token, docs/LANES.md; both come from
# the installing environment, never this repo), and LOOM_WATCH (the logs the
# station watches for new repeated errors, lines of name=path; empty watches
# the station, brain and lane service logs, docs/LANES.md), and
# LOOM_REWRITE_CMD and LOOM_REWRITE_PRIVATE_CMD (the agent commands that
# rewrite loom:triage issues into tickets, docs/BRAIN.md Triage; for example
# "lanes/rewrite_agent.sh <key-ssm> opencode-go/deepseek-v4.1-flash"; private
# repos default to the codex subscription when LOOM_CODEX_MODEL is set), and
# LOOM_SELF_REPO (the repo this checkout deploys from, default LOOM_STATUS_REPO:
# its merged tickets move to loom:shipped once a deploy carries them), and
# LOOM_PIN_TARGETS (private JSON array of deployment pin targets), and
# LOOM_ROLL_SHIP_DOCS (goal ship documents for module source pins), and
# LOOM_PIN_SHIP_REPOS (deployment repos whose merged pin pull requests ship
# the tickets they deploy, docs/BRAIN.md), and
# LOOM_SHIP_ON_MERGE and LOOM_SHIP_WORKFLOWS (repos whose merged tickets ship
# at merge, and OWNER/NAME=WORKFLOW_FILE repos that ship when their deploy
# workflow succeeds on the merge commit, docs/BRAIN.md), and
# LOOM_EVIDENCE_CMDS (private JSON map of repo to read-only log command for
# verify briefs, docs/LANES.md Verify runs), and
# LOOM_LOG_KEEP_DAYS and LOOM_DISK_FREE_MIN_GB (days a lane's logs are kept,
# default 7, 0 forever; and the free disk under which every pass warns,
# default 10 GB, docs/LANES.md Disk), and
# LOOM_INVARIANT_GOAL and LOOM_PLAN_CHECK (the goal home invariant breaches
# are filed on, and the review bot's plan check, docs/LANES.md Invariants), and
# LOOM_NO_TOPUP (provider lanes whose credit is never topped up: their slots
# held out of credit are no lane-held breach, docs/LANES.md Invariants), and
# LOOM_SETTLE_CLAIMED and LOOM_SETTLE_CLAIMED_MAX (settle operator-claimed
# issues whose pull requests merged: empty or dry logs only, on writes, off
# skips; and the most settled a run, default 10, docs/BRAIN.md), and
# LOOM_GITHUB_APP_ID, LOOM_GITHUB_APP_INSTALLATION_ID and LOOM_GITHUB_APP_KEY_SSM
# or LOOM_GITHUB_APP_KEY_FILE (a GitHub App installation token instead of the
# personal one, minted from the key in that SSM parameter or that mode-600
# file, docs/LANES.md; all or none, checked above). Runs from
# this checkout, like the lanes, through
# lanes/mac/station.sh, which first fast-forwards the checkout to its default
# branch once that head passed CI (LOOM_DEPLOY=0 turns it off).
LABEL="com.loom.station"
PLIST="$AGENTS/$LABEL.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$X_REPO/lanes/mac/station.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$X_REPO</string>
  <key>StartInterval</key><integer>$STATION_INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$X_LOOM/logs/station.log</string>
  <key>StandardErrorPath</key><string>$X_LOOM/logs/station.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$X_HOME/.local/bin:$X_HOME/.opencode/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$X_HOME</string>
    <key>LOOM_HOME</key><string>$X_LOOM</string>
    <key>LOOM_REPOS</key><string>$(xml "${LOOM_REPOS:-}")</string>
    <key>LOOM_REWRITE_OPENCODE</key><string>$(xml "${LOOM_REWRITE_OPENCODE:-$HOME/.opencode/bin/opencode}")</string>
    <key>LOOM_DEPLOY</key><string>$(xml "${LOOM_DEPLOY:-1}")</string>
    <key>LOOM_BRIEF_AUTHORS</key><string>$(xml "${LOOM_BRIEF_AUTHORS:-}")</string>
    <key>LOOM_TRUSTED_BRIEFERS</key><string>$(xml "${LOOM_TRUSTED_BRIEFERS:-}")</string>
    <key>LOOM_REPO_OWNERS</key><string>$(xml "$LOOM_REPO_OWNERS")</string>
    <key>LOOM_STATUS_REPO</key><string>$(xml "${LOOM_STATUS_REPO:-}")</string>
    <key>LOOM_SELF_REPO</key><string>$(xml "${LOOM_SELF_REPO:-}")</string>
    <key>LOOM_PIN_TARGETS</key><string>$(xml "${LOOM_PIN_TARGETS:-}")</string>
    <key>LOOM_ROLL_SHIP_DOCS</key><string>$(xml "${LOOM_ROLL_SHIP_DOCS:-}")</string>
    <key>LOOM_PIN_SHIP_REPOS</key><string>$(xml "${LOOM_PIN_SHIP_REPOS:-}")</string>
    <key>LOOM_SHIP_ON_MERGE</key><string>$(xml "${LOOM_SHIP_ON_MERGE:-}")</string>
    <key>LOOM_SHIP_WORKFLOWS</key><string>$(xml "${LOOM_SHIP_WORKFLOWS:-}")</string>
    <key>LOOM_LOG_KEEP_DAYS</key><string>$(xml "${LOOM_LOG_KEEP_DAYS:-}")</string>
    <key>LOOM_DISK_FREE_MIN_GB</key><string>$(xml "${LOOM_DISK_FREE_MIN_GB:-}")</string>
    <key>LOOM_SETTLE_CLAIMED</key><string>$(xml "${LOOM_SETTLE_CLAIMED:-}")</string>
    <key>LOOM_SETTLE_CLAIMED_MAX</key><string>$(xml "${LOOM_SETTLE_CLAIMED_MAX:-}")</string>
    <key>LOOM_DISCORD_WEBHOOK_SSM</key><string>$(xml "${LOOM_DISCORD_WEBHOOK_SSM:-}")</string>
    <key>LOOM_DISCORD_MENTION</key><string>$(xml "${LOOM_DISCORD_MENTION:-}")</string>
    <key>LOOM_DIGEST_HOUR</key><string>$(xml "${LOOM_DIGEST_HOUR:-}")</string>
    <key>LOOM_REPORT_DIGEST_TIMES</key><string>$(xml "${LOOM_REPORT_DIGEST_TIMES-10:00,21:00}")</string>
    <key>LOOM_METRICS_GOAL</key><string>$(xml "${LOOM_METRICS_GOAL:-}")</string>
    <key>LOOM_AWS_REGION</key><string>$(xml "${LOOM_AWS_REGION:-}")</string>
    <key>LOOM_RETRY_MAX</key><string>$(xml "${LOOM_RETRY_MAX:-}")</string>
    <key>LOOM_DISCORD_CHANNEL_ID</key><string>$(xml "${LOOM_DISCORD_CHANNEL_ID:-}")</string>
    <key>LOOM_DISCORD_BOT_TOKEN_SSM</key><string>$(xml "${LOOM_DISCORD_BOT_TOKEN_SSM:-}")</string>
    <key>LOOM_DISCORD_DEFAULT_GOAL</key><string>$(xml "${LOOM_DISCORD_DEFAULT_GOAL:-}")</string>
    <key>LOOM_ANTHROPIC_BUDGET_USD</key><string>$(xml "${LOOM_ANTHROPIC_BUDGET_USD:-}")</string>
    <key>LOOM_ANTHROPIC_BUDGET_SINCE</key><string>$(xml "${LOOM_ANTHROPIC_BUDGET_SINCE:-}")</string>
    <key>LOOM_ANTHROPIC_BUDGET_EXPIRES</key><string>$(xml "${LOOM_ANTHROPIC_BUDGET_EXPIRES:-}")</string>
    <key>LOOM_ANTHROPIC_FLOOR_USD</key><string>$(xml "${LOOM_ANTHROPIC_FLOOR_USD:-}")</string>
    <key>LOOM_NO_TEST_REPOS</key><string>$(xml "${LOOM_NO_TEST_REPOS:-}")</string>
    <key>LOOM_TRANSCRIPT_URL</key><string>$(xml "${LOOM_TRANSCRIPT_URL:-}")</string>
    <key>LOOM_TRANSCRIPT_TOKEN_SSM</key><string>$(xml "${LOOM_TRANSCRIPT_TOKEN_SSM:-}")</string>
    <key>LOOM_WATCH</key><string>$(xml "${LOOM_WATCH:-}")</string>
    <key>LOOM_EVIDENCE_CMDS</key><string>$(xml "${LOOM_EVIDENCE_CMDS:-}")</string>
    <key>LOOM_REWRITE_CMD</key><string>$(xml "${LOOM_REWRITE_CMD:-}")</string>
    <key>LOOM_REWRITE_PRIVATE_CMD</key><string>$(xml "${LOOM_REWRITE_PRIVATE_CMD:-}")</string>
    <key>LOOM_REWRITE_CLAUDE_TOKEN_SSM</key><string>$(xml "${LOOM_REWRITE_CLAUDE_TOKEN_SSM:-}")</string>
    <key>LOOM_INVARIANT_GOAL</key><string>$(xml "${LOOM_INVARIANT_GOAL:-}")</string>
    <key>LOOM_PLAN_CHECK</key><string>$(xml "${LOOM_PLAN_CHECK:-}")</string>
    <key>LOOM_NO_TOPUP</key><string>$(xml "${LOOM_NO_TOPUP:-}")</string>
    <key>LOOM_GITHUB_APP_ID</key><string>$(xml "${LOOM_GITHUB_APP_ID:-}")</string>
    <key>LOOM_GITHUB_APP_INSTALLATION_ID</key><string>$(xml "${LOOM_GITHUB_APP_INSTALLATION_ID:-}")</string>
    <key>LOOM_GITHUB_APP_KEY_SSM</key><string>$(xml "${LOOM_GITHUB_APP_KEY_SSM:-}")</string>
    <key>LOOM_GITHUB_APP_KEY_FILE</key><string>$(xml "${LOOM_GITHUB_APP_KEY_FILE:-}")</string>
$(model_env)
  </dict>
</dict>
</plist>
EOF
# Never boot the station out mid-pass: a report sent but not yet recorded
# would go to Discord twice (bus/station.py persists the ledger per send).
# Bounded: after the bound it installs anyway, saying so.
wait_station_free "$LOOM_HOME/state/station.lock" || true
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
wait_job_gone "gui/$(id -u)/$LABEL"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL -> lanes/mac/station.sh (deploys main, then python3 -m bus station; idle unless LOOM_REPOS is set)"

# The brain tick (lanes/mac/brain.sh): every LOOM_BRAIN_INTERVAL seconds
# (default 1800), take the brain lock and run one docs/MUSE.md tick on the
# first lane in LOOM_BRAIN_LADDER (default claude codex spark) that has
# usage. It reads LOOM_REPOS, LOOM_STATUS_REPO, LOOM_BRIEF_AUTHORS,
# LOOM_TRUSTED_BRIEFERS,
# LOOM_AWS_REGION, LOOM_BRAIN_*, the rungs' LOOM_CODEX_MODEL,
# LOOM_SPARK_MODEL and LOOM_SPARK_URL, and LOOM_TRANSCRIPT_URL and
# LOOM_TRANSCRIPT_TOKEN_SSM (for the handoff packet) from the installing
# environment; with LOOM_REPOS empty it stays idle. LOOM_BRAIN=0 skips it and
# removes an installed one.
LABEL="com.loom.brain"
PLIST="$AGENTS/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
wait_job_gone "gui/$(id -u)/$LABEL"
if [ "${LOOM_BRAIN:-1}" = 0 ]; then
  rm -f "$PLIST" "$LOOM_HOME/state/brain.json" "$LOOM_HOME/state/brain-ok"
  echo "removed $LABEL (LOOM_BRAIN=0)"
else
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$X_REPO/lanes/mac/brain.sh</string>
  </array>
  <key>StartInterval</key><integer>$BRAIN_INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$X_LOOM/logs/brain-service.log</string>
  <key>StandardErrorPath</key><string>$X_LOOM/logs/brain-service.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$X_HOME/.local/bin:$X_HOME/.opencode/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$X_HOME</string>
    <key>LOOM_HOME</key><string>$X_LOOM</string>
$(loom_env)
$(app_env BRAIN_)
  </dict>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $LABEL -> $X_REPO/lanes/mac/brain.sh"
fi

# The transcript viewer: a long-running server on localhost, restarted if it
# exits. Installed alongside the lanes unless LOOM_VIEWER=0.
if [ "${LOOM_VIEWER:-1}" != 0 ]; then
  VIEWER_PORT="${LOOM_VIEWER_PORT:-7340}"
  LABEL="com.loom.viewer"
  PLIST="$AGENTS/$LABEL.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$X_REPO/lanes/viewer/serve.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$X_LOOM/logs/viewer.log</string>
  <key>StandardErrorPath</key><string>$X_LOOM/logs/viewer.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$X_HOME</string>
    <key>LOOM_HOME</key><string>$X_LOOM</string>
    <key>LOOM_VIEWER_PORT</key><string>$(xml "$VIEWER_PORT")</string>
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  wait_job_gone "gui/$(id -u)/$LABEL"
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $LABEL -> http://localhost:$VIEWER_PORT/"
fi

# The dashboard (docs/DASHBOARD.md): python3 -m dashboard from this checkout,
# a long-running server restarted if it exits, like the viewer.
# LOOM_DASHBOARD=1 installs it, LOOM_DASHBOARD=0 removes an installed one, and
# unset leaves it as it is. It gets every LOOM_* setting of the installing
# environment. LOOM_DASHBOARD_HOST is the one address it listens on (empty:
# every interface; give the private-network address), LOOM_DASHBOARD_PORT its
# port (default 8080). With no LOOM_DB_PASSWORD and no LOOM_TRANSCRIPTS_MEMORY
# it keeps transcripts in memory (LOOM_TRANSCRIPTS_MEMORY=1) instead of
# failing on the missing database, and its log says so.
LABEL="com.loom.dashboard"
PLIST="$AGENTS/$LABEL.plist"
case "${LOOM_DASHBOARD:-}" in
0)
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  wait_job_gone "gui/$(id -u)/$LABEL"
  rm -f "$PLIST"
  echo "removed $LABEL (LOOM_DASHBOARD=0)"
  ;;
1)
  DASHBOARD_MEMORY="${LOOM_TRANSCRIPTS_MEMORY:-}"
  if [ -z "$DASHBOARD_MEMORY" ] && [ -z "${LOOM_DB_PASSWORD:-}" ]; then
    DASHBOARD_MEMORY=1
    echo "$LABEL: no LOOM_DB_PASSWORD, so transcripts are kept in memory (LOOM_TRANSCRIPTS_MEMORY=1)"
  fi
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>-m</string>
    <string>dashboard</string>
  </array>
  <key>WorkingDirectory</key><string>$X_REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$X_LOOM/logs/dashboard.log</string>
  <key>StandardErrorPath</key><string>$X_LOOM/logs/dashboard.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$X_HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$X_HOME</string>
    <key>LOOM_HOME</key><string>$X_LOOM</string>
    <key>LOOM_DASHBOARD_HOST</key><string>$(xml "$DASHBOARD_HOST")</string>
    <key>LOOM_DASHBOARD_PORT</key><string>$(xml "$DASHBOARD_PORT")</string>
    <key>LOOM_TRANSCRIPTS_MEMORY</key><string>$(xml "$DASHBOARD_MEMORY")</string>
$(loom_env LOOM_DASHBOARD LOOM_DASHBOARD_HOST LOOM_DASHBOARD_PORT LOOM_TRANSCRIPTS_MEMORY)
$(app_env "")
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  wait_job_gone "gui/$(id -u)/$LABEL"
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $LABEL -> http://${DASHBOARD_HOST:-localhost}:$DASHBOARD_PORT/ (listening on ${DASHBOARD_HOST:-every interface})"
  ;;
esac
