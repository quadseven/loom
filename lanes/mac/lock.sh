#!/usr/bin/env bash

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
  launchctl print "$target" >/dev/null 2>&1 && return 1
}
