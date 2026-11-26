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
#
# A stopping pod drains: on SIGTERM it starts no runner and
# marks run/<slot>.drain, so a runner takes no new brief; it waits up to
# LOOM_POD_DRAIN_SECONDS (default 240) for the runs in flight to end, then
# stops each agent still running (the process group its runner recorded in
# run/<slot>.lock/pgid) and waits up to LOOM_POD_PRESERVE_SECONDS (default 90)
# while each runner keeps its stopped run for the next pod (run_lane.sh
# keep_drained_run). The pod's termination grace period must cover both.
set -uo pipefail

SLOTS="${LOOM_POD_SLOTS:?set LOOM_POD_SLOTS to the lane slots this pod runs}"
EVERY="${LOOM_POD_INTERVAL:-60}"
DRAIN="${LOOM_POD_DRAIN_SECONDS:-240}"
PRESERVE="${LOOM_POD_PRESERVE_SECONDS:-90}"
# The sign-in probe (lanes/signin_probe.sh, loom#3572): every
# LOOM_SIGNIN_PROBE_INTERVAL seconds (default 1800; 0 turns it off), and once
# at start, one probe per lane in LOOM_SIGNIN_LANES (default "codex claude",
# the lanes on a subscription sign-in) that has a slot here, covering all of
# that lane's slots, which share its sign-in. Its output goes to
# logs/signin-probe.log. A round starts only when the last one has ended.
PROBE_EVERY="${LOOM_SIGNIN_PROBE_INTERVAL:-1800}"
PROBE_LANES="${LOOM_SIGNIN_LANES-codex claude}"
[[ "$PROBE_EVERY" =~ ^[0-9]+$ ]] || { echo "LOOM_SIGNIN_PROBE_INTERVAL must be a whole number of seconds" >&2; exit 2; }
[[ "$EVERY" =~ ^[1-9][0-9]*$ ]] || { echo "LOOM_POD_INTERVAL must be a whole number of seconds" >&2; exit 2; }
[[ "$DRAIN" =~ ^[0-9]+$ ]] || { echo "LOOM_POD_DRAIN_SECONDS must be a whole number of seconds" >&2; exit 2; }
[[ "$PRESERVE" =~ ^[0-9]+$ ]] || { echo "LOOM_POD_PRESERVE_SECONDS must be a whole number of seconds" >&2; exit 2; }
for slot in $SLOTS; do
  [[ "$slot" =~ ^[a-z][a-z-]*[a-z](-[0-9]+)?$ ]] || { echo "bad lane slot $slot" >&2; exit 2; }
done
: "${LOOM_HOME:?set LOOM_HOME}"
export LOOM_SANDBOX="${LOOM_SANDBOX:-bwrap}" LOOM_DROP_TREES="${LOOM_DROP_TREES:-1}"
RUNNER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/run_lane.sh"
PROBE="$(dirname "$RUNNER")/signin_probe.sh"
mkdir -p "$LOOM_HOME/logs" "$LOOM_HOME/run" || { echo "cannot write $LOOM_HOME" >&2; exit 2; }
# This pod is the only runner set for its slots, and a restarted container
# starts its process ids again from 1: a lock left by the last container
# could name a live, unrelated pid and hold its slot for good. A drain mark
# left by the last container would keep every runner from taking a brief.
for slot in $SLOTS; do rm -rf "$LOOM_HOME/run/$slot.lock" "$LOOM_HOME/run/$slot.drain"; done

# True once every runner has ended, false when $1 seconds pass first.
runners_end_within() {
  local until=$((SECONDS + $1))
  while [ -n "$(jobs -pr)" ]; do
    [ "$SECONDS" -lt "$until" ] || return 1
    sleep 1
  done
}

PROBES=""
PROBED_AT=""
# Start a sign-in probe round when one is due and the last has ended.
probe_signins() {
  local lane slot slots pid
  [ "$PROBE_EVERY" != 0 ] || return 0
  for pid in $PROBES; do kill -0 "$pid" 2> /dev/null && return 0; done
  [ -z "$PROBED_AT" ] || [ $((SECONDS - PROBED_AT)) -ge "$PROBE_EVERY" ] || return 0
  PROBES="" PROBED_AT="$SECONDS"
  for lane in $PROBE_LANES; do
    slots=""
    for slot in $SLOTS; do
      [ "$slot" = "$lane" ] || [ "${slot%-[0-9]*}" = "$lane" ] && slots="$slots $slot"
    done
    [ -n "$slots" ] || continue
    # shellcheck disable=SC2086 # one slot a word
    bash "$PROBE" $slots >> "$LOOM_HOME/logs/signin-probe.log" 2>&1 &
    PROBES="$PROBES $!"
  done
}

drain() {
  trap - TERM INT
  local slot pgid
  for slot in $SLOTS; do : > "$LOOM_HOME/run/$slot.drain"; done
  # A probe takes no lock once the drain marks are down; one mid-call ends now.
  # shellcheck disable=SC2086 # one pid a word
  [ -z "$PROBES" ] || kill -TERM $PROBES 2> /dev/null
  [ -z "${sleeper:-}" ] || { kill "$sleeper" 2> /dev/null; wait "$sleeper" 2> /dev/null; }
  echo "[$(date -u +%FT%TZ)] lane pod stopping: no new brief; waiting up to ${DRAIN}s for runs in flight" >&2
  if ! runners_end_within "$DRAIN"; then
    for slot in $SLOTS; do
      pgid="$(cat "$LOOM_HOME/run/$slot.lock/pgid" 2> /dev/null)"
      [[ "$pgid" =~ ^[1-9][0-9]*$ ]] || continue
      echo "[$(date -u +%FT%TZ)] lane pod stopping: stopping $slot's agent (process group $pgid)" >&2
      kill -TERM -- "-$pgid" 2> /dev/null || true
    done
    runners_end_within "$PRESERVE" ||
      echo "[$(date -u +%FT%TZ)] lane pod stopping: runners still busy after ${PRESERVE}s more; stopping them" >&2
  fi
  # A runner still here is stopped; its EXIT trap drops its lock and scratch.
  # An agent in its own process group may outlive it by moments; the
  # container's exit ends it.
  # shellcheck disable=SC2046 # one pid a word
  kill -TERM $(jobs -pr) 2> /dev/null
  wait
  exit 143
}
trap drain TERM INT

while :; do
  for slot in $SLOTS; do
    bash "$RUNNER" "$slot" >> "$LOOM_HOME/logs/$slot-service.log" 2>&1 &
  done
  probe_signins
  if [ "${LOOM_POD_ONCE:-}" = 1 ]; then wait; exit 0; fi
  sleep "$EVERY" &
  sleeper=$!
  wait "$sleeper"
  sleeper=""
done
