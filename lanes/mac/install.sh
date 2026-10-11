#!/usr/bin/env bash
# Install the codex and claude lanes as launchd agents on the login machine.
#
#   lanes/mac/install.sh [lane ...]     default: codex claude
#
# Each agent runs lanes/run_lane.sh from THIS checkout once a minute, so the
# code that runs is the code in git. No KeepAlive: a pass that exits is done,
# and the next tick starts a fresh one. Re-running replaces the agent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$LOOM_HOME/logs"

LANES=("$@")
[ "${#LANES[@]}" -gt 0 ] || LANES=(codex claude)

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
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $LABEL -> $REPO_ROOT/lanes/run_lane.sh $L"
done
