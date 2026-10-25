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
# A codex lane run has no network, so uv could not fetch the packages a test
# needs and the run failed on a DNS error (loom#1665). When the runner left
# the agent a copy of the base check's uv cache (AGENT_UV_DIR, with the
# check's Pythons in AGENT_UV_PYTHON), uv runs offline from it: the copy is
# the one extra directory codex may write (uv locks its cache), and the
# network stays closed.
agent_cli() {
  local uv_env=() uv_dir=()
  if [ "$1:$2" = codex:lane ] && [ -n "${AGENT_UV_DIR:-}" ]; then
    uv_env=("UV_CACHE_DIR=$AGENT_UV_DIR/cache" "UV_PYTHON_INSTALL_DIR=$AGENT_UV_PYTHON"
            UV_OFFLINE=1 UV_MANAGED_PYTHON=1 PIP_NO_INDEX=1)
    uv_dir=(--add-dir "$AGENT_UV_DIR")
  fi
  case "$1:$2" in
    claude:lane) timeout "$AGENT_TIMEOUT" claude -p --model "$MODEL" "$3" < /dev/null ;;
    claude:brain) timeout "$AGENT_TIMEOUT" claude -p --model "$MODEL" \
      --permission-mode bypassPermissions "$3" < /dev/null ;;
    codex:lane) timeout "$AGENT_TIMEOUT" env ${uv_env[@]+"${uv_env[@]}"} codex exec --sandbox workspace-write \
      ${uv_dir[@]+"${uv_dir[@]}"} --model "$MODEL" "$3" < /dev/null ;;
    codex:brain) timeout "$AGENT_TIMEOUT" codex exec --sandbox danger-full-access \
      --skip-git-repo-check --model "$MODEL" "$3" < /dev/null ;;
    *) echo "no agent CLI for lane $1 ($2)" >&2; return 2 ;;
  esac
}

# What ended a failed run. A provider limit: a usage, rate or quota limit, or
# an HTTP 429 or 402 answer. An expired or refused key: what the provider says
# when it will not take the key, or an HTTP 401. A transient fault: the
# network or the provider's servers. The limit and credit patterns are matched
# against the whole run output (the last lines only missed a usage limit that
# the CLI printed before more output); the auth and transient patterns stay on
# the short tail (the last five non-blank lines), so a ticket that is about
# expired keys or network errors is not read as a fault, and the reason shown
# to people stays the short tail.
# A bare "quota" is no limit: a timed-out agent's git log or a file it read
# can say it, and three Spark lanes were held an hour on a commit subject
# (2026-10-20). Providers say the quota is reached, exceeded or exhausted.
# A full shared free pool ("has no lane available; retry after 1s") is a
# short limit, not the agent's failure (18 runs, 2026-10-21).
LIMIT_RE='usage limit|rate[ _-]?limit|has no lane available|quota (reached|exceeded|exhausted|resets?)|exceeded (your |the )?(current )?quota|resource[ _]exhausted|too many requests|insufficient[ _](credit|balance|funds)|credit balance is too low|payment required|hit your (weekly |daily |session )?limit'
# A bare HTTP status counts only in the short tail: a repo's own test output
# printed "status=error detail=http 402", and a codex run that exited 0 was
# classed out of credit and its lane parked a day (2026-10-21).
LIMIT_CODE_RE='(http|status|code|error)[^0-9a-z]{0,3}(429|402)([^0-9]|$)'
KEY_STATE='expired|revoked|invalid|incorrect|disabled'
# A CLI that signs in through a browser (agy) and finds its sign-in lapsed
# waits for a login nobody gives, then says "authentication timed out" or
# "authentication failed": that is auth, not transient (2026-10-24). It is the
# lane's sign-in that lapsed, not a key, whatever key parameter the lane names.
SIGNIN_RE="error:[ ]*authentication[ ]+(timed[ ]+out|failed)"
AUTH_RE="($KEY_STATE)[ _-]*api[ _-]*key|api[ _-]*key([ _-]*(is|was|has|been|are))*[ _-]*($KEY_STATE)|(http|status|code|error)[^0-9a-z]{0,3}401([^0-9]|\$)|$SIGNIN_RE"
# A quota on the whole account, not one model: every model of the lane is
# limited until it resets (agy, 2026-10-18: "Individual quota reached ...
# Resets in 67h", but only the model that ran was marked, so the lane stayed
# available and took a claim every few minutes).
ACCOUNT_QUOTA_RE='individual quota reached|quota (exceeded|reached) for (this|the|your) (account|project|user|organization)'
OUT_OF_CREDIT_RE='credit balance is too low|insufficient[ _](credit|balance|funds)|payment required'
CREDIT_CODE_RE='(http|status|code|error)[^0-9a-z]{0,3}402([^0-9]|$)'
NET_RE='econnreset|econnrefused|etimedout|enotfound|eai_again|socket hang up|network error|fetch failed|connection (reset|refused|closed)|could not resolve host|overloaded|service unavailable|bad gateway|gateway timeout|internal server error|(http|status|code|error)[^0-9a-z]{0,3}(500|502|503|504|529)([^0-9]|$)'
# When the provider says its limit lifts: an epoch after "limit reached|" or a
# "reset" key, or a "retry after" / "try again in" / "resets in" duration.
EPOCH_RE='(limit reached\||reset[a-z_-]*["'"'"': =]+)([0-9]{10})([^0-9]|$)'
AFTER_RE='(retry[ -]after|try again in|resets? in)[^0-9]{0,3}([0-9]+) ?(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)?([^a-z]|$)'
# A daily allowance with no reset time given lifts at the next midnight UTC
# (Workers AI: "used up your daily free allocation of 10,000 neurons";
# OpenRouter: "free-models-per-day"). The fixed hour retried it, and failed,
# every hour until then (2026-10-19).
DAILY_RE='daily (free )?(allocation|allowance|limit|quota)|per[ _-]day'
MONTHLY_RE='copilot.*(allowance|limit|quota|usage|request)|monthly ([a-z]+ )*(allocation|allowance|limit|quota)|per[ _-]month|resets? (at|on) the first of (the )?month'
# A model the agent CLI no longer offers (opencode drops one models.dev marks
# deprecated) rests the lane LIMIT_WAIT, then the lane checks the CLI's model
# list before it claims (model_listed in run_lane.sh). A day's rest assumed
# the model stays gone; free catalogs rotate within hours (2026-10-21: zen's
# model was listed again within 13 hours, with the lane parked for 11 more).
MODEL_GONE_RE='ProviderModelNotFoundError: Model not found'
LIMIT_WAIT=3600
LIMIT_WAIT_MAX=86400
# A reset time the provider states ("try again at 11:46 PM", "try again at
# Oct 24th, 2026 4:43 PM") holds the lane until then, up to a week: codex
# said when its limit reset and the lane retried hourly anyway, a ticket
# round each time (2026-10-21).
RESET_WAIT_MAX=$((7 * 86400))

