#!/usr/bin/env bash
# Loom sign-in probe (loom#3572): prove a subscription lane's sign-in with the
# cheapest real call its CLI offers, and record the result for the station.
#
#   signin_probe.sh <slot>...   every slot on one sign-in, all of one lane
#                               (codex codex-2 codex-3 codex-4)
#
# lanes/k8s/pod.sh starts it every LOOM_SIGNIN_PROBE_INTERVAL seconds in the
# lane's own container, where the sign-in lives. A Codex sign-in is a file
# under CODEX_HOME whose refresh token rotates when used: a refresh by one
# holder logs the others out. So the probe runs in place, on that file, and
# only while it holds the run lock (run/<slot>.lock) of every slot on the
# sign-in: no lane run can start beside it, and it starts beside none. A slot
# whose lock is taken makes it let go of the locks it took and try again every
# LOOM_SIGNIN_PROBE_POLL seconds (default 15), so it never holds a free slot
# idle while it waits, for up to LOOM_SIGNIN_PROBE_WAIT seconds (default
# 1500); past that it records nothing and the next round tries again. A lane
# runner that starts while the probe holds its lock exits at once, as it does
# for another runner (run_lane.sh is_lane_runner_pid).
#
# Result, written with `python3 -m bus.signin record` to
# state/signin/<first slot>.json: pass when the CLI answered; unknown when it
# hit a usage limit, the network or the time limit (LOOM_SIGNIN_PROBE_TIMEOUT,
# default 180 seconds), which says nothing of the sign-in; fail otherwise. The
# station alerts and asks on a fail and closes the ask on the next pass
# (bus/signin.py).
#
# The call: codex runs `codex exec` read-only on the lane's model
# (LOOM_CODEX_MODEL); claude runs `claude -p` on LOOM_SIGNIN_PROBE_CLAUDE_MODEL
# (default haiku) with the first slot's token file (LOOM_<SLOT>_AUTH_FILE, else
# LOOM_CLAUDE_AUTH_FILE), as a lane run does. Each answers one short prompt.
set -uo pipefail

[ "$#" -ge 1 ] || { echo "usage: signin_probe.sh <slot>..." >&2; exit 2; }
lane_of() { local s="$1"; [[ "$s" =~ -[0-9]+$ ]] && s="${s%-*}"; printf '%s' "$s"; }
LANE="$(lane_of "$1")"
for slot in "$@"; do
  [[ "$slot" =~ ^[a-z][a-z-]*[a-z](-[0-9]+)?$ ]] || { echo "bad lane slot $slot" >&2; exit 2; }
  [ "$(lane_of "$slot")" = "$LANE" ] || { echo "slot $slot is not on lane $LANE" >&2; exit 2; }
done
case "$LANE" in
  codex|claude) ;;
  *) echo "no sign-in probe for lane $LANE" >&2; exit 2 ;;
esac
: "${LOOM_HOME:?set LOOM_HOME}"
WAIT="${LOOM_SIGNIN_PROBE_WAIT:-1500}"
POLL="${LOOM_SIGNIN_PROBE_POLL:-15}"
LIMIT="${LOOM_SIGNIN_PROBE_TIMEOUT:-180}"
for n in "$WAIT" "$POLL" "$LIMIT"; do
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "LOOM_SIGNIN_PROBE_WAIT, _POLL and _TIMEOUT are whole seconds" >&2; exit 2; }
done
LOOM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$LOOM_HOME/run"
mkdir -p "$RUN" || exit 2
PROMPT="Reply with the single word ok."

HELD=()
SCRATCH=""
# Let go of each lock this probe took, and only while it is still this
# probe's: a lock is never taken from a runner.
release() {
  local slot
  for slot in ${HELD[@]+"${HELD[@]}"}; do
    [ "$(cat "$RUN/$slot.lock/pid" 2> /dev/null)" = "$$" ] && rm -rf "$RUN/$slot.lock"
  done
  HELD=()
}
cleanup() { release; [ -z "$SCRATCH" ] || rm -rf "$SCRATCH"; }
trap cleanup EXIT
trap 'exit 143' TERM INT

# Take every slot's lock, or none.
take_all() {
  local slot
  for slot in "$@"; do
    if mkdir "$RUN/$slot.lock" 2> /dev/null; then
      echo "$$" > "$RUN/$slot.lock/pid"
      HELD+=("$slot")
    else
      release
      return 1
    fi
  done
}

draining() {
  local slot
  for slot in "$@"; do [ ! -e "$RUN/$slot.drain" ] || return 0; done
  return 1
}

deadline=$((SECONDS + WAIT))
until take_all "$@"; do
  if draining "$@"; then exit 0; fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "[$(date -u +%FT%TZ)] sign-in probe $LANE: slots busy for ${WAIT}s; next round" >&2
    exit 0
  fi
  sleep "$POLL"
done
draining "$@" && exit 0

# The failure patterns a lane run is classed by, and the sign-in file of a
# claude slot (lanes/agent_cli.sh).
# shellcheck source=lanes/agent_cli.sh
. "$LOOM_ROOT/lanes/agent_cli.sh"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/signin-probe.XXXXXX")" || exit 2
probe_cli() {
  case "$LANE" in
    codex)
      (cd "$SCRATCH" && CODEX_HOME="${CODEX_HOME:-$HOME/.codex}" timeout "$LIMIT" \
        codex exec --skip-git-repo-check --sandbox read-only \
        ${LOOM_CODEX_MODEL:+--model "$LOOM_CODEX_MODEL"} "$PROMPT" < /dev/null) ;;
    claude)
      local model="${LOOM_SIGNIN_PROBE_CLAUDE_MODEL:-haiku}" auth
      auth="$(claude_auth_file "$1")"
      if [ -n "$auth" ] && [ -f "$auth" ]; then
        (cd "$SCRATCH" && CLAUDE_CODE_OAUTH_TOKEN="$(cat "$auth")" \
          timeout "$LIMIT" claude -p --model "$model" "$PROMPT" < /dev/null)
      else
        (cd "$SCRATCH" && timeout "$LIMIT" claude -p --model "$model" "$PROMPT" < /dev/null)
      fi ;;
  esac
}
out="$(probe_cli "$1" 2>&1)"
status=$?
release

result=fail
shopt -s nocasematch
if [ "$status" -eq 0 ]; then
  result=pass
elif [[ "$out" =~ $AUTH_RE ]]; then
  result=fail
elif [ "$status" -eq 124 ] || [[ "$out" =~ $LIMIT_RE ]] || [[ "$out" =~ $LIMIT_CODE_RE ]] ||
     [[ "$out" =~ $NET_RE ]]; then
  result=unknown
fi
shopt -u nocasematch
printf '%s' "$out" | PYTHONPATH="$LOOM_ROOT" python3 -B -m bus.signin record "$result" "$@" ||
  exit 2
echo "[$(date -u +%FT%TZ)] sign-in probe $LANE ($*): $result" >&2
