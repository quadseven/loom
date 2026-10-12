#!/usr/bin/env bash
# Install the codex and claude lanes as launchd agents on the login machine.
#
#   lanes/mac/install.sh [lane ...]
#     default: codex claude openrouter opencode-go anthropic zen zai workersai
#
# API lanes read their provider key from SSM at run time. Name each
# parameter in the environment when installing (LOOM_OPENROUTER_KEY_SSM,
# LOOM_OPENCODE_GO_KEY_SSM, LOOM_ANTHROPIC_KEY_SSM, LOOM_ZAI_KEY_SSM, optional
# LOOM_AWS_REGION; zen needs no key); they are written into the local plists
# only, never into this repo. A lane whose key is missing reports itself
# unavailable and receives no work.
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
LOOM_ZEN_MODEL="${LOOM_ZEN_MODEL:-opencode/space-bunny-free}"
LOOM_ZAI_MODEL="${LOOM_ZAI_MODEL:-zai/glm-4.7-flash}"
LOOM_WORKERSAI_MODEL="${LOOM_WORKERSAI_MODEL:-cloudflare-workers-ai/@cf/qwen/qwen2.5-coder-32b-instruct}"
export LOOM_CODEX_MODEL LOOM_CLAUDE_MODEL LOOM_OPENROUTER_MODEL LOOM_OPENCODE_GO_MODEL LOOM_ANTHROPIC_MODEL \
  LOOM_ZEN_MODEL LOOM_ZAI_MODEL LOOM_WORKERSAI_MODEL
# Briefs may only name repos of these owners. Default: this checkout's owner.
LOOM_REPO_OWNERS="${LOOM_REPO_OWNERS:-$(git -C "$REPO_ROOT" remote get-url origin | sed -E 's#^.*[:/]([^/]+)/[^/]+$#\1#')}"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$LOOM_HOME/logs"

LANES=("$@")
[ "${#LANES[@]}" -gt 0 ] || LANES=(codex claude openrouter opencode-go anthropic zen zai workersai)

# PATH puts the official CLI install dirs (~/.local/bin for claude,
# ~/.opencode/bin for opencode) ahead of package-manager copies, which can lag
# behind and break (a Homebrew opencode crashed on start, 2026-10-11).

# XML-escape a value for a plist <string>.
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
X_REPO="$(xml "$REPO_ROOT")" X_LOOM="$(xml "$LOOM_HOME")" X_HOME="$(xml "$HOME")"

# Every LOOM_* setting in the environment, as plist entries.
loom_env() {
  env | LC_ALL=C sort | while IFS='=' read -r k v; do
    case "$k" in LOOM_HOME|LOOM_REPO_OWNERS) continue ;; LOOM_*) ;; *) continue ;; esac
    v="$(xml "$v")"
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
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $LABEL -> $X_REPO/lanes/run_lane.sh $L"
done

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
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $LABEL -> http://localhost:$VIEWER_PORT/"
fi
