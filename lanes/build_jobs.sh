#!/bin/bash
# The job cap a lane agent run and each step of a lane check start under,
# sourced by lanes/run_lane.sh after CHECK_WORKERS is set.
#
# The host load gates only stop a run from starting. A run in flight could
# still take every core: one lane check compiled a dependency from source
# with 28 compiler processes on a 12-core host, beside other builds, and the
# 1-minute load reached 80. LOOM_BUILD_JOBS (default 4, 0 for no cap) is the
# number of parallel jobs each run's build tools are told to use.
#
# It reaches the tools two ways. The ones that read a job count from the
# environment get it there (build_jobs_names). xcodebuild and swift take a
# job count only as a flag, so a wrapper for each sits in lanes/jobcap, which
# goes first on the run's PATH (BUILD_JOBS_BIN) and adds the flag.
#
# A tool that reads the core count for itself and takes no setting is not
# capped. Neither is a build tool started by its full path, or through
# xcrun, past the wrapper.
# shellcheck shell=bash disable=SC2034 # the arrays and BUILD_JOBS_BIN are read by the caller

BUILD_JOBS="${LOOM_BUILD_JOBS:-4}"
# A value that is not a whole number is a typo, not a request for no cap.
[[ "$BUILD_JOBS" =~ ^[0-9]{1,4}$ ]] || BUILD_JOBS=4
BUILD_JOBS=$((10#$BUILD_JOBS))

# The wrapper directory, or empty when there is no cap: PATH and the sandbox
# profile are then exactly what they were without this file.
BUILD_JOBS_BIN=""
[ "$BUILD_JOBS" -eq 0 ] || BUILD_JOBS_BIN="$LOOM_ROOT/lanes/jobcap"

# Every name the cap sets, for docs and tests: build_jobs_list sets these
# and no others.
build_jobs_names() {
  printf '%s\n' LOOM_BUILD_JOBS MAKEFLAGS CMAKE_BUILD_PARALLEL_LEVEL PYTEST_XDIST_AUTO_NUM_WORKERS \
    CARGO_BUILD_JOBS GOMAXPROCS GRADLE_OPTS UV_CONCURRENT_BUILDS RAYON_NUM_THREADS \
    OMP_NUM_THREADS npm_config_jobs
}

# The cap as NAME=value lines, none when the cap is off. $1, when given, is
# the check's worker count (LOOM_CHECK_WORKERS): a test runner then gets the
# smaller of the two, and LOOM_CHECK_WORKERS itself is lowered to the cap
# when it is larger. No value holds a space, so each line is one word.
build_jobs_list() {
  local n="$BUILD_JOBS" workers="${1:-}" tests
  [ "$n" -gt 0 ] || return 0
  tests="$n"
  if [[ "$workers" =~ ^[0-9]{1,4}$ ]] && [ "$((10#$workers))" -gt 0 ]; then
    if [ "$((10#$workers))" -lt "$n" ]; then
      tests="$((10#$workers))"
    elif [ "$((10#$workers))" -gt "$n" ]; then
      printf 'LOOM_CHECK_WORKERS=%s\n' "$n"
    fi
  fi
  # LOOM_BUILD_JOBS itself travels too: the wrappers read it.
  printf '%s\n' "LOOM_BUILD_JOBS=$n" "MAKEFLAGS=-j$n" "CMAKE_BUILD_PARALLEL_LEVEL=$n" \
    "PYTEST_XDIST_AUTO_NUM_WORKERS=$tests" "CARGO_BUILD_JOBS=$n" "GOMAXPROCS=$n" \
    "GRADLE_OPTS=-Dorg.gradle.workers.max=$n" "UV_CONCURRENT_BUILDS=1" \
    "RAYON_NUM_THREADS=$n" "OMP_NUM_THREADS=$n" "npm_config_jobs=$n"
}

# BUILD_JOBS_ENV: what an agent run's environment carries. An emptied agent
# environment (api_agent, agy_agent) is given these words one by one.
# BUILD_JOBS_CHECK_ENV: the same for a check step, with the check's workers.
BUILD_JOBS_ENV=()
BUILD_JOBS_CHECK_ENV=()
while IFS= read -r build_jobs_kv; do
  [ -z "$build_jobs_kv" ] || BUILD_JOBS_ENV+=("$build_jobs_kv")
done <<< "$(build_jobs_list)"
while IFS= read -r build_jobs_kv; do
  [ -z "$build_jobs_kv" ] || BUILD_JOBS_CHECK_ENV+=("$build_jobs_kv")
done <<< "$(build_jobs_list "${CHECK_WORKERS:-}")"
unset build_jobs_kv

# Put the cap into this shell's environment, for an agent that inherits it
# (the subscription lanes) and as the PATH an emptied one is given. Called in
# the agent's own subshell, so the runner's environment is never changed.
# Flags the host already set for make and gradle are kept, with the cap
# after them so it wins.
build_jobs_export() {
  local kv
  [ "$BUILD_JOBS" -gt 0 ] || return 0
  for kv in ${BUILD_JOBS_ENV[@]+"${BUILD_JOBS_ENV[@]}"}; do
    case "$kv" in
      MAKEFLAGS=*) export MAKEFLAGS="${MAKEFLAGS:+$MAKEFLAGS }${kv#MAKEFLAGS=}" ;;
      GRADLE_OPTS=*) export GRADLE_OPTS="${GRADLE_OPTS:+$GRADLE_OPTS }${kv#GRADLE_OPTS=}" ;;
      *) export "${kv?}" ;;
    esac
  done
  export PATH="$BUILD_JOBS_BIN:$PATH"
}
