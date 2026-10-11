#!/usr/bin/env bash
# Install the codex and claude lanes as launchd agents on the login machine.
#
#   lanes/mac/install.sh [lane ...]
#     default: codex claude openrouter opencode-go anthropic
#
# API lanes read their provider key from SSM at run time. Name each
# parameter in the environment when installing (LOOM_OPENROUTER_KEY_SSM,
# LOOM_OPENCODE_GO_KEY_SSM, LOOM_ANTHROPIC_KEY_SSM, optional LOOM_AWS_REGION);
# they are written into the local plists only, never into this repo. A lane
# whose key is missing reports itself unavailable and receives no work.
#
# Each agent runs lanes/run_lane.sh from THIS checkout once a minute, so the
# code that runs is the code in git. No KeepAlive: a pass that exits is done,
# and the next tick starts a fresh one. Re-running replaces the agent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
# Lane default models, named in every PR. codex: the CLI's own configured
# default; claude: the current Opus. Override either in the environment.
LOOM_CODEX_MODEL="${LOOM_CODEX_MODEL:-$(sed -nE 's/^[[:space:]]*model[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$HOME/.codex/config.toml" 2>/dev/null | head -1)}"
LOOM_CLAUDE_MODEL="${LOOM_CLAUDE_MODEL:-claude-opus-5-5}"
LOOM_OPENROUTER_MODEL="${LOOM_OPENROUTER_MODEL:-openrouter/poolside/laguna-s-2.1:free}"
LOOM_OPENCODE_GO_MODEL="${LOOM_OPENCODE_GO_MODEL:-opencode-go/kimi-k2.7-code}"
LOOM_ANTHROPIC_MODEL="${LOOM_ANTHROPIC_MODEL:-claude-sonnet-5-5}"
export LOOM_CODEX_MODEL LOOM_CLAUDE_MODEL LOOM_OPENROUTER_MODEL LOOM_OPENCODE_GO_MODEL LOOM_ANTHROPIC_MODEL
# Briefs may only name repos of these owners. Default: this checkout's owner.
LOOM_REPO_OWNERS="${LOOM_REPO_OWNERS:-$(git -C "$REPO_ROOT" remote get-url origin | sed -E 's#^.*[:/]([^/]+)/[^/]+$#\1#')}"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$LOOM_HOME/logs"

LANES=("$@")
[ "${#LANES[@]}" -gt 0 ] || LANES=(codex claude openrouter opencode-go anthropic)

# Every LOOM_* setting in the environment, as plist entries.
loom_env() {
  env | LC_ALL=C sort | while IFS='=' read -r k v; do
    case "$k" in LOOM_HOME|LOOM_REPO_OWNERS) continue ;; LOOM_*) ;; *) continue ;; esac
    printf '    <key>%s</key><string>%s</string>\n' "$k" "$v"
  done
}

for L in "${LANES[@]}"; do
  LABEL="com.loom.$L"
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
    <string>$REPO_ROOT/lanes/run_lane.sh</string>
    <string>$L</string>
  </array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOOM_HOME/logs/$L-service.log</string>
  <key>StandardErrorPath</key><string>$LOOM_HOME/logs/$L-service.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
    <key>LOOM_HOME</key><string>$LOOM_HOME</string>
    <key>LOOM_REPO_OWNERS</key><string>$LOOM_REPO_OWNERS</string>
$(loom_env)
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $LABEL -> $REPO_ROOT/lanes/run_lane.sh $L"
done
