#!/usr/bin/env bash
# Install the read-only evidence endpoint as a launchd agent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
: "${LOOM_EVIDENCE_CONFIG:?LOOM_EVIDENCE_CONFIG is required}"
# This is the SSM parameter name; the bearer value is fetched by the service.
: "${LOOM_EVIDENCE_TOKEN_SSM:?LOOM_EVIDENCE_TOKEN_SSM is required}"
: "${LOOM_EVIDENCE_BIND_HOST:?LOOM_EVIDENCE_BIND_HOST must be the tailnet interface address}"
: "${LOOM_AWS_REGION:?LOOM_AWS_REGION is required}"
LOOM_EVIDENCE_PORT="${LOOM_EVIDENCE_PORT:-7341}"
LOOM_TAILSCALE_BIN="${LOOM_TAILSCALE_BIN:-tailscale}"
[[ "$LOOM_EVIDENCE_PORT" =~ ^[0-9]+$ ]] && (( LOOM_EVIDENCE_PORT > 0 && LOOM_EVIDENCE_PORT < 65536 )) || {
  echo "LOOM_EVIDENCE_PORT must be a valid port" >&2
  exit 1
}
[[ -f "$LOOM_EVIDENCE_CONFIG" ]] || { echo "evidence config file not found" >&2; exit 1; }
[[ "$LOOM_EVIDENCE_BIND_HOST" != "0.0.0.0" && "$LOOM_EVIDENCE_BIND_HOST" != "::" &&
   "$LOOM_EVIDENCE_BIND_HOST" != "127.0.0.1" && "$LOOM_EVIDENCE_BIND_HOST" != "localhost" ]] || {
  echo "LOOM_EVIDENCE_BIND_HOST must be a non-loopback interface address" >&2
  exit 1
}

AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$HOME/Library/Logs"
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
PLIST="$AGENTS/com.loom.evidence.plist"
cat > "$PLIST" <<EOF_PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.loom.evidence</string>
  <key>ProgramArguments</key>
  <array><string>$(xml "$(command -v python3)")</string><string>$(xml "$REPO_ROOT/lanes/evidence/serve.py")</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(xml "$HOME/Library/Logs/loom-evidence.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$HOME/Library/Logs/loom-evidence.log")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LOOM_EVIDENCE_CONFIG</key><string>$(xml "$LOOM_EVIDENCE_CONFIG")</string>
    <key>LOOM_EVIDENCE_TOKEN_SSM</key><string>$(xml "$LOOM_EVIDENCE_TOKEN_SSM")</string>
    <key>LOOM_EVIDENCE_BIND_HOST</key><string>$(xml "$LOOM_EVIDENCE_BIND_HOST")</string>
    <key>LOOM_EVIDENCE_PORT</key><string>$(xml "$LOOM_EVIDENCE_PORT")</string>
    <key>LOOM_TAILSCALE_BIN</key><string>$(xml "$LOOM_TAILSCALE_BIN")</string>
    <key>LOOM_AWS_REGION</key><string>$(xml "$LOOM_AWS_REGION")</string>
    <key>PATH</key><string>$(xml "$PATH")</string>
  </dict>
</dict>
</plist>
EOF_PLIST
launchctl bootout "gui/$(id -u)/com.loom.evidence" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed com.loom.evidence"