# The epoch of the reset time or duration stated in $1 ("try again at ...",
# "resets in ..."), read as the machine's local time or now plus the duration,
# the time of day alone meaning its next occurrence; prints nothing when $1
# states none.
stated_reset() {
  python3 -c '
import re, sys, time
from datetime import datetime, timedelta
m = re.search(r"(?:try again at|resets) (?:([A-Za-z]{3}) ([0-9]{1,2})(?:st|nd|rd|th)?, ([0-9]{4}) )?([0-9]{1,2})(?::([0-9]{2}))? ?([AP]M)", sys.argv[1], re.I)
done = False
if m:
    hour = int(m.group(4)) % 12 + (12 if m.group(6).upper() == "PM" else 0)
    minute = int(m.group(5)) if m.group(5) else 0
    now = datetime.now()
    try:
        if m.group(1):
            at = datetime.strptime("%s %s %s" % (m.group(1).title(), m.group(2), m.group(3)), "%b %d %Y")
            at = at.replace(hour=hour, minute=minute)
        else:
            at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at <= now:
                at += timedelta(days=1)
        print(int(time.mktime(at.timetuple())))
        done = True
    except ValueError:
        pass
if not done:
    m = re.search(r"(?:try again in|resets? in)[:\s]\s*((?:[0-9]+\s*[a-z]+[.,\s]*(?:and\s+)?)+)", sys.argv[1], re.I)
    if m:
        units = {
            "d": 86400, "day": 86400, "days": 86400,
            "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
            "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
            "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        }
        parts = re.findall(r"([0-9]+)\s*([a-z]+)", m.group(1), re.I)
        if parts and all(u.lower() in units for _, u in parts):
            seconds = sum(int(v) * units[u.lower()] for v, u in parts)
            if seconds > 0:
                print(int(time.time() + seconds))
' "$1" 2>/dev/null
}

# Sets FAIL_CLASS (auth, limit, transient or work) for a run that ended with
# exit status $1 and output $2, where $3 is "edited" when the agent changed
# the tree. $2 is the run's whole output (from LOG_START in the lane runner,
# log_start in the brain) and $4 is the short tail (its last five non-blank
# lines); the limit and credit patterns match $2 while auth and transient
# match $4. On limit also sets RETRY_AT. A key the provider will not take is
# auth: no retry and no attempt can fix it, so the runner takes its lanes
# offline until the operator renews it. A crash before any edit, a timeout,
# a signal or a network fault is transient; anything else is work.
# An agent CLI's JSON events that carry the work, not the provider's word:
# tool results, the model's own text and reasoning, step markers (opencode
# --format json). Code about rate limits that a tool read is no limit: two
# Spark lanes were held an hour on such tool results (2026-10-21). A provider
# error arrives as its own line ({"type":"error",...} or plain text).
WORK_EVENT_RE='^\{"type":"(tool_use|text|reasoning|step_start|step_finish)"'

# Stdin without the agent's work events, for the limit and credit patterns.
provider_text() {
  grep -vE "$WORK_EVENT_RE" || true
}

classify_failure() {
  local status="$1" whole="$2" edited="$3" now n tail cap stated monthly=0 seen
  tail="${4:-$whole}"
  # A run that exited 0 wrote its answer: a limit phrase earlier in its
  # output came from a file or test it read (a Makefile echoing "Rate-limiting
  # wrapper deployed", a test named test_..._rate_limited parked two codex
  # lanes until midnight, 2026-10-25), so only what its CLI said last counts.
  seen="$whole"
  [ "$status" -eq 0 ] && seen="$tail"
  FAIL_CLASS=work RETRY_AT="" OUT_OF_CREDIT=""
  now="$(date +%s)"
  shopt -s nocasematch
  if [[ "$tail" =~ $AUTH_RE ]]; then
    FAIL_CLASS=auth
  elif [[ "$whole" =~ $MODEL_GONE_RE ]]; then
    FAIL_CLASS=limit RETRY_AT=$((now + LIMIT_WAIT))
  elif [[ "$seen" =~ $LIMIT_RE ]] || [[ "$tail" =~ $LIMIT_CODE_RE ]]; then
    FAIL_CLASS=limit RETRY_AT=$((now + LIMIT_WAIT)) cap=$LIMIT_WAIT_MAX
    # A reset time or duration the provider states wins: codex's five-hour
    # limit says "purchase more credits or try again at 10:28 AM", agy's
    # "Resets in 87h14m1s" (2026-10-21).
    if stated="$(stated_reset "$whole")" && [ -n "$stated" ]; then
      RETRY_AT="$stated" cap=$RESET_WAIT_MAX
    elif [[ "$whole" =~ $OUT_OF_CREDIT_RE ]] || [[ "$tail" =~ $CREDIT_CODE_RE ]]; then
      OUT_OF_CREDIT=1 RETRY_AT=$((now + LIMIT_WAIT_MAX))
    elif [[ "$whole" =~ $EPOCH_RE ]]; then
      RETRY_AT="${BASH_REMATCH[2]}"
    elif [[ "$whole" =~ $AFTER_RE ]]; then
      n="${BASH_REMATCH[2]}"
      case "${BASH_REMATCH[3]}" in h*) n=$((n * 3600)) ;; m*) n=$((n * 60)) ;; esac
      RETRY_AT=$((now + n))
    elif [[ "$whole" =~ $DAILY_RE ]]; then
      RETRY_AT=$((now - now % 86400 + 86400))
    elif [[ "$whole" =~ $MONTHLY_RE ]]; then
      monthly=1
      RETRY_AT="$(python3 -c 'from datetime import datetime, timezone; n=datetime.now(timezone.utc); y=n.year+(n.month==12); m=1 if n.month==12 else n.month+1; print(int(datetime(y,m,1,tzinfo=timezone.utc).timestamp()))')"
    fi
    [ "$RETRY_AT" -ge $((now + 60)) ] || RETRY_AT=$((now + 60))
    if [ "$monthly" -eq 0 ] && [ "$RETRY_AT" -gt $((now + cap)) ]; then
      RETRY_AT=$((now + cap))
    fi
  elif [ "$status" -ne 0 ]; then
    if [ "$status" -eq 124 ] || [ "$status" -gt 128 ] || [ "$edited" != edited ] ||
       [[ "$tail" =~ $NET_RE ]]; then
      FAIL_CLASS=transient
    fi
  fi
  shopt -u nocasematch
}
