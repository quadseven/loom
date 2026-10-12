#!/usr/bin/env bash
# One brain tick on the lane machine (docs/BRAIN.md, docs/MUSE.md).
#
#   lanes/mac/brain.sh
#
# launchd runs it every LOOM_BRAIN_INTERVAL seconds (lanes/mac/install.sh).
# It takes the brain lock, refreshes its own checkout of this repo, runs the
# claude CLI headless on docs/MUSE.md, and releases the lock, also when the
# agent fails or times out. A lock held by anyone else (Muse, an interactive
# session) skips the tick before any model call, so only one brain acts at a
# time. With LOOM_REPOS empty it stays idle.
#
# Settings: LOOM_REPOS, LOOM_STATUS_REPO (where the lock lives; default this
# checkout's origin), LOOM_BRAIN_MODEL, LOOM_BRAIN_HOLDER, LOOM_BRAIN_MINUTES
# (the agent's hard stop, also the lock's expiry).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
WORK="$LOOM_HOME/brain"
LOG="$LOOM_HOME/logs/brain.log"
MODEL="${LOOM_BRAIN_MODEL:-claude-opus-5-5}"
HOLDER="${LOOM_BRAIN_HOLDER:-lane-machine}"
MINUTES="${LOOM_BRAIN_MINUTES:-40}"
ORIGIN="$(git -C "$REPO_ROOT" remote get-url origin)"
LOCK_REPO="${LOOM_STATUS_REPO:-$(printf '%s' "$ORIGIN" | sed -E 's#^.*[:/]([^/]+/[^/.]+)(\.git)?$#\1#')}"
mkdir -p "$WORK" "$(dirname "$LOG")"

say() { echo "[$(date -u +%FT%TZ)] brain: $*" >> "$LOG"; }

[ -n "${LOOM_REPOS:-}" ] || exit 0
[[ "$MINUTES" =~ ^[0-9]+$ ]] && [ "$MINUTES" -ge 5 ] || { say "LOOM_BRAIN_MINUTES must be a number of at least 5"; exit 1; }

bus() { (cd "$REPO_ROOT" && python3 -m bus "$@"); }

# The lock outlives the agent by 5 minutes, so a tick cut off by the timeout
# still releases it below before anyone else could take it.
if ! bus lock "$LOCK_REPO" --holder "$HOLDER" --minutes "$((MINUTES + 5))" >> "$LOG" 2>&1; then
  exit 0
fi
trap 'bus unlock "$LOCK_REPO" --holder "$HOLDER" >> "$LOG" 2>&1' EXIT
# launchd stops a job with SIGTERM; exiting on it runs the EXIT trap.
trap 'exit 143' TERM INT

if [ -d "$WORK/loom/.git" ]; then
  git -C "$WORK/loom" pull -q --ff-only >> "$LOG" 2>&1 || { say "could not update $WORK/loom"; exit 1; }
else
  git clone -q "$ORIGIN" "$WORK/loom" >> "$LOG" 2>&1 || { say "could not clone $ORIGIN"; exit 1; }
fi

PROMPT="You are the Loom brain for one tick. Follow $WORK/loom/docs/MUSE.md, \
with these differences: the checkout is already up to date, and the tick lock \
is already held for you as holder $HOLDER by the wrapper, so skip the lock and unlock steps \
(never lock or unlock). LOOM_REPOS is set in your environment. Work from \
$WORK/loom. Read each goal's home issue and its rules before acting on that \
goal. Stop within $((MINUTES - 5)) minutes, not 15; you are cut off at $MINUTES."

say "tick start (model $MODEL)"
(cd "$WORK" && timeout "$((MINUTES * 60))" claude -p --model "$MODEL" \
  --permission-mode bypassPermissions "$PROMPT" < /dev/null) >> "$LOG" 2>&1
say "tick end (exit $?)"
