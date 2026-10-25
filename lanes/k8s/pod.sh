#!/usr/bin/env bash
# Loom lane pod entrypoint: what launchd does for a lane on the Mac, in a
# container. Every LOOM_POD_INTERVAL seconds (default 60, launchd's
# StartInterval) it starts lanes/run_lane.sh once for each slot in
# LOOM_POD_SLOTS (space-separated, e.g. "opencode-go-5"), each in the
# background with its output in $LOOM_HOME/logs/<slot>-service.log, the
# Mac's file name. A slot whose runner is still busy holds its lane lock, so
# the new runner exits at once, as on the Mac.
#
# Defaults for a pod: LOOM_SANDBOX=bwrap (no sandbox-exec on Linux) and
# LOOM_DROP_TREES=1 (each run's scratch goes when it ends). LOOM_POD_ONCE=1
# runs one round, waits for it and exits (tests). What the pod needs:
# docs/LANES.md, "Lane pods".
set -uo pipefail

SLOTS="${LOOM_POD_SLOTS:?set LOOM_POD_SLOTS to the lane slots this pod runs}"
EVERY="${LOOM_POD_INTERVAL:-60}"
[[ "$EVERY" =~ ^[1-9][0-9]*$ ]] || { echo "LOOM_POD_INTERVAL must be a whole number of seconds" >&2; exit 2; }
for slot in $SLOTS; do
  [[ "$slot" =~ ^[a-z][a-z-]*[a-z](-[0-9]+)?$ ]] || { echo "bad lane slot $slot" >&2; exit 2; }
done
: "${LOOM_HOME:?set LOOM_HOME}"
export LOOM_SANDBOX="${LOOM_SANDBOX:-bwrap}" LOOM_DROP_TREES="${LOOM_DROP_TREES:-1}"
RUNNER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/run_lane.sh"
mkdir -p "$LOOM_HOME/logs" || { echo "cannot write $LOOM_HOME" >&2; exit 2; }
# This pod is the only runner set for its slots, and a restarted container
# starts its process ids again from 1: a lock left by the last container
# could name a live, unrelated pid and hold its slot for good.
for slot in $SLOTS; do rm -rf "$LOOM_HOME/run/$slot.lock"; done

# A stopping pod stops its runners; each runner's EXIT trap drops its lock
# and scratch. An agent in its own process group may outlive its runner by
# moments; the container's exit ends it.
trap 'trap - TERM INT; kill -TERM $(jobs -p) 2> /dev/null; wait; exit 143' TERM INT

while :; do
  for slot in $SLOTS; do
    bash "$RUNNER" "$slot" >> "$LOOM_HOME/logs/$slot-service.log" 2>&1 &
  done
  if [ "${LOOM_POD_ONCE:-}" = 1 ]; then wait; exit 0; fi
  sleep "$EVERY" &
  wait $!
done
