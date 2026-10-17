# Loom agent CLIs, sourced by lanes/run_lane.sh (one brief on a lane) and
# lanes/mac/brain.sh (one brain tick on whichever lane has usage): how the
# subscription lanes run their agent headless, and how a failed run is
# classed. The opencode lanes run through opencode_run (lanes/agent_sandbox.sh).
#
# The caller sets MODEL and AGENT_TIMEOUT (seconds).
# shellcheck shell=bash disable=SC2034 # FAIL_CLASS and RETRY_AT are read by the caller

# Run lane $1's agent CLI on prompt $3, as the lane runner does ($2 = lane)
# or as the brain does ($2 = brain). A lane run edits one tree: codex is
# sandboxed to it with no network, and claude asks for nothing it cannot
# answer in print mode. A brain tick reads and writes GitHub and works from
# its own checkout, so its agent gets the network and skips every prompt.
# Neither reads the keychain: both CLIs keep their sign-in in a file.
agent_cli() {
  case "$1:$2" in
    claude:lane) timeout "$AGENT_TIMEOUT" claude -p --model "$MODEL" "$3" < /dev/null ;;
    claude:brain) timeout "$AGENT_TIMEOUT" claude -p --model "$MODEL" \
      --permission-mode bypassPermissions "$3" < /dev/null ;;
    codex:lane) timeout "$AGENT_TIMEOUT" codex exec --sandbox workspace-write --model "$MODEL" "$3" < /dev/null ;;
    codex:brain) timeout "$AGENT_TIMEOUT" codex exec --sandbox danger-full-access \
      --skip-git-repo-check --model "$MODEL" "$3" < /dev/null ;;
    *) echo "no agent CLI for lane $1 ($2)" >&2; return 2 ;;
  esac
}

# What ended a failed run, from the tail of the agent's output. A provider
# limit: a usage, rate or quota limit, or an HTTP 429 or 402 answer. A
# transient fault: the network or the provider's servers. The patterns look
# at the last lines only, so a ticket that is about rate limits does not
# read as one.
LIMIT_RE='usage limit|rate[ _-]?limit|quota|too many requests|insufficient[ _](credit|balance|funds)|credit balance is too low|payment required|(http|status|code|error)[^0-9a-z]{0,3}(429|402)([^0-9]|$)'
OUT_OF_CREDIT_RE='credit balance is too low|insufficient[ _](credit|balance|funds)|payment required|(http|status|code|error)[^0-9a-z]{0,3}402([^0-9]|$)'
NET_RE='econnreset|econnrefused|etimedout|enotfound|eai_again|socket hang up|network error|fetch failed|connection (reset|refused|closed)|could not resolve host|overloaded|service unavailable|bad gateway|gateway timeout|internal server error|(http|status|code|error)[^0-9a-z]{0,3}(500|502|503|504|529)([^0-9]|$)'
# When the provider says its limit lifts: an epoch after "limit reached|" or a
# "reset" key, or a "retry after" / "try again in" / "resets in" duration.
EPOCH_RE='(limit reached\||reset[a-z_-]*["'"'"': =]+)([0-9]{10})([^0-9]|$)'
AFTER_RE='(retry[ -]after|try again in|resets? in)[^0-9]{0,3}([0-9]+) ?(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)?([^a-z]|$)'
LIMIT_WAIT=3600
LIMIT_WAIT_MAX=86400

# Sets FAIL_CLASS (limit, transient or work) for a run that ended with exit
# status $1 and output $2, where $3 is "edited" when the agent changed the
# tree. On limit also sets RETRY_AT. A crash before any edit, a timeout, a
# signal or a network fault is transient; anything else is work.
classify_failure() {
  local status="$1" out="$2" edited="$3" now n
  FAIL_CLASS=work RETRY_AT=""
  now="$(date +%s)"
  shopt -s nocasematch
  if [[ "$out" =~ $LIMIT_RE ]]; then
    FAIL_CLASS=limit RETRY_AT=$((now + LIMIT_WAIT))
    if [[ "$out" =~ $OUT_OF_CREDIT_RE ]]; then
      RETRY_AT=$((now + LIMIT_WAIT_MAX))
    elif [[ "$out" =~ $EPOCH_RE ]]; then
      RETRY_AT="${BASH_REMATCH[2]}"
    elif [[ "$out" =~ $AFTER_RE ]]; then
      n="${BASH_REMATCH[2]}"
      case "${BASH_REMATCH[3]}" in h*) n=$((n * 3600)) ;; m*) n=$((n * 60)) ;; esac
      RETRY_AT=$((now + n))
    fi
    [ "$RETRY_AT" -ge $((now + 60)) ] || RETRY_AT=$((now + 60))
    [ "$RETRY_AT" -le $((now + LIMIT_WAIT_MAX)) ] || RETRY_AT=$((now + LIMIT_WAIT_MAX))
  elif [ "$status" -ne 0 ]; then
    if [ "$status" -eq 124 ] || [ "$status" -gt 128 ] || [ "$edited" != edited ] ||
       [[ "$out" =~ $NET_RE ]]; then
      FAIL_CLASS=transient
    fi
  fi
  shopt -u nocasematch
}
