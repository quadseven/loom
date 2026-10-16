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
LOOM_OPENROUTER_MODEL="${LOOM_OPENROUTER_MODEL:-openrouter/poolside/laguna-s-2.1:free}"
LOOM_OPENCODE_GO_MODEL="${LOOM_OPENCODE_GO_MODEL:-opencode-go/kimi-k2.7-code}"
LOOM_OPENCODE_GO_3_MODEL="${LOOM_OPENCODE_GO_3_MODEL:-opencode-go/deepseek-v4.1-flash}"
LOOM_OPENCODE_GO_4_MODEL="${LOOM_OPENCODE_GO_4_MODEL:-opencode-go/muse-spark-1.3-contributor}"
# OpenCode Go's chat endpoint, asked for its usage limit before each run
# (opencode run retries a limit silently until the run times out).
LOOM_OPENCODE_GO_PROBE_URL="${LOOM_OPENCODE_GO_PROBE_URL:-https://opencode.ai/zen/go/v1/chat/completions}"
LOOM_OPENCODE_GO_PROBE_HEADER="${LOOM_OPENCODE_GO_PROBE_HEADER:-x-opencode-session}"
LOOM_ANTHROPIC_MODEL="${LOOM_ANTHROPIC_MODEL:-claude-sonnet-5-5}"
LOOM_ZEN_MODEL="${LOOM_ZEN_MODEL:-opencode/space-bunny-free}"
LOOM_ZAI_MODEL="${LOOM_ZAI_MODEL:-zai/glm-4.7-flash}"
LOOM_WORKERSAI_MODEL="${LOOM_WORKERSAI_MODEL:-cloudflare-workers-ai/@cf/qwen/qwen3.8-27b}"
LOOM_AGY_MODEL="${LOOM_AGY_MODEL:-gemini-3.8-flash-high}"
LOOM_SPARK_URL="${LOOM_SPARK_URL:-}"
# The other spark slots' boxes (spark-2, spark-3, spark-4); empty uses LOOM_SPARK_URL.
LOOM_SPARK_2_URL="${LOOM_SPARK_2_URL:-}"
LOOM_SPARK_3_URL="${LOOM_SPARK_3_URL:-}"
LOOM_SPARK_4_URL="${LOOM_SPARK_4_URL:-}"
LOOM_SPARK_MODEL="${LOOM_SPARK_MODEL:-}"
# The context and output window the spark endpoint serves (lanes/agent_sandbox.sh).
LOOM_SPARK_CONTEXT="${LOOM_SPARK_CONTEXT:-32768}"
LOOM_SPARK_OUTPUT="${LOOM_SPARK_OUTPUT:-4096}"
export LOOM_CODEX_MODEL LOOM_CLAUDE_MODEL LOOM_OPENROUTER_MODEL LOOM_OPENCODE_GO_MODEL LOOM_OPENCODE_GO_3_MODEL LOOM_OPENCODE_GO_4_MODEL \
  LOOM_OPENCODE_GO_PROBE_URL LOOM_OPENCODE_GO_PROBE_HEADER LOOM_ANTHROPIC_MODEL \
  LOOM_ZEN_MODEL LOOM_ZAI_MODEL LOOM_WORKERSAI_MODEL LOOM_AGY_MODEL LOOM_SPARK_URL LOOM_SPARK_2_URL LOOM_SPARK_3_URL LOOM_SPARK_4_URL LOOM_SPARK_MODEL \
  LOOM_SPARK_CONTEXT LOOM_SPARK_OUTPUT
# Briefs may only name repos of these owners. Default: this checkout's owner.
LOOM_REPO_OWNERS="${LOOM_REPO_OWNERS:-$(git -C "$REPO_ROOT" remote get-url origin | sed -E 's#^.*[:/]([^/]+)/[^/]+$#\1#')}"
# Checked before anything is installed.
BRAIN_INTERVAL="${LOOM_BRAIN_INTERVAL:-1800}"
[[ "$BRAIN_INTERVAL" =~ ^[0-9]+$ ]] && [ "$BRAIN_INTERVAL" -ge 60 ] ||
  { echo "LOOM_BRAIN_INTERVAL must be a number of seconds, at least 60" >&2; exit 1; }
STATION_INTERVAL="${LOOM_STATION_INTERVAL:-60}"
[[ "$STATION_INTERVAL" =~ ^[0-9]+$ ]] && [ "$STATION_INTERVAL" -ge 60 ] ||
  { echo "LOOM_STATION_INTERVAL must be a number of seconds, at least 60" >&2; exit 1; }
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$LOOM_HOME/logs"

LANES=("$@")
[ "${#LANES[@]}" -gt 0 ] || LANES=(codex claude openrouter opencode-go anthropic zen zai workersai agy spark
  anthropic-2 anthropic-3 anthropic-4 anthropic-5 anthropic-6 anthropic-7 anthropic-8
  codex-2 opencode-go-2 opencode-go-3 opencode-go-4 spark-2 spark-3 spark-4)

# PATH puts the official CLI install dirs (~/.local/bin for claude,
# ~/.opencode/bin for opencode) ahead of package-manager copies, which can lag
# behind and break (a Homebrew opencode crashed on start, 2026-10-11). The
# station's PATH holds ~/.opencode/bin too, for lanes/rewrite_agent.sh.

# XML-escape a value for a plist <string>.
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
X_REPO="$(xml "$REPO_ROOT")" X_LOOM="$(xml "$LOOM_HOME")" X_HOME="$(xml "$HOME")"

# Every LOOM_* setting in the environment, as plist entries.
loom_env() {
  env | LC_ALL=C sort | while IFS='=' read -r k v; do
    case "$k" in LOOM_HOME|LOOM_REPO_OWNERS|LOOM_EVIDENCE_CMDS) continue ;; LOOM_*) ;; *) continue ;; esac
    v="$(xml "$v")"
    printf '    <key>%s</key><string>%s</string>\n' "$k" "$v"
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
# with LOOM_REPOS empty it stays idle. It also reads LOOM_STATUS_REPO (default:
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
# LOOM_EVIDENCE_CMDS (private JSON map of repo to read-only log command for
# verify briefs, docs/LANES.md Verify runs), and
# LOOM_INVARIANT_GOAL and LOOM_PLAN_CHECK (the goal home invariant breaches
# are filed on, and the review bot's plan check, docs/LANES.md Invariants). Runs from
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
    <key>LOOM_REPO_OWNERS</key><string>$(xml "$LOOM_REPO_OWNERS")</string>
    <key>LOOM_STATUS_REPO</key><string>$(xml "${LOOM_STATUS_REPO:-}")</string>
    <key>LOOM_SELF_REPO</key><string>$(xml "${LOOM_SELF_REPO:-}")</string>
    <key>LOOM_PIN_TARGETS</key><string>$(xml "${LOOM_PIN_TARGETS:-}")</string>
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
# (default 1800), take the brain lock and run one docs/MUSE.md tick with the
# claude CLI. It reads LOOM_REPOS, LOOM_STATUS_REPO, LOOM_BRIEF_AUTHORS,
# LOOM_AWS_REGION and LOOM_BRAIN_* from the installing environment; with
# LOOM_REPOS empty it stays idle. LOOM_BRAIN=0 skips it and removes an
# installed one.
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
    <key>PATH</key><string>$X_HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$X_HOME</string>
    <key>LOOM_HOME</key><string>$X_LOOM</string>
$(loom_env)
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
