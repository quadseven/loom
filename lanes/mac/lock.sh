#!/usr/bin/env bash
# Sourced by lanes/mac/install.sh: is a lane's runner busy, and wait for a
# booted-out launchd job to be gone before bootstrapping it again.

runner_busy() {
  local pid_file="$1/pid" pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
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
