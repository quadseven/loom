#!/usr/bin/env bash
# Sourced by lanes/mac/install.sh: is a lane's runner or a station pass busy,
# and wait for a booted-out launchd job to be gone before bootstrapping it
# again.

runner_busy() {
  local pid_file="$1/pid" pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

# Is a station pass holding its lock? bus/station.py writes its pid into the
# flock file state/station.lock for the whole pass; the pid is alive while a
# pass runs, and a stale pid from a killed pass reads as free.
station_busy() {
  local lock="$1" pid
  pid="$(cat "$lock" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

# Wait, bounded, while the station holds its lock, so booting it out never
# kills a pass mid-send (a report sent but not yet recorded would go out
# twice). LOOM_STATION_WAIT is the number of 0.2s tries (default 300, about a
# minute); 0 checks once. Says so and returns 1 when the bound is reached.
wait_station_free() {
  local lock="$1" tries="${LOOM_STATION_WAIT:-300}" i=0
  while station_busy "$lock"; do
    if [ "$i" -ge "$tries" ]; then
      echo "station still holds its lock after $tries tries; installing anyway"
      return 1
    fi
    i=$((i + 1))
    sleep 0.2
  done
  return 0
}

wait_job_gone() {
  local target="$1"
  for _ in {1..50}; do
    launchctl print "$target" >/dev/null 2>&1 || return 0
    sleep 0.2
  done
  # Gone at the last look counts as gone; still there is a failure.
  ! launchctl print "$target" >/dev/null 2>&1
}
