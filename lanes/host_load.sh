#!/bin/bash
# Shared by the lane checks, agent starts and station claims.
CHECK_LOAD_1M="${LOOM_CHECK_LOAD_1M:-24}"
CHECK_LOAD_5M="${LOOM_CHECK_LOAD_5M:-18}"
CHECK_LOAD_FILE="${LOOM_CHECK_LOAD_FILE:-/proc/loadavg}"
CHECK_LOAD_NOTE=""
[[ "$CHECK_LOAD_1M" =~ ^[0-9]+(\.[0-9]+)?$ ]] || CHECK_LOAD_1M=24
[[ "$CHECK_LOAD_5M" =~ ^[0-9]+(\.[0-9]+)?$ ]] || CHECK_LOAD_5M=18

# Print the host's 1-minute and 5-minute load averages as "<1m> <5m>";
# return 1 when there is no reading.
check_load_reading() {
  local line="" one="" five="" rest=""
  if [ -r "$CHECK_LOAD_FILE" ]; then
    IFS= read -r line < "$CHECK_LOAD_FILE" || [ -n "$line" ] || return 1
  else
    line="$(sysctl -n vm.loadavg 2>/dev/null || /usr/sbin/sysctl -n vm.loadavg 2>/dev/null)" || return 1
  fi
  # macOS prints "{ 2.92 3.15 3.21 }", and a comma for the point in some locales.
  line="${line//[\{\}]/ }"
  line="${line//,/.}"
  read -r one five rest <<< "$line"
  [[ "$one" =~ ^[0-9]+(\.[0-9]+)?$ && "$five" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 1
  printf '%s %s\n' "$one" "$five"
}

# Return 0 when the host load is above a limit, with CHECK_LOAD_NOTE saying
# which reading and limit; 1 when there is room, both limits are 0, or the
# load cannot be read (CHECK_LOAD_NOTE is then "unread").
check_load_high() {
  local reading one five
  CHECK_LOAD_NOTE=""
  [[ "$CHECK_LOAD_1M" =~ ^0+(\.0+)?$ && "$CHECK_LOAD_5M" =~ ^0+(\.0+)?$ ]] && return 1
  reading="$(check_load_reading)" || { CHECK_LOAD_NOTE=unread; return 1; }
  read -r one five <<< "$reading"
  CHECK_LOAD_NOTE="$(awk -v a="$one" -v b="$five" -v la="$CHECK_LOAD_1M" -v lb="$CHECK_LOAD_5M" 'BEGIN {
    if (la + 0 > 0 && a + 0 > la + 0) printf "1-minute load %s is above %s", a, la
    else if (lb + 0 > 0 && b + 0 > lb + 0) printf "5-minute load %s is above %s", b, lb
  }')"
  [ -n "$CHECK_LOAD_NOTE" ]
}

if [[ "${BASH_SOURCE[0]}" = "$0" ]]; then
  check_load_high
  printf '%s\n' "$CHECK_LOAD_NOTE"
fi
