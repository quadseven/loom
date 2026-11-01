#!/usr/bin/env bash
# Loom lane runner: one pass over a lane's inbox, one brief at a time.
#
#   run_lane.sh <lane>   lane = codex | claude | openrouter | opencode-go | anthropic
#                               | zen | zai | workersai | agy | spark | copilot
#                               | <lane>-<n> (an extra slot of that lane)
#
# A slot such as anthropic-2 is a second copy of the anthropic lane: its own
# inbox, lock, log, scratch home and heartbeat, the same provider, key and
# model settings (LOOM_ANTHROPIC_*). Slots let one provider run several briefs
# at once.
#
# Run it on a schedule (launchd StartInterval), not as a forever loop. Every
# brief ends in exactly one place: done/ (a PR exists for its branch) or
# failed/ (the run did not produce one). A brief is never run twice, so a
# crash or restart cannot turn into a re-run loop. Each failure is classed
# limit, transient or work in its log line; the station may re-brief a limit
# or transient failure to another lane as a new attempt on a fresh branch.
# A limit failure also holds this lane unavailable until the provider's reset
# (else 60 minutes). The agent edits (and
# may commit); this runner commits what is left, pushes, and opens the PR.
# Unreferenced scratch files added outside the brief's paths are dropped;
# other added files outside those paths are refused before validation and push.
# A repo with a validation contract (.loom/validation.conf) is prepared
# before the agent and checked again on its commit; a failing check parks
# the brief and keeps the edits on a side branch instead of opening a PR.
# A fix brief (a Fix-PR: <number> line, from the station) instead works on
# that open PR's existing branch: it pushes fast-forward only and comments on
# the PR, and never opens a second one.
# A verify brief (Deliverable: verify, from the station) changes no code: the
# agent leaves verify.json in its tree, the runner copies it to the lane's log
# folder with the run's model beside it (<ticket>.verify.model), and the
# station checks it and moves the ticket, naming lane and model as the proof's
# maker. No pull request.
# A review brief (Deliverable: review, from the station, loom#2142) also
# changes no code: its tree is the reviewed pull request's own branch (a
# Review-PR: <number> line names it, cloned like a fix brief's), the agent
# leaves review.json in its tree, and the runner copies it to the lane's log
# folder the same way; the station posts the verdict on the pull request.
# Lanes never merge. codex runs sandboxed: no network, no .git writes.
#
# Env: LOOM_REPO_OWNERS (required: space-separated GitHub owners whose repos
# a brief may name; the agent trusts the cloned repo's own config),
# LOOM_<LANE>_MODEL (the lane's model when a brief has no Model: line, e.g.
# LOOM_OPENCODE_GO_MODEL; every run passes its model explicitly and names it
# in the PR), LOOM_<LANE>_KEY_SSM (API lanes: the SSM parameter holding the
# provider key, read at run time; never stored in this repo; zen needs none),
# LOOM_HOME (default ~/loom), LOOM_AGENT_TIMEOUT (seconds, default 3600),
# LOOM_SKILL_DIRS (colon-separated skill folders the sandboxed lanes get a
# snapshot of; default ~/.agents/skills:~/.claude/skills),
# LOOM_NET_TIMEOUT (seconds per gh/git network call, default 120),
# LOOM_CHECK_TIMEOUT (seconds per validation step, default 1800; see
# load_contract for the repo's .loom/validation.conf),
# LOOM_<SLOT>_OPENCODE (the opencode binary one slot runs, default opencode
# from PATH; a 2.x binary uses a private server inside its sandbox),
# LOOM_<SLOT>_PROBE_URL and _PROBE_HEADER (API lanes: the provider's chat
# endpoint, asked for a usage limit before each run and after a timeout; see
# provider_at_limit),
# LOOM_SANDBOX (seatbelt, the default, or bwrap in a lane pod, which also
# needs LOOM_EGRESS_DENY_PROBES, IPv4:port addresses the pod's egress policy
# blocks, and LOOM_EGRESS_ALLOW_PROBE, a host:port it must reach; see
# egress_denied),
# LOOM_DROP_TREES (1: each run's scratch goes when it ends), LOOM_SSM_DIR (a
# folder of copied SSM values read in place of SSM; docs/LANES.md, "Lane
# pods"), LOOM_CLONE_BASE (default git@github.com:), for the anthropic lanes
# LOOM_ANTHROPIC_BUDGET_USD, _BUDGET_SINCE, _BUDGET_EXPIRES and _FLOOR_USD
# (docs/LANES.md, "Anthropic spend"), and for the spark lane LOOM_SPARK_URL
# (the OpenAI-compatible base URL of the local vLLM coder, e.g.
# http://your-spark.internal:8000/v1; no key).
set -uo pipefail

SLOT="${1:?usage: run_lane.sh <lane>}"
[[ "$SLOT" =~ ^[a-z][a-z-]*[a-z](-[0-9]+)?$ ]] || { echo "bad lane name $SLOT" >&2; exit 2; }
# The provider lane: the slot without a trailing -<n> (opencode-go stays whole).
LANE="$SLOT"
[[ "$SLOT" =~ -[0-9]+$ ]] && LANE="${SLOT%-*}"
# A free provider slot (free-ollama, free-kilo, ...) is lane free: one
# OpenAI-compatible route per provider in the spark-gateway's free pool
# (infra#5617). Not a Spark: it shares the spark lane's endpoint handling.
[[ "$SLOT" =~ ^free-[a-z]+$ ]] && LANE=free
LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
export LOOM_HOME
# This checkout, for its python helpers (bus/spend.py).
LOOM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The leak scanner a public-repo push reads lives in infra-public, kept at
# $LOOM_HOME/infra-public and fast-forwarded at most hourly
# (keep_scanner_current). The default reads it from there.
LEAK_SCANNER="${LOOM_LEAK_SCANNER:-$LOOM_HOME/infra-public/.github/scripts/check_private_leaks.py}"
# The remote infra-public is cloned from, LOOM_CLONE_BASE-prefixed like the
# ticket repos the lane clones. Override it on a machine without network.
INFRA_PUBLIC="$LOOM_HOME/infra-public"
INFRA_PUBLIC_REMOTE="${LOOM_CLONE_BASE:-git@github.com:}quadseven/infra-public"
INFRA_PUBLIC_STAMP="$LOOM_HOME/run/infra-public.updated"
# The SSM parameter of the shared deny-list: names and products with no
# generic shape, which only this list catches. Every scan passes it.
LEAK_DENY_SSM="${LOOM_LEAK_DENY_SSM:-/infra/leak-scan/deny-list}"

# The public repo leak scan, the one way the runner calls the scanner, so no
# scan runs without the deny-list. Arguments are the scanner's (--diff RANGE
# or --text-file FILE, --allow-repo-ref ...). Exit 0 clean, 1 a hit, 2 no
# scan: the scanner is missing, or the deny-list cannot be read (the scanner
# exits 2). A lane pod or cluster runner (LOOM_SSM_DIR) holds no AWS key: the
# scanner reads the deny-list's copy under LOOM_SSM_DIR through the stand-in
# lanes/k8s/ssm-dir-bin/aws, first on its PATH. With no copy there it scans
# nothing and pushes nothing to a public repo rather than scan without the list.
leak_scan() {
  if [ ! -f "$LEAK_SCANNER" ]; then
    echo "leak scanner not found: $LEAK_SCANNER" >&2
    return 2
  fi
  if [ -n "${LOOM_SSM_DIR:-}" ]; then
    if [ ! -s "$LOOM_SSM_DIR$LEAK_DENY_SSM" ]; then
      echo "no copy of the leak scan deny-list $LEAK_DENY_SSM under LOOM_SSM_DIR; nothing scanned" >&2
      return 2
    fi
    PATH="$LOOM_ROOT/lanes/k8s/ssm-dir-bin:$PATH" timeout "$NET_TIMEOUT" \
      python3 -B "$LEAK_SCANNER" "$@" --deny-list-ssm "$LEAK_DENY_SSM"
    return
  fi
  env ${LOOM_AWS_REGION:+AWS_REGION="$LOOM_AWS_REGION"} timeout "$NET_TIMEOUT" \
    python3 -B "$LEAK_SCANNER" "$@" --deny-list-ssm "$LEAK_DENY_SSM"
}
# Keep $INFRA_PUBLIC cloned and fast-forward it at most once an hour, so the
# leak scanner a public-repo push needs is present before any lane scans.
# The first lane that needs it clones it; a later run fetches at most hourly,
# gated by the stamp file. Returns 0 when the checkout is usable, 1 when it
# cannot be had (the scanner stays missing and the run becomes an incident).
keep_scanner_current() {
  local now stamp=0
  now="$(date +%s)"
  [ -f "$INFRA_PUBLIC_STAMP" ] && stamp="$(tr -cd '0-9' < "$INFRA_PUBLIC_STAMP")"
  if [ -d "$INFRA_PUBLIC/.git" ]; then
    if [ $((now - stamp)) -ge 3600 ]; then
      (cd "$INFRA_PUBLIC" && timeout "$NET_TIMEOUT" git fetch -q origin \
          && timeout "$NET_TIMEOUT" git merge -q --ff-only FETCH_HEAD) >> "$LOG" 2>&1 && date +%s > "$INFRA_PUBLIC_STAMP"
    fi
    return 0
  fi
  mkdir -p "$LOOM_HOME"
  if timeout "$NET_TIMEOUT" git clone -q "$INFRA_PUBLIC_REMOTE" "$INFRA_PUBLIC" >> "$LOG" 2>&1; then
    date +%s > "$INFRA_PUBLIC_STAMP"
    return 0
  fi
  echo "could not clone the leak scanner's repo $INFRA_PUBLIC_REMOTE" >&2
  return 1
}
INBOX="$LOOM_HOME/briefs/$SLOT"
LOCK="$LOOM_HOME/run/$SLOT.lock"

mkdir -p "$INBOX/done" "$INBOX/failed" "$LOOM_HOME/run"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# The process groups in the tree under pid $1: the agent's job-control
# subshell and everything below it. `timeout` puts itself in a group of its
# own, so killing one group is not enough.
agent_groups() {
  local c
  ps -o pgid= -p "$1" 2>/dev/null | tr -d ' '
  for c in $(pgrep -P "$1" 2>/dev/null); do agent_groups "$c"; done
}

# A runner killed mid-brief (a reinstall's bootout kills its process group)
# leaves its agent running in groups of their own. Before a new run, stop the
# whole tree under the recorded subshell, but only if that pid is still this
# lane's runner subshell (a recycled pid is left alone).
stop_orphaned_agent() {
  local leader="$1" mine groups g alive
  [[ "$leader" =~ ^[0-9]+$ ]] && kill -0 "$leader" 2>/dev/null || return 0
  ps -o command= -p "$leader" 2>/dev/null | grep -q -- "run_lane.sh $SLOT\$" || return 0
  mine="$(ps -o pgid= -p $$ | tr -d ' ')"
  groups="$(agent_groups "$leader" | grep -v -x -e "$mine" -e 0 -e 1 | sort -u | tr '\n' ' ')"
  [ -n "$groups" ] || return 0
  echo "stale lane lock: stopping the orphaned agent's process groups $groups" >&2
  for g in $groups; do kill -TERM -- "-$g" 2>/dev/null || true; done
  for _ in {1..50}; do
    alive=""
    for g in $groups; do kill -0 -- "-$g" 2>/dev/null && alive=1; done
    [ -n "$alive" ] || return 0
    sleep 0.1
  done
  for g in $groups; do kill -KILL -- "-$g" 2>/dev/null || true; done
}

# One runner per lane. mkdir is atomic; a stale lock from a killed run is
# cleared when its pid is gone.
if ! mkdir "$LOCK" 2>/dev/null; then
  if kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then exit 0; fi
  stop_orphaned_agent "$(cat "$LOCK/pgid" 2>/dev/null || true)"
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
echo $$ > "$LOCK/pid"
# The check's scratch home (CHECK_HOME, a few hundred MB once a check has
# built its caches) goes with the runner, before the lock is let go, so run/
# holds one only while its lane is running (loom#643).
drop_check_home() { [ -z "${CHECK_HOME:-}" ] || rm -rf "$CHECK_HOME"; }
# LOOM_DROP_TREES=1 (a lane pod; docs/LANES.md, "Lane pods"): a run's scratch
# goes when the run ends, whatever its outcome: its tree, the runner's files
# beside it, the agent's scratch home and the check's. The station prunes
# trees on the Mac (prune_trees) but cannot see a pod's disk, and a pod's
# scratch volume is sized for the runs in flight, not for kept trees. Logs and
# the brief stay where the station reads them. Called before each brief and
# when the runner exits.
drop_run_scratch() {
  [ "${LOOM_DROP_TREES:-}" = 1 ] && [ "$LANE" != agy ] || return 0
  drop_check_home
  [ -z "${CHECK_HOME:-}" ] || rm -rf "$CHECK_HOME-default"
  [ -z "${AGENT_HOME:-}" ] || rm -rf "$AGENT_HOME"
  [[ "${TICKET:-}" =~ ^[A-Za-z0-9._-]+$ ]] && [ -n "${WORKDIR:-}" ] || return 0
  local f
  for f in "" -default-check .answers .described-step .final-message .last-message .no-change.md .opencode-events .run.md .summary.md; do
    rm -rf "${WORKDIR:?}/$TICKET$f"
  done
}
# When a run ends, drop the build output git ignores from its tree and its
# default-branch check copy: .venv, node_modules, __pycache__ and
# .pytest_cache folders that `git check-ignore` confirms and that hold no
# tracked path. The tracked tree stays for fix rounds. On 2026-10-25 an infra
# run left 17 ignored .venv folders, about 2 GB a tree, kept for TREE_KEEP
# (loom#1808). find never follows a link, so a linked folder is never entered
# or matched, and rm -rf removes links inside without following them. Called
# before each brief (for the run before it) and when the runner exits.
drop_build_output() {
  [[ "${TICKET:-}" =~ ^[A-Za-z0-9._-]+$ ]] && [ -n "${WORKDIR:-}" ] || return 0
  local tree path tracked
  for tree in "$WORKDIR/$TICKET" "$WORKDIR/$TICKET-default-check"; do
    [ -d "$tree" ] && [ ! -L "$tree" ] && [ -e "$tree/.git" ] || continue
    while IFS= read -r -d '' path; do
      # find names every path ./..., so none reads as an option.
      [[ "$path" == ./* ]] || continue
      git -C "$tree" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
        check-ignore -q -- "$path" 2> /dev/null || continue
      tracked="$(git -C "$tree" -c core.fsmonitor=false ls-files -- "$path" 2> /dev/null)" || continue
      [ -z "$tracked" ] || continue
      rm -rf -- "${tree:?}/$path" 2> /dev/null || true
    done < <(cd "$tree" && find . -name .git -prune -o -type d \( -name .venv -o -name node_modules \
               -o -name __pycache__ -o -name .pytest_cache \) -prune -print0 2> /dev/null)
  done
  return 0
}

# The runner writes its usage heartbeat between runs, not during one, and a
# run can take over an hour (loom#1281). While this runner lives, a background
# loop stamps state/usage/<slot>.alive with the time every
# LOOM_KEEPALIVE_SECONDS, so the station reads a lane deep in a run as alive
# whatever its heartbeat's age or its lock says. The loop ends when this
# runner's pid is gone (a killed runner), and the stamp goes with the runner;
# a stamp left by a runner killed hard goes stale after the station's window.
# The stamp is no heartbeat: it says nothing of availability, so a busy lane
# is not offered more work and no limit or hold is hidden.
# The loop sleeps through `wait` so a TERM ends it at once, and the runner
# waits for it to exit before removing the stamp: a loop still mid-write
# after the remove left the stamp behind.
ALIVE="$LOOM_HOME/state/usage/$SLOT.alive"
KEEPALIVE_PID=""
keepalive_loop() {
  local parent="$1" every="${LOOM_KEEPALIVE_SECONDS:-60}" tmp="$ALIVE.$1" sleeper=""
  [[ "$every" =~ ^[1-9][0-9]*$ ]] || every=60
  trap '[ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; rm -f "$tmp"; exit 0' TERM
  while kill -0 "$parent" 2>/dev/null; do
    date +%s > "$tmp" 2>/dev/null && mv -f "$tmp" "$ALIVE" 2>/dev/null
    sleep "$every" & sleeper=$!
    wait "$sleeper"
  done
  rm -f "$tmp"
}
stop_keepalive() {
  [ -z "$KEEPALIVE_PID" ] || { kill "$KEEPALIVE_PID" 2>/dev/null; wait "$KEEPALIVE_PID" 2>/dev/null; }
  rm -f "$ALIVE"
}
mkdir -p "$LOOM_HOME/state/usage"
keepalive_loop $$ > /dev/null 2>&1 &
KEEPALIVE_PID=$!
trap 'drop_build_output; drop_run_scratch; drop_check_home; stop_keepalive; rm -rf "$LOCK"' EXIT

AGENT_TIMEOUT="${LOOM_AGENT_TIMEOUT:-3600}"
# Agent retries in one run for a pre-push refusal (prepush_gate).
PREPUSH_RETRIES="${LOOM_PREPUSH_RETRIES:-1}"
# Seconds a retry run gets: short, so the job's own limit (lane.yml) still
# holds the first run, the retry, the check and the push.
PREPUSH_TIMEOUT="${LOOM_PREPUSH_TIMEOUT:-1200}"
NET_TIMEOUT="${LOOM_NET_TIMEOUT:-120}"
CHECK_TIMEOUT="${LOOM_CHECK_TIMEOUT:-1800}"
REPO_OWNERS=" ${LOOM_REPO_OWNERS:-} "
# A lane with GitHub App settings (LOOM_GITHUB_APP_*, the Loom Lanes App)
# writes as that App, never as the operator (loom#1142): lanes opened 58 pull
# requests as the operator in a day. It clones over https, and each run mints
# an installation token for gh and git (lane_app_token). The agent never gets
# the token in its environment, nor the App's settings or git's helper: its gh
# reads the token from a per-run gh config directory (agent_gh_config, loom#665).
# LOOM_APP_TOKEN_CMD, when set, prints the token in place of
# `python3 -m bus app token`.
if [ -n "${LOOM_GITHUB_APP_ID:-}" ]; then
  CLONE_BASE="${LOOM_CLONE_BASE:-https://github.com/}"
  # git asks gh for github.com credentials, and gh answers with GH_TOKEN.
  export GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=credential.https://github.com.helper \
    GIT_CONFIG_VALUE_0="" GIT_CONFIG_KEY_1=credential.https://github.com.helper \
    GIT_CONFIG_VALUE_1="!gh auth git-credential"
else
  CLONE_BASE="${LOOM_CLONE_BASE:-git@github.com:}"
fi
WORKDIR="$LOOM_HOME/worktrees"
LOGDIR="$LOOM_HOME/logs/$SLOT"
# The agy lane's agent runs as a dedicated macOS user (default "loom") that
# cannot read this user's home or keychain. agy keeps its sign-in in that
# user's own keychain, a non-"login" keychain (a "login" keychain will not
# unlock from the command line), unlocked with its password from SSM.
AGY_USER="${LOOM_AGY_USER:-loom}"
AGY_HOME="${LOOM_AGY_HOME:-/Users/$AGY_USER}"
AGY_BIN="${LOOM_AGY_BIN:-$AGY_HOME/.local/bin/agy}"
AGY_KEYCHAIN="${LOOM_AGY_KEYCHAIN:-$AGY_HOME/Library/Keychains/loom.keychain-db}"
if [ -n "${LOOM_AGY_RUNAS:-}" ]; then AGY_RUNAS=("$LOOM_AGY_RUNAS"); else AGY_RUNAS=(sudo -n -u "$AGY_USER" -H); fi
# Its trees live in a folder both users can write (ACLs set up with the user).
[ "$LANE" = agy ] && WORKDIR="${LOOM_AGY_WORKDIR:-$AGY_HOME/lanes}"
mkdir -p "$WORKDIR" "$LOGDIR"
# launchd starts with a bare PATH; the agent CLIs live in these.
PATH="$PATH:$HOME/.local/bin:$HOME/.opencode/bin:/opt/homebrew/bin:/usr/local/bin"

# A check that hashes a file needs a sha256sum this machine can execute. The
# lane plist puts the Intel Homebrew bin directory ahead of everything but the
# arm64 one, and an x86_64 coreutils link there never runs on an arm64 host
# without Rosetta (#2317). The macOS system binary sits in /sbin, on no lane's
# PATH, so its directory goes in just before the one that holds the broken
# tool: it can shadow only that directory and those after it.
tool_runs() { printf '' | "$1" > /dev/null 2>&1; }
runnable_sha256sum_on_path() {
  local found dir holder tail
  found="$(command -v sha256sum)"
  [ -n "$found" ] && tool_runs "$found" && return 0
  for dir in ${LOOM_SHA256SUM_DIRS:-/sbin /usr/bin /bin}; do
    [ -x "$dir/sha256sum" ] && tool_runs "$dir/sha256sum" || continue
    if [ -n "$found" ]; then
      holder="$(dirname "$found")"
      case "$PATH" in
        "$holder":*) PATH="$dir:$PATH" ;;
        *) tail="${PATH#*:"$holder":}"; PATH="${PATH%%:"$holder":*}:$dir:$holder:$tail" ;;
      esac
    else
      PATH="$PATH:$dir"
    fi
    return 0
  done
}
runnable_sha256sum_on_path

field() { grep -m1 "^$1:" "$2" | cut -d' ' -f2-; }

# LOOM_<LANE>_<SUFFIX>, e.g. opencode-go + MODEL -> LOOM_OPENCODE_GO_MODEL.
# A slot's own LOOM_<SLOT>_<SUFFIX> comes first, so one slot can run another
# model on the same provider (opencode-go-3 -> LOOM_OPENCODE_GO_3_MODEL).
setting_var() { printf 'LOOM_%s_%s' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')" "$2"; }
lane_var() { setting_var "$LANE" "$1"; }
lane_setting() {
  local s v value route
  s="$(setting_var "$SLOT" "$1")" v="$(lane_var "$1")"
  value="${!s:-${!v:-}}"
  if [ -z "$value" ] && [ "$LANE" = free ] && [ "$1" = URL ] && [ -n "${LOOM_FREE_GATEWAY_URL:-}" ]; then
    case "$SLOT" in
      free-ollama) route=/free/ollama-cloud/v1 ;;
      free-gemma) route=/free/gemini/v1 ;;
      free-nvidia) route=/free-public/nvidia-nim/v1 ;;
      free-kilo) route=/free-public/kilo/v1 ;;
      free-groq) route=/free-public/groq/v1 ;;
      free-openrouter) route=/free-public/openrouter/v1 ;;
    esac
    value="${LOOM_FREE_GATEWAY_URL%/}$route"
  fi
  printf '%s' "$value"
}

# Each pass records whether this lane can take work. The dispatcher routes
# only to lanes with a fresh, available heartbeat. An optional third argument
# is the epoch time a provider limit lifts; until then every pass stays
# unavailable without reading the key or taking a brief.
# A lane with a model ladder (LOOM_<SLOT>_MODELS) stays available while one
# model is limited, so the beat also names each model limited now and the
# epoch it lifts ("limited_models"). The station reads it so a ticket pinned
# to that model is not claimed for this lane again before the reset: a pinned
# model's limit parked the same ticket here every pass, 24 claims in 90
# minutes, while the beat said available.
BEAT="$LOOM_HOME/state/usage/$SLOT.json"
write_beat() {
  local slot="$1" reason retry="" models
  # shellcheck disable=SC1003 # a literal backslash is one of the deleted characters
  reason="$(printf '%s' "$3" | tr -d '"\\' | tr '[:cntrl:]' ' ')"
  [[ "${4:-}" =~ ^[0-9]+$ ]] && retry=", \"retry_at\": $4"
  models="$(limited_models_json "$slot")"
  mkdir -p "$LOOM_HOME/state/usage"
  printf '{"lane": "%s", "at": %s, "available": %s, "reason": "%s"%s, "limited_models": {%s}}\n' \
    "$slot" "$(date +%s)" "$2" "$reason" "$retry" "$models" > "$LOOM_HOME/state/usage/$slot.json.tmp" &&
    mv "$LOOM_HOME/state/usage/$slot.json.tmp" "$LOOM_HOME/state/usage/$slot.json"
}
# The slot's model limits that have not lifted, as JSON object members
# ("model": epoch, ...), from its MODEL_LIMIT_FILE rows; only names a model
# line may carry.
limited_models_json() {
  local file="$LOOM_HOME/state/usage/$1.models"
  [ -f "$file" ] || return 0
  awk -v now="$(date +%s)" '
    $1 ~ /^[A-Za-z0-9._:][A-Za-z0-9._:\/@-]*$/ && $2 ~ /^[0-9]+$/ && $2 + 0 > now + 0 {
      printf "%s\"%s\": %s", (n++ ? ", " : ""), $1, $2
    }' "$file"
}
heartbeat() { write_beat "$SLOT" "$@"; }

# An empty API credit holds every slot that shares it, not only the one that
# hit it: each gets the same unavailable heartbeat until $1. The shared set
# is bus/factory.py's, so the factory and this runner agree on it.
hold_shared_credit() {
  local until="$1" slots slot
  slots="$(PYTHONPATH="$LOOM_ROOT" python3 -B -m bus.factory credit-slots 2>/dev/null)" || return 0
  case $'\n'"$slots"$'\n' in *$'\n'"$SLOT"$'\n'*) ;; *) return 0 ;; esac
  for slot in $slots; do
    write_beat "$slot" false "limit: out of credit; unavailable until $(utc "$until")" "$until"
  done
}

utc() { date -u -r "$1" +%FT%TZ 2>/dev/null || date -u -d "@$1" +%FT%TZ; }

# How a run's agent CLI is invoked (claude, codex) and how a failed run is
# classed (classify_failure), shared with lanes/mac/brain.sh.
# shellcheck source=lanes/agent_cli.sh
. "$LOOM_ROOT/lanes/agent_cli.sh"

# Body of one "## <name>" section of the brief, blank lines dropped.
section() {
  awk -v h="## $1" '$0 == h {on=1; next} /^##? / {on=0} on' "$2" | sed '/^[[:space:]]*$/d'
}

# The acceptance criteria of $1, blank lines dropped: its "## Acceptance
# criteria" section, in any case, or its "## Acceptance" section. Tickets
# write either heading; read as exactly "## Acceptance criteria", a ticket
# with "## Acceptance" opened a pull request with an empty section that the
# review bot's plan check failed on every fix round (2026-10-25).
# bus/pr_repair.py::_brief_criteria reads the same headings.
criteria_section() {
  awk '{h = tolower($0); sub(/[ \t]+$/, "", h)}
       h == "## acceptance criteria" || h == "## acceptance" {on=1; next}
       /^#+ / {on=0} on' "$1" | sed '/^[[:space:]]*$/d'
}

# Chief's format: ## Intent (the change class), ## Why, ## Summary,
# ## Acceptance criteria, Size:, ## Out of scope.
INTENT_CLASS=""
INTENT_TEXT=""
SENSITIVE_FILE=.loom/sensitive-paths
SUMMARY_FILE=.loom/summary.md
# The section a lane fills with the steps a person takes before its change
# merges, or "none" (bus/operator_hold.py holds a pull request that lists any).
OPERATOR_SECTION="Operator steps before merge"
# The section a lane fills with bugs it found outside its ticket, each
# citing a real issue (bus/bug_refs.py checks every one).
BUGS_SECTION="Bugs discovered"

# What the agent is told about the Intent summary: one line per behavior
# change in .loom/summary.md, else the runner falls back to the ticket's
# What section, or the reason a run changed nothing (no_change_reason). The
# file itself is never committed.
summary_note() {
  printf '\n\n## Intent summary\nWhen you finish, write one line per behavior change to %s at the top of the tree (plain text, one change per line). The runner reads it for the pull request Intent section and never commits it. Also include a ## Acceptance criteria section containing the ticket criteria as checkboxes: tick each criterion the change builds; leave every live check (a Live line, or anything seen on a running system, a host, a network or over time) and every operator action unticked, since only the station'"'"'s verifier ticks those; and for an unbuilt criterion append \"; not built: <reason>\". A reason saying the work is already there must cite the code on the base branch as <path>:<line> or <path>::<symbol>, or the pull request goes back. The runner puts that checklist in the pull request body. Also include a ## Risk section with two lines: \"Door: one-way\" when the change cannot be walked back by a revert, else \"Door: two-way\"; and \"Blast radius: <one word>\" naming what a mistake reaches, such as ticket, repo or fleet. The runner writes the Risk line from them, and a missing answer reads as door unstated. Also include a ## %s section: each step a person must take before this change can merge and deploy (a data migration, a cutover, a VM or disk resize, scaling a workload down), one per line, or the single word none. Loom never merges a pull request that lists a step: its ticket goes to the operator. If you found a bug outside this ticket, add a ## %s section, one bug per line, each citing its issue as #N (this repo) or OWNER/NAME#N; file the issue first if none exists. The station looks each issue up and leaves out any line whose issue does not exist, is a pull request, or is closed and was not filed in the last two days. If there is no behavior change, leave the file out, unless you change nothing because the work is already done or no longer applies: then write that reason there (for example \"nothing to change: the finding no longer holds\"), and it is recorded on the ticket as the answer.\n' "$SUMMARY_FILE" "$OPERATOR_SECTION" "$BUGS_SECTION"
}

# How the agent is told to work, after its brief. On 2026-10-14, 27 of 95
# tickets took six or more rounds: no lane ran a test before opening its pull
# request, and most fix rounds answered review findings a checklist covers.
# On 2026-10-22 a lane test imported a package the repo's CI does not install
# (its AGENTS.md rules), and another redid work its operator notes said had
# shipped in other repos.
work_note() {
  printf '\n\n## How to work\n- Before you change anything, read the repo'"'"'s AGENTS.md and CLAUDE.md and the docs they point to: they state rules its CI enforces, such as which packages a test may import.\n- When the Operator notes or a merged pull request under Linked pull requests show the work is already done or belongs elsewhere, change nothing and say why in the Intent summary.\n- Work test-first, as the implement skill says: write the test the acceptance criteria name, run it and see it fail, make the change, run it and see it pass.\n- Before you finish, run the repo'"'"'s full test suite (or the validation check below) and fix what fails. A pull request with red checks comes straight back.\n- The review bot checks every change: keep each new function small (cyclomatic complexity under 15, cognitive under 25), never add a branch to a function already over either cap (put the new logic in a new small function and call it), never swallow an error, leave no dead code, keep docs and comments in step with the code, and write ASCII only.\n- Do not poll pull request checks or reviews. Open or update the pull request, then stop; the station reads checks and hands work back.\n- For a public repo, use documentation-range IP addresses (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) in tests. If a range bound must appear deliberately, add leak-guard-allow to that line. In a public repo, never write a reference to another repo'"'"'s issue in the OWNER/NAME#<number> or NAME#<number> shape, not even a made-up one in a test fixture: write NAME#N, or add leak-guard-allow to that line when the shape is what the test is about.\n- Add new files only where the ticket points: a new file the brief does not name, at the top level or under a new or hidden top-level directory, gets the commit refused and nothing pushed.\n- Commit your work. A run that ends with no commit fails the ticket, unless it says in the Intent summary why there is nothing to change.\n'
}

# Lists the sandbox probes and out-of-path scratch files the agent added
# (BASE..HEAD), then removes them from the branch. A never-pushed branch is
# rebuilt without them so no commit carries them; a fix round gets a
# removal commit on top so the push stays fast-forward. Names each file
# dropped on stdout and sets DROPPED_FILES and DROPPED_SCRATCH_FILES.
drop_agent_files() {
  local probes scratch dropped f stamp
  DROPPED_FILES=""
  DROPPED_SCRATCH_FILES=""
  probes="$(PYTHONPATH="$REPO_ROOT" python3 -B -m bus.probe_drop "$TREE" "$BASE" HEAD 2>/dev/null)" || return 1
  scratch="$(PYTHONPATH="$REPO_ROOT" python3 -B -m bus.scope_check --scratch "$TREE" "$BASE" HEAD "$BRIEF" ${FIX_UPSTREAM:+"$FIX_UPSTREAM"})" || return 1
  dropped="$(printf '%s\n%s\n' "$probes" "$scratch" | sed '/^$/d' | sort -u)"
  [ -n "$dropped" ] || return 0
  stamp="$(date -u +%FT%TZ)"
  while IFS= read -r f; do
    safe_git rm -q -f -- "$f" || return 1
    if printf '%s\n' "$probes" | grep -Fxq -- "$f"; then
      printf '[%s] %s dropped sandbox probe file: %s\n' "$stamp" "$TICKET" "$f"
    else
      printf '[%s] %s dropped scratch file: %s\n' "$stamp" "$TICKET" "$f"
    fi
  done <<EOF
$dropped
EOF
  DROPPED_FILES="$probes"
  DROPPED_SCRATCH_FILES="$scratch"
  if [ -z "$FIX_PR" ] && [ -z "$CONFLICTS" ]; then
    safe_git reset -q --soft "$BASE" || return 1
    safe_git add -A || return 1
    safe_git diff --cached --quiet && return 0
    safe_git commit -qm "${TITLE:-$TICKET}" || return 1
  else
    safe_git diff --cached --quiet && return 0
    safe_git commit -qm "Drop agent scratch files: ${dropped//$'\n'/, }" || return 1
  fi
}

# Sets STRAY_FILES to the files the agent's commits (BASE..HEAD) add outside
# the brief: not named by it (as a path or a directory above one) and not
# under a non-hidden top-level directory the base already tracks
# (bus/scope_check.py). A fix round that merged the default branch passes
# that branch, so files the merge brought in are never stray. Returns 1 when
# the check could not run, so a broken check never reads as a clean one.
check_scope() {
  STRAY_FILES="$(timeout "$NET_TIMEOUT" env PYTHONPATH="$REPO_ROOT" python3 -B -m bus.scope_check "$TREE" "$BASE" HEAD "$1" ${FIX_UPSTREAM:+"$FIX_UPSTREAM"})"
}

# Checks the commits' Kubernetes manifest changes against the live cluster,
# SSM and DNS (bus/live_cluster.py), over the range the leak scan reads. A
# miss parks the brief as work and returns 1, so nothing is pushed. A check
# that cannot run at all, or a part of it, sets LIVE_NOTES, which the log and
# the pull request body or round comment carry. Hosts are looked up only
# under the zones in LOOM_LIVE_CHECK_ZONES (docs/LANES.md, "Live-cluster
# check").
LIVE_NOTES=""
live_cluster_check() {
  local range="$BASE..HEAD" out rc=0
  LIVE_NOTES=""
  [ -n "$FIX_UPSTREAM" ] && range="$FIX_UPSTREAM...HEAD"
  out="$(timeout "$NET_TIMEOUT" env PYTHONPATH="$REPO_ROOT" ${LOOM_AWS_REGION:+AWS_REGION="$LOOM_AWS_REGION"} \
    python3 -B -m bus.live_cluster "$TREE" "$range" 2>> "$LOG")" || rc=$?
  if [ "$rc" -eq 1 ]; then
    park "$BRIEF" "$out; nothing pushed"
    return 1
  fi
  [ -z "$out" ] || printf '%s\n' "$out" >> "$LOG"
  if [ "$rc" -ne 0 ]; then
    out="live-cluster check could not run: the check exited $rc"
    printf '%s\n' "$out" >> "$LOG"
  fi
  LIVE_NOTES="$(printf '%s\n' "$out" | grep '^live-cluster check could not run: ' || true)"
}

# STRAY_FILES as one comma-separated line, at most ten names.
stray_list() {
  local n
  n="$(printf '%s\n' "$STRAY_FILES" | sed '/^$/d' | wc -l | tr -d ' ')"
  printf '%s\n' "$STRAY_FILES" | sed '/^$/d' | head -10 | paste -sd ',' - | sed 's/,/, /g'
  [ "$n" -le 10 ] || printf ' and %s more' "$((n - 10))"
}

# The ## Intent section for the top of a new lane PR body: the change
# class, one line per behavior change, and the counts of files by class.
# Sensitive globs come from SENSITIVE_FILE on the base revision; a missing
# file means no sensitive class. Sets INTENT_CLASS, INTENT_TIER and
# INTENT_TEXT, with the Risk line bus/risk_line.py computes under the class.
# The scratch files live under $LOOM_HOME/run, never the default temp
# directory: a lane sandbox denies that one, and a failed mktemp handed cp
# an empty target, which macOS cp takes as the working directory.
prepare_intent() {
  local brief="$1" dir files_tmp sens_tmp sum_tmp what_tmp full_tmp raw_tmp intent_tmp out risked
  INTENT_CLASS="logic"
  INTENT_TIER=""
  INTENT_TEXT=""
  dir="$(mktemp -d "$LOOM_HOME/run/$SLOT-intent.XXXXXX")" ||
    { echo "intent: could not make a scratch directory; class stays $INTENT_CLASS" >&2; return 0; }
  files_tmp="$dir/files" sens_tmp="$dir/sensitive" sum_tmp="$dir/summary" what_tmp="$dir/what"
  full_tmp="$dir/full" raw_tmp="$dir/raw" intent_tmp="$dir/intent"
  safe_git diff --name-only "$BASE" HEAD 2>/dev/null | sed '/^[[:space:]]*$/d' > "$files_tmp"
  safe_git cat-file blob "$BASE:$SENSITIVE_FILE" 2>/dev/null > "$sens_tmp"
  # Only the change lines before the summary's first heading: its
  # ## Acceptance criteria checklist goes to the body's own section.
  if [ -f "$WORKDIR/$TICKET.summary.md" ]; then
    awk '/^#+ /{exit} {print}' "$WORKDIR/$TICKET.summary.md" > "$sum_tmp"
    cp "$WORKDIR/$TICKET.summary.md" "$full_tmp" 2>/dev/null
  elif [ -f "$TREE/$SUMMARY_FILE" ]; then
    awk '/^#+ /{exit} {print}' "$TREE/$SUMMARY_FILE" > "$sum_tmp" 2>/dev/null
    cp "$TREE/$SUMMARY_FILE" "$full_tmp" 2>/dev/null
  fi
  section What "$brief" > "$what_tmp" 2>/dev/null
  safe_git -c core.quotePath=false diff --raw -M "$BASE" HEAD 2>/dev/null > "$raw_tmp"
  out="$(PYTHONPATH="$REPO_ROOT" python3 -B -m bus.change_class --files "$files_tmp" --sensitive "$sens_tmp" --summary "$sum_tmp" --what "$what_tmp" 2>/dev/null)"
  [ -n "$out" ] || { rm -rf "$dir"; return 0; }
  INTENT_CLASS="$(printf '%s' "$out" | tail -1 | sed 's/^class://')"
  case "$INTENT_CLASS" in sensitive|logic|tests|docs) ;;
    *) INTENT_CLASS="logic" ;;
  esac
  INTENT_TEXT="$(printf '%s' "$out" | sed '$d')"
  # A failed Risk read costs the line and the label, never the pull request.
  printf '%s\n' "$INTENT_TEXT" > "$intent_tmp"
  risked="$(PYTHONPATH="$REPO_ROOT" python3 -B -m bus.risk_line --repo "$REPO" --class "$INTENT_CLASS" --intent "$intent_tmp" --raw "$raw_tmp" --summary "$full_tmp" --brief "$brief" 2>/dev/null)" || risked=""
  rm -rf "$dir"
  [ -n "$risked" ] || { echo "intent: no Risk line; the pull request opens without it" >&2; return 0; }
  INTENT_TIER="$(printf '%s' "$risked" | tail -1)"
  INTENT_TEXT="$(printf '%s' "$risked" | sed '$d')"
}

# The verified bugs section of the lane's summary, as "## Bugs discovered"
# and its lines, or nothing: a line whose cited issue does not check out is
# left out, and bus/bug_refs.py says which on stderr (the lane log). No
# outer timeout: each gh lookup runs under its own NET_TIMEOUT, which kills
# that gh call, and the section is capped at bug_refs.MAX_LINES lookups.
bugs_part() {
  local file="$WORKDIR/$TICKET.summary.md" private=() lines
  [ -f "$file" ] || file="$TREE/$SUMMARY_FILE"
  [ -f "$file" ] || return 0
  repo_is_public || private=(--private)
  lines="$(NET_TIMEOUT="$NET_TIMEOUT" PYTHONPATH="$REPO_ROOT" \
    python3 -B -m bus.bug_refs "$file" --repo "$REPO" ${private[@]+"${private[@]}"} 2>> "$LOG")" ||
    { echo "bugs section check failed; the section is left out" >> "$LOG"; return 0; }
  [ -z "$lines" ] || printf '## %s\n%s' "$BUGS_SECTION" "$lines"
}

pr_body() {
  local size issue refs="" criteria ops bugs why summary
  issue="$(field Issue "$1" || true)"
  [[ "$issue" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+$ ]] && refs="$(printf '\nRefs %s' "$issue")"
  # The size as the review bot's letter (Medium -> M), so its plan check
  # passes whatever word the brief used; an unknown size is kept as written.
  # bus.hunt_plan reads a ## Size section or an inline "Size: S" line.
  size="$(PYTHONPATH="$REPO_ROOT" python3 -B -m bus.hunt_plan brief-size "$1" 2>/dev/null ||
    section Size "$1" | head -1 | awk '{print $1}' | tr -d ':')"
  local validated=""
  [ -n "$HAS_CONTRACT" ] && validated="$(printf '\nValidation: %s (%s).' "$VALIDATED" "$CONTRACT_FILE")"
  [ -n "$INTENT_TEXT" ] || prepare_intent "$1"
  # The lane's checklist, from the summary the runner moved out of the tree
  # before committing: read from the tree, every lane pull request carried
  # the ticket's unticked criteria and went back for them (2026-10-20).
  criteria="$(criteria_section "$WORKDIR/$TICKET.summary.md" 2>/dev/null)"
  [ -n "$criteria" ] || criteria="$(criteria_section "$TREE/$SUMMARY_FILE" 2>/dev/null)"
  [ -n "$criteria" ] || criteria="$(criteria_section "$1")"
  # The lane's own word on operator steps, else the ticket's; a body with
  # neither says so, and the factory's keyword backstop still reads both.
  ops="$(section "$OPERATOR_SECTION" "$WORKDIR/$TICKET.summary.md" 2>/dev/null)"
  [ -n "$ops" ] || ops="$(section "$OPERATOR_SECTION" "$TREE/$SUMMARY_FILE" 2>/dev/null)"
  [ -n "$ops" ] || ops="$(section "$OPERATOR_SECTION" "$1")"
  [ -n "$ops" ] || ops="none stated"
  # Command substitution drops the trailing blank line the format needs.
  bugs="$(bugs_part)"
  [ -z "$bugs" ] || bugs="$bugs"$'\n\n'
  # Briefs write the change under "## What" or "## Summary"; an empty Why
  # takes that change, so the plan check's ## Why is not empty (loom#1468).
  # Keep in step with bus/pr_repair.py::_render_generated.
  why="$(section Why "$1")"
  summary="$(section What "$1")"
  [ -n "$summary" ] || summary="$(section Summary "$1")"
  [ -n "$why" ] || why="$summary"
  printf '%s\n\n## Why\n%s\n\n## Summary\n%s\n\n## Acceptance criteria\n%s\n\nSize: %s\n\n## Out of scope\n%s\n\n## %s\n%s\n\n%s---\nLoom ticket %s, lane %s, model %s.%s%s\n' \
    "$INTENT_TEXT" "$why" "$summary" "$criteria" \
    "$size" "$(section 'Out of scope' "$1")" "$OPERATOR_SECTION" "$ops" "$bugs" \
    "$2" "$LANE" "$MODEL" "$validated" "$refs"
}

# The markers bus/pr_repair.py names PLAN_HOLD_MARKER (in a pull request
# body) and PLAN_NOTE_MARKER (the ticket's one plan-hold note).
PLAN_HOLD_MARKER="<!-- loom:plan-hold v1 -->"
PLAN_NOTE_MARKER="<!-- loom:plan-hold-note v1 -->"

# The body of a new pull request, in PR_BODY. A body that fails the review
# bot's plan checks (bus/hunt_plan.py) has the failing parts rebuilt from the
# brief by bus/pr_repair.py, the code the factory and fix rounds use. What the
# brief still lacks goes in PLAN_GAPS, one per line: the pull request opens
# anyway (the work is done), marked so the factory sends it no lane round,
# whose agent edits code and never the body (loom#1837).
compose_pr_body() {
  local problems body_file out_file summary=() private=()
  PLAN_GAPS=""
  PR_BODY="$(pr_body "$BRIEF" "$TICKET")"
  problems="$(printf '%s' "$PR_BODY" | PYTHONPATH="$REPO_ROOT" python3 -B -m bus.hunt_plan check - 2>&1)" && return 0
  echo "[$(date -u +%FT%TZ)] $TICKET pull request body fails the plan checks: $(printf '%s' "$problems" | tr '\n' ';')" >> "$LOG"
  if ! body_file="$(mktemp)" || ! out_file="$(mktemp)"; then
    PLAN_GAPS="$problems"; return 0
  fi
  printf '%s\n' "$PR_BODY" > "$body_file"
  if [ -f "$WORKDIR/$TICKET.summary.md" ]; then
    summary=(--summary "$WORKDIR/$TICKET.summary.md")
  elif [ -f "$TREE/$SUMMARY_FILE" ]; then
    summary=(--summary "$TREE/$SUMMARY_FILE")
  fi
  repo_is_public || private=(--private)
  if PLAN_GAPS="$(NET_TIMEOUT="$NET_TIMEOUT" PYTHONPATH="$REPO_ROOT" python3 -B -m bus.pr_repair plan \
       "$BRIEF" "$body_file" "$LANE" "$MODEL" "$TICKET" --out "$out_file" --repo "$REPO" \
       ${summary[@]+"${summary[@]}"} ${private[@]+"${private[@]}"} 2>> "$LOG")"; then
    PR_BODY="$(cat "$out_file")"
    PLAN_GAPS=""
    echo "[$(date -u +%FT%TZ)] $TICKET pull request body rebuilt from the brief; it passes the plan checks" >> "$LOG"
  else
    [ -n "$PLAN_GAPS" ] || PLAN_GAPS="$problems"
    echo "[$(date -u +%FT%TZ)] $TICKET the brief lacks for the plan checks: $(printf '%s' "$PLAN_GAPS" | tr '\n' ';')" >> "$LOG"
  fi
  rm -f "$body_file" "$out_file"
}

# The ticket's one plan-hold note, edited in place on every later pull
# request of the ticket: the factory holds the ticket for the brain and keeps
# the same comment. Argument: the new pull request's URL.
note_plan_hold() {
  local number="${1##*/}"
  [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]] || return 0
  printf 'The ticket lacks what the runner writes these parts of the body from:\n%s\n\nA lane cannot edit the ticket, so the factory sends no fix round for it. Add the missing parts to this ticket; the factory then rebuilds the pull request body from it.\n' \
    "$(printf '%s\n' "$PLAN_GAPS" | sed 's/^/- /')" |
    upsert_sticky_comment "$REPO" "$ISSUE_NUMBER" "$PLAN_NOTE_MARKER" repeat \
      --headline "Held for the brain: a pull request body fails the review bot's plan check" \
      --items-label "Pull requests" --item "#$number" ||
    echo "[$(date -u +%FT%TZ)] $TICKET: the plan-hold note on the ticket failed" >> "$LOG"
}

# A fix brief's PR must still be open on the brief's branch. Returns 1 with
# FIX_REASON set when it is not, and with FIX_REASON empty when gh could not
# say (the brief then waits for the next pass).
fix_pr_open() {
  local state pr="${1:-$FIX_PR}"
  FIX_REASON=""
  state="$(timeout "$NET_TIMEOUT" gh pr view "$pr" -R "$REPO" --json state,headRefName \
    --jq '.state + " " + .headRefName')" || return 1
  [ "$state" = "OPEN $BRANCH" ] && return 0
  FIX_REASON="pull request #$pr is no longer open on $BRANCH (${state:-no answer})"
  return 1
}

# What a fix brief fixed: the ### headings of its ## Fix section, one line.
fix_items() {
  awk '$0 == "## Fix" {on=1; next} on && /^## / {on=0}
        on && /^### / {sub(/^### /, ""); gsub(/[[:cntrl:]]/, ""); out = out (n++ ? "; " : "") $0}
        on && n && /^Fixing: / {sub(/^Fixing: /, ""); gsub(/[[:cntrl:]]/, ""); out = out " [" $0 "]"}
        on && n && /^Does not apply/ {out = out " [does not apply]"}
        on && n && /^Fixed$/ {out = out " [fixed]"}
        END {print out}' "$1" | cut -c1-500
}

# The checks a fix brief asks a round to fix: the "### Check:" headings of its
# ## Fix section, "; " between them.
fix_checks() {
  awk '$0 == "## Fix" {on=1; next} on && /^## / {on=0}
        on && /^### Check: / {sub(/^### Check: /, ""); gsub(/[[:cntrl:]]/, ""); out = out (n++ ? "; " : "") $0}
        END {print out}' "$1" | cut -c1-500
}

# The fix brief's checks a commit must fix: fix_checks without the review
# bot's own checks (LOOM_REVIEW_CHECK and LOOM_PLAN_CHECK, as bus/factory.py
# names them), which a round may settle with no commit, by answering a
# finding or repairing the PR text.
fix_code_checks() {
  awk -v review="${LOOM_REVIEW_CHECK:-Grug - Elder}" -v plan="${LOOM_PLAN_CHECK:-Grug - Chief}" \
    '$0 == "## Fix" {on=1; next} on && /^## / {on=0}
     on && /^### Check: / {sub(/^### Check: /, ""); gsub(/[[:cntrl:]]/, "")
       if ($0 != review && $0 != plan) out = out (n++ ? "; " : "") $0}
     END {print out}' "$1" | cut -c1-500
}

# The head commit the fix brief's checks failed on (its loom:fix-head marker),
# or nothing.
fix_head() {
  sed -n 's/^<!-- loom:fix-head \([0-9a-f]\{40\}\) -->$/\1/p' "$1" | tail -1
}

# One line for the round's log: whether the repo's validation contract runs a
# check for the failing CI jobs the brief names. The contract's check runs on
# the round's head before any push (final_validation), so a failure there is a
# failed round; a job it does not run is first checked by CI after the push.
fix_contract_line() {
  local checks
  checks="$(fix_checks "$1")"
  [ -n "$checks" ] || return 0
  if [ -z "$HAS_CONTRACT" ]; then
    printf 'no validation contract check maps to the failing checks (%s): the repo has no %s, so CI is the first to run them on the new head' "$checks" "$CONTRACT_FILE"
  elif grep -q "so the lane's own validation never ran it" "$1"; then
    printf 'no validation contract check maps to some failing checks (%s): the brief names CI jobs %s does not run; the contract check %s' "$checks" "$CONTRACT_FILE" "${VALIDATED:-did not pass}"
  else
    printf 'the contract check for the failing checks (%s) ran before push: %s' "$checks" "${VALIDATED:-did not pass}"
  fi
}

# A fix round's commits for its PR comment: at most ten, each hash and
# subject in a code span, so nothing a subject says renders or mentions anyone.
pushed_commits() {
  safe_git log --format='%h%x09%s' "$BASE..HEAD" 2>> "$LOG" | head -10 |
    while IFS=$'\t' read -r sha subject; do
      subject="$(printf '%s' "$subject" | tr -d '`[:cntrl:]' | cut -c1-120)"
      printf -- '- `%s` `%s`\n' "$sha" "$subject"
    done
}

# One comment per subject, edited in place. A lane that repeats a fact (a fix
# round on a pull request, a failure that is still failing) used to post a
# comment each time, and every one emailed the ticket's author and
# participants: about 60 a day on pull requests, 13 on one incident. This finds
# the comment on issue or pull request $2 of $1 that starts with marker $3,
# builds its next body with `python3 -m bus.sticky $4` from the old body and
# the text on stdin (the remaining arguments are bus.sticky's), and edits that
# comment, or posts it when there is none. A failed read posts nothing, so a
# bad API answer cannot start a second comment; a failed write returns 1 for
# the caller to log. Arguments: repo, number, marker, mode, bus.sticky's own.
upsert_sticky_comment() {
  local repo="$1" number="$2" marker="$3" mode="$4" listing id old_file new_file input_file ok=0
  shift 4
  old_file="$(mktemp)" && new_file="$(mktemp)" && input_file="$(mktemp)" || return 1
  cat > "$input_file"
  if ! listing="$(MARKER="$marker" timeout "$NET_TIMEOUT" gh api --paginate \
        "repos/$repo/issues/$number/comments" \
        --jq '.[] | select(.body | startswith(env.MARKER)) | .id' 2>> "$LOG")"; then
    rm -f "$old_file" "$input_file" "$new_file"; return 1
  fi
  id="$(printf '%s\n' "$listing" | head -1 | tr -cd '0-9')"
  : > "$old_file"
  if [ -n "$id" ] && ! timeout "$NET_TIMEOUT" gh api "repos/$repo/issues/comments/$id" \
        --jq .body > "$old_file" 2>> "$LOG"; then
    rm -f "$old_file" "$input_file" "$new_file"; return 1
  fi
  if ! PYTHONPATH="$LOOM_ROOT" python3 -B -m bus.sticky "$mode" --marker "$marker" \
        --now "$(date -u +%FT%TZ)" --old "$old_file" "$@" < "$input_file" > "$new_file" 2>> "$LOG"; then
    rm -f "$old_file" "$input_file" "$new_file"; return 1
  fi
  if [ -n "$id" ]; then
    timeout "$NET_TIMEOUT" gh api -X PATCH "repos/$repo/issues/comments/$id" \
      -F "body=@$new_file" >> "$LOG" 2>&1 || ok=1
  else
    timeout "$NET_TIMEOUT" gh api -X POST "repos/$repo/issues/$number/comments" \
      -F "body=@$new_file" >> "$LOG" 2>&1 || ok=1
  fi
  rm -f "$old_file" "$input_file" "$new_file"
  return "$ok"
}
# The marker on the one comment per pull request that lists a lane's fix rounds.
ROUND_MARKER="<!-- loom:lane-round v1 -->"

# Keep agent-authored review replies outside the commit. The agent cannot call
# GitHub from its sandbox; only the runner posts these after the push succeeds.
capture_replies() {
  local source="$TREE/.loom/replies.json" target="$LOOM_HOME/run/$SLOT-replies.json"
  REPLIES_FILE=""
  [ -e "$source" ] || return 0
  if [ -L "$source" ] || [ ! -f "$source" ]; then
    echo "invalid .loom/replies.json; ignoring non-regular file" >&2
    rm -f "$source" "$target"
    return 0
  fi
  if ! python3 - "$source" "$target" <<'PY'
import json, os, sys
source, target = sys.argv[1:]
try:
    with open(source, encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, list):
        raise ValueError("expected a list")
    for item in value:
        if (not isinstance(item, dict) or not isinstance(item.get("thread"), str)
                or not item["thread"] or not isinstance(item.get("reply"), str)
                or not isinstance(item.get("resolve"), bool)):
            raise ValueError("invalid reply entry")
    with open(target, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
except (OSError, ValueError, TypeError) as error:
    print("invalid .loom/replies.json; ignoring it: %s" % error, file=sys.stderr)
    sys.exit(1)
PY
  then
    rm -f "$source" "$target"
    return 0
  fi
  REPLIES_FILE="$target"
  rm -f "$source"
}

# The reply text reaches gh as a -f field value (a GraphQL variable), never
# spliced into the query or a shell command line, so it cannot inject anything.
# The agent names the thread ids, so each one is checked first: a thread on
# any pull request but this round's own ($REPO#$FIX_PR) is skipped, and so is
# a reply bus/text.py refuses. Resolving a thread is a merge gate input; an
# agent must never answer or resolve one on another pull request.
post_replies() {
  local entry thread body resolve owner text_file
  [ -n "$REPLIES_FILE" ] && [ -f "$REPLIES_FILE" ] || return 0
  while IFS= read -r entry; do
    thread="$(jq -r '.thread' <<< "$entry")"
    body="$(jq -r '.reply' <<< "$entry")"
    resolve="$(jq -r '.resolve' <<< "$entry")"
    owner="$(timeout "$NET_TIMEOUT" gh api graphql \
         -f 'query=query($threadId:ID!){node(id:$threadId){... on PullRequestReviewThread{pullRequest{number repository{nameWithOwner}}}}}' \
         -f "threadId=$thread" \
         --jq '.data.node.pullRequest | "\(.repository.nameWithOwner)#\(.number)"' 2>> "$LOG")" || owner=""
    if [ "$owner" != "$REPO#$FIX_PR" ]; then
      echo "review thread $thread is not on $REPO#$FIX_PR; its reply was skipped" >> "$LOG"
      continue
    fi
    text_file="$(mktemp "${TMPDIR:-/tmp}/loom-reply.XXXXXX")" || continue
    printf '%s' "$body" > "$text_file"
    if ! body="$(checked_text "$text_file" 2>> "$LOG")"; then
      rm -f "$text_file"
      echo "the reply to review thread $thread was refused; it was skipped" >> "$LOG"
      continue
    fi
    rm -f "$text_file"
    if ! timeout "$NET_TIMEOUT" gh api graphql \
         -f 'query=mutation($threadId:ID!,$body:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId,body:$body}){comment{id}}}' \
         -f "threadId=$thread" -f "body=$body" >> "$LOG" 2>&1; then
      echo "reply to review thread $thread failed" >> "$LOG"
      continue
    fi
    if [ "$resolve" = true ] && ! timeout "$NET_TIMEOUT" gh api graphql \
         -f 'query=mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId}){thread{isResolved}}}' \
         -f "threadId=$thread" >> "$LOG" 2>&1; then
      echo "resolving review thread $thread failed" >> "$LOG"
    fi
  done < <(jq -c '.[]' "$REPLIES_FILE")
  rm -f "$REPLIES_FILE"
}

# The review threads a fix brief lists (the "### Thread PRRT_..." lines of its
# ## Fix section) that this round left without an answer: no entry in its
# replies file and no file kept from .loom-answers. Prints them ", "-joined,
# or nothing. The brief requires an answer for every one (bus/tickets.py
# REPLIES_NOTE); an unanswered thread holds the merge gate, so the round says
# which ones it left.
unanswered_threads() {
  python3 - "$BRIEF" "${REPLIES_FILE:-}" "$INBOX/answers/$TICKET" <<'PY' 2>> "$LOG"
import json, os, re, sys
brief, replies, answers = sys.argv[1:]
with open(brief, encoding="utf-8", errors="replace") as stream:
    parts = stream.read().rsplit("\n## Fix\n", 1)
listed = (re.findall(r"^### Thread (PRRT_[A-Za-z0-9_-]{1,100}) ", parts[1], re.M)
          if len(parts) > 1 else [])
answered = set()
if replies and os.path.isfile(replies):
    try:
        with open(replies, encoding="utf-8") as stream:
            answered |= {entry["thread"] for entry in json.load(stream)}
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("the replies file could not be read: %s" % error, file=sys.stderr)
if os.path.isdir(answers):
    answered |= {name[:-3] for name in os.listdir(answers) if name.endswith(".md")}
print(", ".join(dict.fromkeys(t for t in listed if t not in answered)))
PY
}

# Reconcile the generated title/body sections of an existing PR with the
# approved ticket revision in the brief, preserving reviewer-authored text
# outside those sections. Scope changes are logged with old/new body hashes.
# Exit: 0 = updated, 1 = no change needed, 2 = refused, 3 = gh/helper error.
repair_pr_text() {
  local summary=() private=()
  [ -f "$WORKDIR/$TICKET.summary.md" ] && summary=(--summary "$WORKDIR/$TICKET.summary.md")
  repo_is_public || private=(--private)
  NET_TIMEOUT="$NET_TIMEOUT" PYTHONPATH="$REPO_ROOT" python3 -m bus.pr_repair \
    --apply "$FIX_PR" --repo "$REPO" ${summary[@]+"${summary[@]}"} ${private[@]+"${private[@]}"} \
    "$BRIEF" "$LANE" "$MODEL" "$TICKET"
}

# Prints the text of file $1 when it is fit for a ticket comment: at most
# 32 KB of non-empty ASCII that passes bus/text.py's checks. Otherwise says
# why on stderr and fails.
checked_text() {
  PYTHONPATH="$REPO_ROOT" python3 - "$1" <<'PY'
import sys
from pathlib import Path
from bus import text
path = Path(sys.argv[1])
try:
    data = path.read_bytes()
    if len(data) > 32768: raise ValueError("answer is too large")
    value = data.decode("utf-8")
    if not value.strip(): raise ValueError("answer is empty")
    if not value.isascii(): raise ValueError("answer is not ASCII")
    text.check(value)
except Exception as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
sys.stdout.write(value)
PY
}

# Words with which an agent says its ticket's work is already done or no
# longer applies.
NO_CHANGE_RE='nothing (left )?to (change|fix|do)|no (code )?change (is )?needed|no longer (holds?|applies|apply)|already (done|fixed|in place|handled)|does not apply'
# The runner's reason, read by the factory, when a run committed nothing
# and its agent gave no such reason.
NO_REASON="agent made no commit and wrote no reason"

# The longest final message a failure reason carries, in characters.
FINAL_SAID_MAX=600

# What the agent said last, for a run that committed nothing and wrote no
# summary (loom#1362), as one ASCII line cut to FINAL_SAID_MAX, through
# checked_text. Sets FINAL_SAID, empty when the agent said nothing or its words
# were refused, and FINAL_SAID_DUMP=1 when the run's output ended on a tool's
# output alone. bus/final_said.py picks the words: the codex final-message
# file, then the last text event of an opencode run, then the end of the
# run's output (RUN_OUTPUT) with escape sequences removed and only the prose
# after the last line that reads like a file or a command's output. On
# 2026-10-25 five parks quoted a source comment, a git config, a test
# assertion and a colored permission prompt as the agent's reason
# (loom#1799). The agent CLI's own log lines and the runner's timestamped
# lines are left out: retried stream errors once filled the whole quote.
# Local paths read <path>: an agent naming the file its sandbox refused
# failed the text checks, and ten agy runs on 2026-10-24 read "wrote no
# reason" (loom#1736).
FINAL_SAID_SKIP_RE='^timestamp=[^ ]+ level=[A-Z]+ |^\[[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+Z\] '
final_said() {
  local file="$WORKDIR/$TICKET.final-message" line status
  FINAL_SAID="" FINAL_SAID_DUMP=""
  line="$(printf '%s\n' "$RUN_OUTPUT" | { grep -vE "$FINAL_SAID_SKIP_RE" || true; } | tail -n 20 |
          PYTHONPATH="$REPO_ROOT" python3 -B -m bus.final_said \
            --message "$WORKDIR/$TICKET.last-message" --events "$WORKDIR/$TICKET.opencode-events" 2>> "$LOG")"
  status=$?
  [ "$status" -ne 3 ] || { FINAL_SAID_DUMP=1; return 0; }
  [ "$status" -eq 0 ] && [ -n "$line" ] || return 0
  [ "${#line}" -le "$FINAL_SAID_MAX" ] || line="${line:0:FINAL_SAID_MAX}..."
  printf '%s\n' "$line" > "$file"
  FINAL_SAID="$(checked_text "$file" 2>> "$LOG")" || FINAL_SAID=""
  rm -f "$file"
  FINAL_SAID="${FINAL_SAID%$'\n'}"
}

# A run that committed nothing is an answer, not a failure, when its agent
# said why: an Intent summary that the work is already done or no longer
# applies, or an answer to a thread kept aside by keep_answers. Sets
# NO_CHANGE_TEXT to the ticket comment that quotes it. Fails with
# NO_CHANGE_WHY empty when the agent gave no reason, or set to why its
# reason cannot be posted.
no_change_reason() {
  local said="$WORKDIR/$TICKET.no-change.md" summary="$WORKDIR/$TICKET.summary.md" f found=""
  NO_CHANGE_TEXT="" NO_CHANGE_WHY=""
  [ -f "$summary" ] && grep -qiE "$NO_CHANGE_RE" "$summary" && found=1
  compgen -G "$WORKDIR/$TICKET.answers/*.md" > /dev/null && found=1
  [ -n "$found" ] || return 1
  {
    printf 'Lane %s made no change: the agent found this work already done or no longer applying. It said:\n\n' "$SLOT"
    [ -f "$summary" ] && sed 's/^/> /' "$summary"
    for f in "$WORKDIR/$TICKET.answers"/*.md; do
      [ -f "$f" ] || continue
      printf '>\n> On review thread %s:\n' "$(basename "$f" .md)"
      sed 's/^/> /' "$f"
    done
  } > "$said"
  if ! NO_CHANGE_TEXT="$(checked_text "$said" 2>&1)"; then
    NO_CHANGE_WHY="${NO_CHANGE_TEXT:-its reason could not be read}"
    NO_CHANGE_TEXT=""
  fi
  rm -f "$said"
  [ -z "$NO_CHANGE_WHY" ]
}

# Label the ticket loom:answered. gh refuses a label its repo lacks ("'X'
# not found"), and a repo enrolled after the station's last label sync lacked
# loom:answered, so an answer was posted and its ticket left with no state
# (loom#2032). On a failure, create each Loom label the edit names from the
# canonical set (bus/labels.py; one that exists already is left as it is)
# and retry once.
mark_answered() {
  local names=(loom:answered loom:running loom:briefed loom:queued) name spec
  timeout "$NET_TIMEOUT" gh issue edit "$ISSUE_NUMBER" -R "$REPO" --add-label loom:answered \
    --remove-label loom:running,loom:briefed,loom:queued >> "$LOG" 2>&1 && return 0
  for name in "${names[@]}"; do
    spec="$(PYTHONPATH="$REPO_ROOT" python3 -B -m bus.labels spec "$name" 2>> "$LOG")" || return 1
    timeout "$NET_TIMEOUT" gh label create "$name" -R "$REPO" --color "${spec%%$'\t'*}" \
      --description "${spec#*$'\t'}" >> "$LOG" 2>&1 || true
  done
  timeout "$NET_TIMEOUT" gh issue edit "$ISSUE_NUMBER" -R "$REPO" --add-label loom:answered \
    --remove-label loom:running,loom:briefed,loom:queued >> "$LOG" 2>&1
}

# Hand an answered run's reason and its thread answers to whoever reports
# it. The reason goes into the .answered marker. A station brief (an `Issue:
# OWNER/NAME#N` line) is reported by the station, which records the reason as
# an event in the ticket's status comment and settles the ticket
# (docs/BRAIN.md, Claim and outcome): a new comment here emailed the ticket's
# author for a routine outcome (a watched repo's ticket, 2026-10-21). Any other
# brief has no reporter, so the runner posts the reason on its ticket and
# labels it loom:answered itself. Says what failed.
post_no_change() {
  if ! printf '%s\n' "$NO_CHANGE_TEXT" > "$LOGDIR/$TICKET.answered.tmp"; then
    echo "answer could not be written"; return 1
  fi
  if ! [[ "$ISSUE_REF" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+$ ]]; then
    if ! timeout "$NET_TIMEOUT" gh issue comment "$ISSUE_NUMBER" -R "$REPO" --body "$NO_CHANGE_TEXT" >> "$LOG" 2>&1; then
      echo "answer comment failed"; return 1
    fi
    if ! mark_answered; then
      echo "answer posted but ticket label update failed"; return 1
    fi
  fi
  if [ -d "$WORKDIR/$TICKET.answers" ]; then
    rm -rf "$INBOX/answers/$TICKET"
    mkdir -p "$INBOX/answers"
    mv "$WORKDIR/$TICKET.answers" "$INBOX/answers/$TICKET"
  fi
}

# A run that did not end in a PR: say why, park the brief, never retry it.
# The reason starts with the failure's class ($3, default work): the station
# re-briefs limit and transient failures to another lane as a new attempt,
# and leaves work failures to the brain.
# A lane whose runs fail [transient] TRANSIENT_STRIKES times in a row rests
# TRANSIENT_REST seconds, unavailable "at a limit", so the station stops
# sending it tickets and the factory moves its briefed ones aside. A free
# model answering "Unexpected server error" to every run failed 14 of zen's
# 40 runs on 2026-10-19 while the lane kept claiming. A run that ends any
# other way clears the count.
TRANSIENT_STRIKES=3
TRANSIENT_REST=1800
TRANSIENT_FILE="$LOOM_HOME/state/usage/$SLOT.transient"
transient_rest_until() {
  local count until
  read -r count until 2>/dev/null < "$TRANSIENT_FILE" || return 1
  [[ "$until" =~ ^[0-9]+$ ]] && [ "$until" -gt "$(date +%s)" ] && printf '%s' "$until"
}
# Count one transient run; prints the epoch the lane rests until once the
# count reaches TRANSIENT_STRIKES, and fails before that.
transient_strike() {
  local count until now
  now="$(date +%s)"
  read -r count until 2>/dev/null < "$TRANSIENT_FILE" || count=0
  [[ "$count" =~ ^[0-9]+$ ]] || count=0
  count=$((count + 1))
  if [ "$count" -ge "$TRANSIENT_STRIKES" ]; then
    printf '0 %s\n' "$((now + TRANSIENT_REST))" > "$TRANSIENT_FILE"
    printf '%s' "$((now + TRANSIENT_REST))"
    return 0
  fi
  printf '%s 0\n' "$count" > "$TRANSIENT_FILE"
  return 1
}
transient_clear() { rm -f "$TRANSIENT_FILE"; }

# A lane whose CLI sign-in lapsed rests SIGNIN_WAIT seconds, unavailable "at a
# limit", and then tries one run again: only an interactive sign-in fixes it,
# and the runner cannot see one happen. agy, 2026-10-24: left available, the
# lane claimed six tickets in two hours, each waiting out the sign-in timeout.
SIGNIN_WAIT=7200
SIGNIN_FILE="$LOOM_HOME/state/usage/$SLOT.signin"
signin_rest_until() {
  local until
  read -r until 2>/dev/null < "$SIGNIN_FILE" || return 1
  [[ "$until" =~ ^[0-9]+$ ]] && [ "$until" -gt "$(date +%s)" ] && printf '%s' "$until"
}

# Mint this run's App token into GH_TOKEN. A personal token in the
# environment is dropped first, so a failed mint leaves gh with nothing to
# post as. Installation tokens last an hour: a run mints again after its agent.
lane_app_token() {
  [ -n "${LOOM_GITHUB_APP_ID:-}" ] || return 0
  local token
  unset GH_TOKEN GITHUB_TOKEN
  if [ -n "${LOOM_APP_TOKEN_CMD:-}" ]; then
    token="$("$LOOM_APP_TOKEN_CMD" 2>> "$LOG")" || return 1
  else
    token="$(cd "$LOOM_ROOT" && timeout "$NET_TIMEOUT" python3 -B -m bus app token 2>> "$LOG")" || return 1
  fi
  # Installation tokens are ghs_ and a JWT-like body: letters, digits, _ . -
  [[ "$token" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  export GH_TOKEN="$token"
}

# The agent's own gh config directory (loom#665): a fresh one per run, holding
# this run's App token in the hosts.yml gh reads, so the agent's gh calls bill
# the App and not the operator. The token never enters the agent's
# environment and never passes through a gh command whose error text could
# echo it into the log; printf is a builtin, so it is in no process's argv.
# Sets AGENT_GH_CONFIG, empty on a lane without App settings.
agent_gh_config() {
  AGENT_GH_CONFIG=""
  [ -n "${LOOM_GITHUB_APP_ID:-}" ] || return 0
  [[ "${GH_TOKEN:-}" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  # One runner per lane holds its lock, so this lane's stores are its own:
  # drop one a killed run left behind before making the next.
  rm -rf "$LOOM_HOME/run/$SLOT-gh."* || return 1
  local dir
  dir="$(mktemp -d "$LOOM_HOME/run/$SLOT-gh.XXXXXX")" || return 1
  if ! (umask 077 && printf 'github.com:\n    oauth_token: %s\n    git_protocol: https\n' "$GH_TOKEN" > "$dir/hosts.yml"); then
    rm -rf "$dir"
    return 1
  fi
  AGENT_GH_CONFIG="$dir"
}
AGENT_GH_WHY="could not write the agent's gh credential store; the agent did not run"

# How many gh commands the agent's output shows (loom#665), counted in the
# bytes this run added to the log; "unknown" when that slice cannot be read.
agent_gh_calls() {
  local out n
  if ! out="$(tail -c +"$((${LOG_START:-0} + 1))" "$LOG")"; then
    printf unknown
    return 0
  fi
  n="$(printf '%s\n' "$out" | grep -cE '(^|[[:space:]])gh [a-z]')"
  # grep -c exits 1 on no match (and prints 0); only 2 is an error.
  case $? in 0|1) printf '%s' "$n" ;; *) printf unknown ;; esac
}
APP_TOKEN_WHY="could not mint the lane's GitHub App token (LOOM_GITHUB_APP_*); nothing was posted"

park() {
  local reason="$2"
  [ -z "${REPLIES_FILE:-}" ] || rm -f "$REPLIES_FILE"
  if [ -n "${DROPPED_SCRATCH_FILES:-}" ]; then
    reason="$reason; dropped scratch files: ${DROPPED_SCRATCH_FILES//$'\n'/, }"
  fi
  echo "[$(date -u +%FT%TZ)] $TICKET failed: [${3:-work}] $reason" | tee -a "$LOG" >&2
  [ -z "${AGENT_GH_CALLS:-}" ] || printf '[%s] %s agent gh calls: %s\n' "$(date -u +%FT%TZ)" "$TICKET" "$AGENT_GH_CALLS" >> "$LOG"
  mv "$1" "$INBOX/failed/"
}

# The agent sandbox (sandbox_profile, api_agent), the opencode run
# (opencode_config, opencode_run) and ssm_value, shared with
# lanes/rewrite_agent.sh.
# shellcheck source=lanes/agent_sandbox.sh
. "$LOOM_ROOT/lanes/agent_sandbox.sh"

# The agy lane's agent runs as the lane user, so that user's home is its real
# home, not a scratch one. It may write only its tree, its TMPDIR, agy's state
# directory, and the keychain and caches agy needs; agy's settings, binaries
# and built-in skills stay read-only, and so does everything else there (shell
# startup files, ~/.gemini config, MCP servers). Other lanes get no rules here.
# antigravity-oauth-token is agy's sign-in when it cannot reach the keychain
# (the sandbox denies /usr/bin/security): wiped, every run asked for a browser
# login (2026-10-22 to 10-24, #1620). The sandbox profile lets only agy's own
# binary read it (agy_token_rules in lanes/agent_sandbox.sh, #1623).
AGY_STATE_KEEP=(settings.json bin builtin installation_id jetski_state.pbtxt updater last_check.timestamp log cli.log crashes cache antigravity-oauth-token)
AGY_CACHE_KEEP=(onboarding.json default_project_id.txt)
# agy keeps its sign-in in its file store, Library/Application Support/
# antigravity: inside the sandbox it cannot run /usr/bin/security to reach the
# keychain (#1465), so it falls back there. Without a write there it could
# neither keep nor refresh a token, and every run asked for a browser login
# (2026-10-22 to 10-24, #1620). The Keychains folder takes no write.
# agy writes its shell helper, bin/agentapi, before each shell command:
# denied with the rest of bin, every agy run from 2026-10-24 could run no
# command and commit nothing. agy_forget makes that file new and empty before
# each run, and the run may rewrite its contents and mode but not create,
# remove or link it: a run free to create the name could make it a hard link
# to another binary in bin, or a directory that stops every later run.
agy_write_rules() {
  [ "$LANE" = agy ] || return 0
  local h s
  h="$(cd "$AGY_HOME" && pwd -P)" || return 1
  s="$h/.gemini/antigravity-cli"
  printf '(deny file-write* (subpath "%s"))\n(allow file-write* %s (subpath "%s") (subpath "%s/Library/Application Support/antigravity") (subpath "%s/Library/Caches"))\n(deny file-write* (literal "%s/settings.json") (literal "%s/installation_id") (subpath "%s/bin") (subpath "%s/builtin"))\n(allow file-write-data file-write-mode (literal "%s/bin/agentapi"))\n' \
    "$h" "$1" "$s" "$h" "$h" "$s" "$s" "$s" "$s" "$s"
}

# agy remembers past conversations (brain, summaries, annotations) and reads
# them back. Before each run, delete everything in its state directory except
# the sign-in and install state, so nothing one ticket's agent left behind
# reaches the next.
agy_forget() {
  local s="$AGY_HOME/.gemini/antigravity-cli"
  # shellcheck disable=SC2016 # expanded by the lane user's shell
  "${AGY_RUNAS[@]}" /bin/bash -c '
    s="$1"; shift; keep=" "; cache_keep=" "
    while [ "$1" != -- ]; do keep="$keep$1 "; shift; done; shift
    for k in "$@"; do cache_keep="$cache_keep$k "; done
    [ -d "$s" ] || exit 0
    if [ -d "$s/bin" ] && [ ! -L "$s/bin" ]; then
      rm -rf "$s/bin/agentapi" && (umask 022; : > "$s/bin/agentapi") && chmod 755 "$s/bin/agentapi" || exit 1
    fi
    # Kept entries are real files and directories. A link an agent left in
    # their place is removed, never followed: the forget would otherwise
    # delete through it, into the keychain folder or another ticket'"'"'s tree.
    for e in "$s"/* "$s"/.[!.]*; do
      [ -e "$e" ] || [ -L "$e" ] || continue
      [ -L "$e" ] && { rm -f "$e" || exit 1; continue; }
      case "$keep" in *" ${e##*/} "*) continue ;; esac
      rm -rf "$e" || exit 1
    done
    [ -d "$s/cache" ] || exit 0
    for e in "$s"/cache/* "$s"/cache/.[!.]*; do
      [ -e "$e" ] || [ -L "$e" ] || continue
      [ -L "$e" ] && { rm -f "$e" || exit 1; continue; }
      case "$cache_keep" in *" ${e##*/} "*) continue ;; esac
      rm -rf "$e" || exit 1
    done' agy-forget "$s" "${AGY_STATE_KEEP[@]}" -- "${AGY_CACHE_KEEP[@]}"
}

# agy's CLI settings (settings.json in its state directory, which the sandbox
# keeps read-only) hold its permission rules. Print mode cannot prompt, so a
# tool no rule allows is denied and the run ends with no output. Before every
# run the runner rewrites the file as the lane user from lanes/agy/settings.json:
# shell commands and the file tools allowed, web fetching denied, and agy's own
# terminal sandbox off (it denied commands those rules allow; the sandbox-exec
# profile below stays the boundary). Only
# trustedWorkspaces, the operator's one-time sign-in choice, is carried over
# from the old file; any other key is dropped. After the run it writes back
# exactly what it wrote before, so nothing a run did to the file survives.
# The text travels on stdin (the lane user cannot read this checkout), and a
# link left in place of the scratch file is removed, never written through.
# Usage: agy_settings merge|keep < text; prints what it wrote.
AGY_SETTINGS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/agy/settings.json"
agy_settings() {
  # shellcheck disable=SC2016 # expanded by the lane user's shell
  "${AGY_RUNAS[@]}" /bin/bash -c '
    s="$1"; f="$s/settings.json"; t="$s/.settings.json.new"
    mkdir -p "$s" && rm -f "$t" || exit 1
    (set -C; cat > "$t") || exit 1
    if [ "$2" = merge ] && [ -f "$f" ] && [ ! -L "$f" ] &&
      tw="$(plutil -extract trustedWorkspaces json -o - "$f" 2> /dev/null)"; then
      plutil -replace trustedWorkspaces -json "$tw" "$t" || exit 1
    fi
    mv -f "$t" "$f" && cat "$f"' agy-settings "$AGY_HOME/.gemini/antigravity-cli" "$1"
}

# agy stores its token so that /usr/bin/security may read it without a
# prompt, and agy itself reads it that way, so the sandbox cannot forbid that
# tool, and a shell command could read the token. The settings deny commands
# that start with security, a speed bump rather than a wall. The lane never
# passes --dangerously-skip-permissions. Slash commands and skill expansion in
# the prompt stay off; the agent can still load a skill it decides to use.
AGY_TOOLS_NOTE="Tools on this lane: run the repo's tests and lint with your shell tool, as the brief asks, and edit files only inside this tree. Web fetching is not available here."

# The agy lane: unlock the lane user's keychain (password on stdin to a shell
# running as that user, never in this process's argv), then run agy as that
# user in an emptied environment inside the sandbox. The runner,
# as this user, commits, pushes and opens the PR from the shared tree.
agy_agent() {
  local profile settings
  AGENT_HOME="$AGY_HOME/tmp"
  "${AGY_RUNAS[@]}" mkdir -p "$AGENT_HOME" || return 3
  agy_forget || { echo "could not clear agy's memory of earlier runs" >&2; return 3; }
  agy_skills
  [ -f "$AGY_SETTINGS" ] || { echo "missing $AGY_SETTINGS" >&2; return 3; }
  # Build the sandbox first: a run that stops here never wrote the settings,
  # and once they are written nothing may fail before agy runs and the
  # restore below follows.
  profile="$(sandbox_profile "$PWD" "$AGY_BIN")" || return 3
  settings="$(agy_settings merge < "$AGY_SETTINGS")" || { echo "could not write agy's settings" >&2; return 3; }
  # shellcheck disable=SC2031 # PATH and LANG here are this shell's, not api_agent's subshell's
  "${AGY_RUNAS[@]}" /usr/bin/env -i PATH="$PATH" HOME="$AGY_HOME" TMPDIR="$AGENT_HOME" TERM=dumb \
    LANG="${LANG:-en_US.UTF-8}" timeout "$AGENT_TIMEOUT" sandbox-exec -p "$profile" \
    "$AGY_BIN" -p "$1

$AGY_TOOLS_NOTE" --mode accept-edits --disable-slash-commands --model "$MODEL" < /dev/null 2>&1 | timeout "$((AGENT_TIMEOUT + 60))" cat
  local rc=$?
  # A failed restore must fail the run, even if agy itself succeeded. Only the
  # copy of the file that agy_settings prints goes to /dev/null, not its status.
  printf '%s\n' "$settings" | agy_settings keep > /dev/null || { echo "could not restore agy's settings" >&2; return 3; }
  return "$rc"
}

# The spark lane: a vLLM coder on a machine of the operator's own, serving
# one stream at a time behind a thermal guard that refuses new connections
# (or answers 503) while the box is hot. That is a limit, not a failure: the
# lane says unavailable for LIMIT_HOLD seconds, and every brief it holds is
# parked with a "limit:" reason, which the station hands to the next lane.
LIMIT_HOLD=900
LIMIT_FILE="$LOOM_HOME/state/usage/$SLOT.limit"
# A spark slot (spark-2) may name its own box: LOOM_SPARK_2_URL, else LOOM_SPARK_URL.
SPARK_URL="$(lane_setting URL)"
SPARK_PROBE_TIMEOUT=15
LIMITED=""
LIMIT_WHY=""
# Whether the provider answered the last probe at all (provider_at_limit).
PROBE_ANSWERED=0
# An expired key holds a lane with no retry time, so its reason is the whole
# of it: "API key expired: renew <the SSM parameter, or the lane sign-in>".
AUTH_HELD=""
AUTH_WHY=""
AUTH_WHY_PREFIX="API key expired: renew"

# Ask the endpoint for its models. 0: it answered; 1: refused or 503, with
# LIMIT_WHY set; 2: no answer for another reason, with READY_REASON set.
spark_probe() {
  local code status
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$SPARK_PROBE_TIMEOUT" "${SPARK_URL%/}/models" 2>/dev/null)"
  status=$?
  if [ "$status" -eq 7 ]; then LIMIT_WHY="the $LANE endpoint refused the connection"; return 1; fi
  if [ "$code" = 503 ]; then LIMIT_WHY="the $LANE endpoint answered 503"; return 1; fi
  [ "$status" -eq 0 ] && return 0
  READY_REASON="the $LANE endpoint did not answer (curl exit $status)"
  return 2
}

# Record a limit for LIMIT_HOLD seconds and say so in the heartbeat.
mark_limit() {
  LIMIT_UNTIL=$(( $(date +%s) + LIMIT_HOLD ))
  mkdir -p "$LOOM_HOME/state/usage"
  printf '%s %s\n' "$LIMIT_UNTIL" "$LIMIT_WHY" > "$LIMIT_FILE"
  LIMITED=1
  READY_REASON="limit: $LIMIT_WHY; unavailable until $(utc "$LIMIT_UNTIL")"
  heartbeat false "$READY_REASON" "$LIMIT_UNTIL"
}

# A limit recorded by an earlier pass that has not run out. A time further
# ahead than LIMIT_HOLD (a clock that was wrong) is ignored.
limit_held() {
  local ends why now
  [ -f "$LIMIT_FILE" ] || return 1
  read -r ends why < "$LIMIT_FILE" || return 1
  now="$(date +%s)"
  [[ "$ends" =~ ^[0-9]+$ ]] && [ "$ends" -gt "$now" ] && [ "$ends" -le $(( now + LIMIT_HOLD )) ] || return 1
  LIMIT_UNTIL="$ends" LIMIT_WHY="$why" LIMITED=1
  READY_REASON="limit: $LIMIT_WHY; unavailable until $(utc "$LIMIT_UNTIL")"
}

spark_ready() {
  [[ "$SPARK_URL" =~ ^https?://[A-Za-z0-9.:_-]+(/[A-Za-z0-9._~/-]*)?$ ]] ||
    { READY_REASON="LOOM_SPARK_URL is not set to an http(s) base URL"; return 1; }
  limit_held && return 1
  spark_probe
  case $? in
    0) return 0 ;;
    1) mark_limit; return 1 ;;
    *) return 1 ;;
  esac
}
# A provider whose CLI retries a usage limit silently instead of failing
# (opencode run against OpenCode Go, 2026-10-14: every run ended exit 124)
# is asked directly. LOOM_<SLOT>_PROBE_URL names the provider's
# OpenAI-compatible chat endpoint and LOOM_<SLOT>_PROBE_HEADER an optional
# session header to send (OpenCode Go: x-opencode-session); a lane without
# the URL is never probed. One request, max_tokens 1, for the run's model.
# The key goes to curl as a config file on stdin, written by the printf
# builtin, so it reaches no process's argv and no log.
PROBE_TIMEOUT=20
# How long a lane waits before it asks the provider again whether the limit
# has lifted. The provider's allowance refills while the lane sleeps (OpenCode
# Go refills 20% of a model's allowance every 5 hours), so a lane that waited
# out the whole window sat idle for days while the provider served it again
# (opencode-go, 2026-10-20: the lane-held invariant fired and the tier's
# tickets waited behind a lane the provider had long since freed). Other
# providers retain their existing recheck interval.
LIMIT_RECHECK=1800
[ "$LANE" = opencode-go ] && LIMIT_RECHECK=300

# The provider's own word for a usage limit. It is in the body the provider
# answers with and in the reason the heartbeat carries, so a lane that asks
# the provider again can tell a hold the probe took from a hold the agent CLI
# recorded (a model it no longer offers, an expired key, a spent allowance).
GO_LIMIT_ERROR="GoUsageLimitError"

# 0 with LIMIT_WHY set when the provider answers 429 GoUsageLimitError for
# MODEL; 1 for any other answer, no answer, or no probe configured.
# PROBE_ANSWERED says the provider answered at all, so a lane that asks it
# again can tell a provider that serves it from one it cannot reach.
provider_at_limit() {
  local url header session model body out code limit_name headers retry_after stamp
  PROBE_ANSWERED=0
  url="$(lane_setting PROBE_URL)" header="$(lane_setting PROBE_HEADER)"
  [ -n "$url" ] && [ -n "$API_KEY" ] || return 1
  [[ "$url" =~ ^https?://[A-Za-z0-9.:_-]+(/[A-Za-z0-9._~/-]*)?$ ]] || return 1
  [ -z "$header" ] || [[ "$header" =~ ^[A-Za-z0-9-]+$ ]] || return 1
  # A quote, backslash or newline would end the key's line in curl's config.
  case "$API_KEY" in *'"'*|*\\*|*$'\n'*) return 1 ;; esac
  session="loom-probe-$SLOT-$(date +%s)-$$"
  # The model goes into the JSON body as written: checked here, not only by
  # the brief loop's allowlist, so no quote or backslash can reach the body.
  model="${MODEL#*/}"
  [[ "$model" =~ ^[A-Za-z0-9._:/@-]+$ ]] || return 1
  body="$(printf '{"model": "%s", "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}' "$model")"
  headers="$LOOM_HOME/state/usage/$SLOT.probe-headers"
  mkdir -p "$LOOM_HOME/state/usage"
  if ! out="$(printf 'header = "Authorization: Bearer %s"\n' "$API_KEY" |
    curl -s -K - -D "$headers" -X POST --max-time "$PROBE_TIMEOUT" -w '\n%{http_code}' \
      -H 'Content-Type: application/json' ${header:+-H "$header: $session"} \
      --data-binary "$body" "$url" 2>/dev/null)"; then
    rm -f "$headers"
    provider_checked
    return 1
  fi
  PROBE_ANSWERED=1
  provider_checked
  code="${out##*$'\n'}" body="${out%$'\n'*}"
  if [ "$code" != 429 ] || [[ "$body" != *"$GO_LIMIT_ERROR"* ]]; then
    rm -f "$headers"
    return 1
  fi
  limit_name="$(printf '%s' "$body" | sed -nE 's/.*"limitName" *: *"([A-Za-z0-9_-]+)".*/\1/p' | head -1)"
  # The provider's Retry-After header, in any case: the awk on the lane
  # machine has no IGNORECASE, so a case-sensitive match never read it and
  # every limit fell back to the window name (2026-10-20).
  retry_after="$(awk 'tolower($0) ~ /^retry-after:/ {sub(/^[^:]*:[ \t]*/, ""); sub(/\r$/, ""); value=$0} END{print value}' "$headers" 2>/dev/null)"
  rm -f "$headers"
  retry_after="$(printf '%s' "$retry_after" | tr -cd '[:alnum:], :-')"
  stamp="$(date +%s)"
  [[ "$retry_after" =~ ^[0-9]+$ ]] && retry_after=$((stamp + retry_after))
  printf '{"at": %s, "limit_name": "%s", "retry_after": "%s", "model": "%s"}\n' \
    "$stamp" "${limit_name:-unknown}" "${retry_after//\"/}" "$model" \
    > "$LOOM_HOME/state/usage/$SLOT.provider-limit.json.tmp" &&
    mv -f "$LOOM_HOME/state/usage/$SLOT.provider-limit.json.tmp" "$LOOM_HOME/state/usage/$SLOT.provider-limit.json"
  LIMIT_WHY="the provider's ${limit_name:-usage} limit (429 $GO_LIMIT_ERROR)"
  return 0
}

# When the provider was last asked whether it is at a limit: the epoch, one
# line. A lane held by a limit asks it again every LIMIT_RECHECK seconds
# (provider_limit_lifted), so the hold ends when the provider serves it.
provider_checked() {
  mkdir -p "$LOOM_HOME/state/usage"
  printf '%s\n' "$(date +%s)" > "$LOOM_HOME/state/usage/$SLOT.provider-check"
}

# Older shared holds recorded the provider answer only on the slot that saw
# it. Use that answer when another slot in the group wakes first.
provider_limit_source() {
  local slot
  if [ -f "$LOOM_HOME/state/usage/$SLOT.provider-limit.json" ]; then
    printf '%s' "$SLOT"
    return 0
  fi
  for slot in $(limit_group); do
    if [ -f "$LOOM_HOME/state/usage/$slot.provider-limit.json" ]; then
      printf '%s' "$slot"
      return 0
    fi
  done
  return 1
}

# Whether the lane asks the provider again: the last probe was at least
# LIMIT_RECHECK seconds ago, and the hold in force is the one the probe took
# (its reason names the provider's limit; no other hold carries that).
provider_recheck_due() {
  local at reason source
  source="$(provider_limit_source)" || return 1
  reason="$(sed -nE 's/.*"reason": *"([^"]*)".*/\1/p' "$BEAT" 2>/dev/null | head -1)"
  case "$reason" in *"$GO_LIMIT_ERROR"*) ;; *) return 1 ;; esac
  at="$(cat "$LOOM_HOME/state/usage/$source.provider-check" 2>/dev/null)"
  [[ "$at" =~ ^[0-9]+$ ]] || return 1
  [ "$(( $(date +%s) - at ))" -ge "$LIMIT_RECHECK" ]
}

# Whether the provider serves this lane again. The provider is asked at most
# once every LIMIT_RECHECK seconds, and only for a hold the probe itself took:
# a hold the agent CLI recorded (a model it no longer offers, a spent
# allowance) stands, for no probe says otherwise. A provider that cannot be
# reached is not a provider that serves the lane: the hold stands.
provider_limit_lifted() {
  local model key_ssm source
  provider_recheck_due || return 1
  source="$(provider_limit_source)" || return 1
  [ -n "$(lane_setting PROBE_URL)" ] || return 1
  key_ssm="$(lane_setting KEY_SSM)"
  [ -n "$key_ssm" ] || return 1
  API_KEY="$(ssm_value "$key_ssm")"
  [ -n "$API_KEY" ] || return 1
  # The model the probe found at a limit: the provider's answer is about that
  # model, so that is the model to ask about again, unless the slot now runs
  # another (a limit Kimi hit says nothing of the slot's Beam Free).
  model="$(sed -nE 's/.*"model": *"([A-Za-z0-9._:/@-]+)".*/\1/p' \
    "$LOOM_HOME/state/usage/$source.provider-limit.json" 2>/dev/null | head -1)"
  if [ -z "$model" ] || { [ -n "$(lane_setting MODEL)" ] && [ "${model#*/}" != "$(lane_setting MODEL | sed 's#^[^/]*/##')" ]; }; then
    model="$(lane_setting MODEL)"
  fi
  [ -n "$model" ] || return 1
  MODEL="$model"
  if provider_at_limit; then
    hold_provider_limit
    return 1
  fi
  [ "${PROBE_ANSWERED:-0}" = 1 ] || return 1
  lift_provider_limit
  return 0
}

# The provider serves the group again: the hold is over for every slot that
# shares the account. The record of the refusal stays: it is the last 429 the
# Usage view shows.
lift_provider_limit() {
  local slot
  for slot in $(limit_group); do
    write_beat "$slot" true ""
  done
}

# A free lane whose provider has a read-only meter (openrouter: the key's
# free-model requests today; workersai: today's Neurons) records it for the
# Usage view through bus/meter.py, at most every five minutes. The key and the
# account id go on stdin, so they reach no argv and no log. LOOM_<SLOT>_METER_URL
# overrides the provider's endpoint. A failed read is recorded, never fatal.
lane_meter() {
  local url
  case "$LANE" in openrouter|workersai) ;; *) return 0 ;; esac
  [ -n "$API_KEY" ] || return 0
  url="$(lane_setting METER_URL)"
  [ -z "$url" ] || [[ "$url" =~ ^https?://[A-Za-z0-9.:_-]+(/[A-Za-z0-9._~/-]*)?$ ]] || return 0
  printf '%s\n%s\n' "$API_KEY" "$LANE_ACCOUNT" |
    PYTHONPATH="$LOOM_ROOT" timeout "$PROBE_TIMEOUT" python3 -B -m bus.meter "$LANE" \
      "$LOOM_HOME/state/usage/$SLOT.meter.json" "$url" > /dev/null 2>&1 || true
}

# The slots that bill the same provider account as this slot, and so the same
# key: a limit or an expired key recorded by one holds every slot in the group.
# opencode-go's four slots share one OpenCode Go account; every other lane is a
# group of one (the numbered slots map to their provider lane above).
key_group() {
  local lane="${LANE:-}"
  case "$lane" in
    opencode-go)
      printf '%s\n' opencode-go opencode-go-2 opencode-go-3 opencode-go-4 ;;
    *)
      [ -n "$lane" ] && printf '%s\n' "$lane" ;;
  esac
}

# The slots a usage limit holds together: the key group, less the slots on a
# free model. OpenCode Go's free models (opencode-go/*-free) sit outside the
# plan's allowance: on 2026-10-23 Kimi spent the monthly limit and the 429
# held the Beam Free slots for five days while Beam Free still answered. A
# free slot is a group of one. An expired key still holds the whole key group.
slot_model() {
  local s v
  s="$(setting_var "$1" MODEL)" v="$(lane_var MODEL)"
  printf '%s' "${!s:-${!v:-}}"
}
free_model() { [[ "$1" == opencode-go/*-free ]]; }
limit_group() {
  local slot
  if [ "${LANE:-}" = opencode-go ] && free_model "$(slot_model "$SLOT")"; then
    printf '%s\n' "$SLOT"
    return 0
  fi
  for slot in $(key_group); do
    free_model "$(slot_model "$slot")" || printf '%s\n' "$slot"
  done
}

# The epoch this slot's provider limit lifts: the reset the provider gave, else
# the next reset of the period the error names (weekly / daily), never a short
# fixed window. Empty when there is no recorded limit to hold on.
limit_until() {
  local file ra name now
  now="$(date +%s)"
  file="$LOOM_HOME/state/usage/$SLOT.provider-limit.json"
  [ -f "$file" ] || return 1
  ra="$(sed -nE 's/.*"retry_after": *"?([0-9]+)"?.*/\1/p' "$file" | head -1)"
  if [ -n "$ra" ] && [ "$ra" -gt "$now" ]; then
    printf '%s' "$ra"; return 0
  fi
  name="$(sed -nE 's/.*"limit_name": *"?([A-Za-z]+)"?.*/\1/p' "$file" | head -1)"
  case "$name" in
    weekly) printf '%s' "$((now + 7 * 86400))"; return 0 ;;
    daily) printf '%s' "$((now + 86400))"; return 0 ;;
  esac
  # No reset the provider gave and no named period: hold a week, not the short
  # default, so a shared account stays held until it is known to have reset.
  printf '%s' "$((now + 7 * 86400))"; return 0
}

# Hold every slot in this slot's group until the provider's reset, and
# hand on every brief still in the inbox this pass.
hold_provider_limit() {
  local until slot source checked tmp
  if ! until="$(limit_until)"; then
    until=$(( $(date +%s) + ${LIMIT_WAIT:-3600} ))
  fi
  LIMITED=1
  [ -n "$LIMIT_WHY" ] || LIMIT_WHY="provider limit reached"
  source="$LOOM_HOME/state/usage/$SLOT.provider-limit.json"
  checked="$(date +%s)"
  for slot in $(limit_group); do
    if [ "$slot" != "$SLOT" ] && [ -f "$source" ]; then
      tmp="$LOOM_HOME/state/usage/$slot.provider-limit.json.tmp"
      if ! cp "$source" "$tmp" \
        || ! mv -f "$tmp" "$LOOM_HOME/state/usage/$slot.provider-limit.json"; then
        rm -f "$tmp"
        return 1
      fi
    fi
    printf '%s\n' "$checked" > "$LOOM_HOME/state/usage/$slot.provider-check" || return 1
    write_beat "$slot" false "limit: $LIMIT_WHY; unavailable until $(utc "$until")" "$until"
  done
}

# The digest of a key, never the key: the hold an expired key puts on a lane
# lifts when the value at its SSM parameter changes, and a digest changes with
# the value. The key goes to the hasher on stdin, so it reaches no argv.
key_digest() {
  if command -v shasum > /dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 | cut -c1-16
  else
    printf '%s' "$1" | sha256sum | cut -c1-16
  fi
}

# Why a lane is unavailable for a key the provider refused: the SSM parameter
# its value came from, or the lane's own sign-in for a lane that reads no key.
key_renew_reason() {
  local param
  param="$(lane_setting KEY_SSM)"
  printf '%s %s' "$AUTH_WHY_PREFIX" "${param:-the $LANE lane sign-in}"
}

# Whether this lane reads its key from an SSM parameter (a name under a path,
# as lanes/mac/install.sh writes them) whose value the operator can change.
# Only such a lane can be held for an expired key: codex, claude and zen keep
# their sign-in in the CLI's own file, where there is no value to watch and
# therefore no hold to wait on.
key_renewable() {
  case "$(lane_setting KEY_SSM)" in /*) return 0 ;; esac
  return 1
}

# The file slot $1's expired-key hold records the digest of the key it refused
# in. The digest, never the key: it is all the hold needs to notice a renewal.
key_sha_file() {
  printf '%s' "$LOOM_HOME/state/usage/$1.key-sha"
}

# Every slot that bills the key the provider refused is unavailable, with no
# retry time: only a renewed key lifts that hold. Each held slot records the
# digest of the key that failed.
hold_expired_key() {  # reason
  local reason="$1" digest slot
  digest="$(key_digest "$API_KEY")"
  mkdir -p "$LOOM_HOME/state/usage"
  for slot in $(key_group); do
    printf '%s\n' "$digest" > "$(key_sha_file "$slot")"
    write_beat "$slot" false "$reason"
  done
}

# Whether an expired key still holds this slot: 0 while it stands, 1 when there
# is no hold. It lifts only when the value at the parameter its heartbeat names
# is not the one the hold was recorded for: only the operator can renew a key.
# Sets AUTH_HELD, AUTH_WHY and READY_REASON while it stands.
key_held() {
  local reason param want key
  reason="$(sed -nE 's/.*"reason": "([^"]*)".*/\1/p' "$BEAT" 2>/dev/null | head -1)"
  case "$reason" in "$AUTH_WHY_PREFIX "/*) ;; *) return 1 ;; esac
  AUTH_HELD=1 AUTH_WHY="$reason" READY_REASON="$reason"
  param="${reason#"$AUTH_WHY_PREFIX "}"
  want="$(cat "$(key_sha_file "$SLOT")" 2>/dev/null)"
  key="$(ssm_value "$param")"
  if [ -n "$want" ] && [ -n "$key" ] && [ "$(key_digest "$key")" != "$want" ]; then
    rm -f "$(key_sha_file "$SLOT")"
    AUTH_HELD="" AUTH_WHY="" READY_REASON=""
    return 1
  fi
  return 0
}

# The operator's ask for an expired key: $1 is the failure's reason, whose
# tail names what to renew (the SSM parameter the key came from, or the lane's
# own sign-in). Filed once per parameter -- another lane that hits the same key
# comments on the open ask instead of filing a second. The body names no agent
# output, so nothing the provider said, or a run echoed, can reach a public
# issue.
ask_renew_key() {
  local reason="$1" param title body
  param="${reason#"$AUTH_WHY_PREFIX "}"
  title="Provider key expired: renew $param"
  body="$(printf '%s\n\nLane %s could not use the provider key it reads.\n\nWhat to renew: `%s`\n\nThe lane log holds what the provider said. A lane that reads that parameter is unavailable until the value there changes.\n' \
    "$KEY_ASK_MARKER" "$SLOT" "$param")"
  file_ask "$title" "$body" "Could not use the key again"
}

# The operator's ask for a lane whose CLI sign-in lapsed: $1 is the reason.
# Filed once per lane, like ask_renew_key, and names no agent output.
# The agy lane signs in as its own macOS user, so the ask names that user and
# the command: a sign-in as the operator never reaches it (2026-10-24).
ask_sign_in() {
  local title body how
  title="Lane $LANE sign-in lapsed: sign in the $LANE lane CLI again"
  how="Sign in the $LANE lane CLI again on the lane machine (an interactive login)."
  if [ "$LANE" = agy ]; then
    # ~user, not the home path: the ask lands in a public issue.
    local bin="${AGY_BIN/#"$AGY_HOME"/~$AGY_USER}"
    how="$(printf 'The lane runs agy as the macOS user %s, with its own sign-in; a sign-in as yourself does not reach it. On the lane machine run:\n\n    sudo -u %s -H %s update\n    sudo -u %s -H %s\n\nand finish the browser sign-in it prints, then quit agy.' \
      "$AGY_USER" "$AGY_USER" "$bin" "$AGY_USER" "$bin")"
  fi
  body="$(printf '%s\n\nLane %s waited for a sign-in nobody gave and timed out: its CLI sign-in lapsed. %s\n\nUntil then each run on the lane fails as auth.\n' \
    "$KEY_ASK_MARKER" "$SLOT" "$how")"
  file_ask "$title" "$body" "Sign-in failed again"
}

# File a needs:human ask titled $1 with body $2, or, with one already open,
# keep one comment on it with a running count headed $3, never one per failure.
file_ask() {
  local title="$1" body="$2" headline="$3" number existing
  existing="$(open_issue "$title")"
  if [ -n "$existing" ]; then
    printf '' | upsert_sticky_comment "$FACTORY_REPO" "$existing" "$KEY_REPEAT_MARKER" repeat \
      --headline "$headline" --items-label "Lanes seen" --item "$SLOT" ||
      echo "could not update the open ask $FACTORY_REPO#$existing" >> "$LOG"
    return 0
  fi
  number="$(gh issue create --repo "$FACTORY_REPO" --title "$title" \
             --label needs:human --body "$body" 2>>"$LOG" ||
           gh issue create --repo "$FACTORY_REPO" --title "$title" --body "$body" 2>>"$LOG")"
  number="${number##*/}"
  case "$number" in ''|*[!0-9]*) echo "needs:human ask filing failed" >> "$LOG" ;; esac
  return 0
}

# Model ladders keep usage limits per model so a slot can continue on the
# next configured model. Each row is model, reset epoch, and safe reason.
MODEL_LIMIT_FILE="$LOOM_HOME/state/usage/$SLOT.models"
model_limit_until() {
  local model="$1" now saved_model ends
  now="$(date +%s)"
  [ -f "$MODEL_LIMIT_FILE" ] || return 1
  while read -r saved_model ends _; do
    [ "$saved_model" = "$model" ] && [[ "$ends" =~ ^[0-9]+$ ]] &&
      [ "$ends" -gt "$now" ] && { printf '%s' "$ends"; return 0; }
  done < "$MODEL_LIMIT_FILE"
  return 1
}
record_model_limit() {
  local model="$1" ends="$2" why="$3" tmp
  tmp="$MODEL_LIMIT_FILE.tmp"
  mkdir -p "$(dirname "$MODEL_LIMIT_FILE")"
  { [ ! -f "$MODEL_LIMIT_FILE" ] || awk -v m="$model" '$1 != m' "$MODEL_LIMIT_FILE"
    printf '%s %s %s\n' "$model" "$ends" "$(printf '%s' "$why" | tr -cd '[:alnum:] ._:-')"; } > "$tmp"
  mv "$tmp" "$MODEL_LIMIT_FILE"
}
# The epoch the last of the lane's configured models comes off its limit;
# fails when one of them is not limited now.
all_models_limited_until() {
  local model ends last=0
  for model in $CONFIGURED_MODELS; do
    ends="$(model_limit_until "$model")" || return 1
    [ "$ends" -gt "$last" ] && last="$ends"
  done
  [ "$last" -gt 0 ] && printf '%s' "$last"
}
ladder_reason() {
  local models="$1" model ends why="" first=1
  for model in $models; do
    if ends="$(model_limit_until "$model")"; then
      [ "$first" -eq 1 ] || why="$why, "
      why="$why$model until $(utc "$ends")"
      first=0
    fi
  done
  printf '%s' "$why"
}

# Go and Zen slots pick each run's model from the live lists, free models
# first (bus/model_pick.py), because free models rotate out without notice: a
# pinned one failed every run until someone repinned it. A model that fails is
# paused for every slot, and the brief waits for the next pass, up to
# MODEL_ROTATIONS times, so a run moves on and never retries a broken model.
# A brief's Model: line and LOOM_<SLOT>_MODEL do not bind these slots;
# LOOM_MODEL_PICK=off or a LOOM_<SLOT>_MODELS ladder goes back to the pin.
MODEL_ROTATIONS=3
PICK_TIMEOUT=300
MODEL_PICKED="" MODEL_KIND="" MODEL_NOTE="" PICK_UNTIL="" PICK_WHY="" MODEL_PAUSED=""
picks_model() {
  [ "${LOOM_MODEL_PICK:-on}" != off ] && [ -z "$(lane_setting MODELS)" ] &&
    { [ "$LANE" = opencode-go ] || [ "$LANE" = zen ]; }
}
model_pick() {
  PYTHONPATH="$LOOM_ROOT" LOOM_HOME="$LOOM_HOME" timeout "$PICK_TIMEOUT" python3 -B -m bus.model_pick "$@"
}
# The reset and reason of an answer that names no model; a pick that died
# (no answer at all) waits LIMIT_HOLD.
pick_refused() {
  local out="${1#$'\t'}"
  PICK_UNTIL="${out%%$'\t'*}" PICK_WHY="${out#*$'\t'}"
  [[ "$PICK_UNTIL" =~ ^[0-9]+$ ]] || { PICK_UNTIL=$(( $(date +%s) + LIMIT_HOLD )); PICK_WHY="the model pick gave no answer"; }
  PICK_WHY="$(printf '%s' "$PICK_WHY" | tr -cd '[:alnum:] ._:;()/-' | cut -c1-200)"
}
# 0 with MODEL set for this run; the key goes to the picker on stdin. A run
# reaches here with the slot's pin in MODEL, which it replaces.
pick_run_model() {
  local out tier=none
  # Paid models run on build work only: a verify, answer or review run stays
  # on free models. An easy ticket runs free first, and any other the strong
  # tier first. A ticket with no Difficulty: line counts as medium.
  if [ -z "$DELIVERABLE" ]; then
    tier=strong
    [ "$(field Difficulty "$BRIEF" || true)" = easy ] && tier=easy
  fi
  if ! out="$(printf '%s\n' "$API_KEY" | model_pick pick "$LANE" "$SLOT" "$REPO_PRIVATE" "$tier" "$(field Demoted-Models "$BRIEF" || true)" 2>> "$LOG")"; then
    pick_refused "$out"
    return 1
  fi
  IFS=$'\t' read -r MODEL_PICKED MODEL_KIND MODEL_NOTE <<< "$out"
  MODEL="$MODEL_PICKED"
  echo "[$(date -u +%FT%TZ)] $TICKET model pick: $MODEL ($MODEL_KIND) on lane $SLOT; fallback: $MODEL_NOTE" >> "$LOG"
}
# 0 when, from what is already known, no model can run: the station should
# send this slot nothing until PICK_UNTIL.
pick_held() {
  local out
  out="$(model_pick status "$LANE" 2>/dev/null)"
  [ $? -eq 3 ] || return 1
  pick_refused "$out"
}

# The opencode binary a slot runs: LOOM_<SLOT>_OPENCODE, else opencode from
# PATH. One slot can try a new major version (its installer would overwrite
# the binary every other opencode lane runs) without moving the rest.
opencode_bin() {
  local b
  b="$(lane_setting OPENCODE)"
  printf '%s' "${b:-opencode}"
}

# Whether the agent CLI lists a model: 0 when it does, 1 when its list for the
# model's provider lacks it. Only opencode lanes are asked (the other CLIs
# have no list command here); a list that cannot be read (command error,
# timeout, empty answer) is unknown, and unknown counts as listed, so a
# broken probe never holds a lane for good.
model_listed() {
  local model="$1" provider list
  case "$LANE" in openrouter|opencode-go|zen|zai|workersai|spark|free) ;; *) return 0 ;; esac
  provider="${model%%/*}"
  [ "$provider" != "$model" ] || return 0
  list="$(timeout "$NET_TIMEOUT" "$(opencode_bin)" models "$provider" 2>/dev/null < /dev/null)" || return 0
  [ -n "$list" ] || return 0
  printf '%s\n' "$list" | tr -d '\r' | grep -qxF -- "$model"
}

# Usage: opencode_agent <key_var> <run arguments...>
opencode_agent() {
  local key_var="$1"
  shift
  opencode_run "$key_var" "$(opencode_bin)" "$@"
}

# OpenCode JSON events delimit assistant steps; only a final step containing
# text and no tool is evidence that the model stopped on a described action.
opencode_described_step() {
  python3 -I - "$1" <<'PY'
import json
import sys

text = tool = False
events_processed = False
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if not isinstance(event, dict):
        continue
    events_processed = True
    kind = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if kind == "step_start":
        text = tool = False
    elif kind == "text" and isinstance(part.get("text"), str) and part["text"].strip():
        text = True
    elif kind in ("tool_use", "tool") or part.get("type") == "tool":
        tool = True
if not events_processed:
    sys.exit(1)
sys.exit(0 if text and not tool else 1)
PY
}

# The lane log's view of an opencode run read as JSON events. "raw" keeps
# the events (the spark lane's log has always held them). "text" keeps what a
# plain run prints: the model's text as lines, and every line that is not a
# work event (a provider error, opencode's own message) as it came, so the
# failure classes and the final message a failure reason quotes (loom#1362)
# read the same as before the lane ran as JSON.
opencode_log_view() {
  [ "$1" = raw ] && { cat; return; }
  python3 -I -u -c '
import json
import sys

WORK = ("tool_use", "tool", "reasoning", "step_start", "step_finish")
for line in sys.stdin:
    try:
        event = json.loads(line)
    except ValueError:
        event = None
    if not isinstance(event, dict):
        sys.stdout.write(line)
    elif event.get("type") == "text":
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if isinstance(part.get("text"), str):
            print(part["text"])
    elif event.get("type") not in WORK:
        sys.stdout.write(line)
'
}

# Usage: opencode_json_pass <key_var> <log view> <events file> <opencode run arguments...>
# One opencode run as JSON events: the raw events to the events file, their
# view to the lane log. Returns the agent's exit status.
opencode_json_pass() {
  local key_var="$1" view="$2" events="$3"
  shift 3
  opencode_agent "$key_var" --format json "$@" | tee "$events" | opencode_log_view "$view"
  return "${PIPESTATUS[0]}"
}

# Every opencode lane gets one continue for a run that ends on a described
# step: no tool call, no change, no reason (loom#1282). Free models answer
# "Let me read the build files." and stop. A second such end is
# marked for the runner to fail as "ended on a described step".
# Usage: opencode_agent_continuing <key_var> <log view> <brief text>
opencode_agent_continuing() {
  local key_var="$1" view="$2" brief="$3" status
  local events="$WORKDIR/$TICKET.opencode-events"
  opencode_json_pass "$key_var" "$view" "$events" "$brief"
  status=$?
  [ "$status" -eq 0 ] || return "$status"
  if opencode_has_no_work &&
     { [ ! -f "$TREE/$SUMMARY_FILE" ] ||
       ! grep -qiE "$NO_CHANGE_RE" "$TREE/$SUMMARY_FILE"; } &&
     opencode_described_step "$events"; then
    echo "[$(date -u +%FT%TZ)] $TICKET: continuing a text-only $LANE step once"
    opencode_json_pass "$key_var" "$view" "$events" --continue \
      "Continue: carry out the step you just described, then commit or write .loom/summary.md"
    status=$?
    # A failed continue is not swallowed: its exit status goes to the runner,
    # which classes it (limit, auth, transient or work) from the run's output.
    [ "$status" -eq 0 ] || return "$status"
    if opencode_has_no_work && opencode_described_step "$events"; then
      : > "$WORKDIR/$TICKET.described-step"
    fi
  fi
}

opencode_has_no_work() {
  local status
  [ "$(safe_git rev-parse HEAD)" = "$BASE" ] || return 1
  status="$(safe_git status --porcelain --untracked-files=all)" || return 1
  ! printf '%s\n' "$status" | grep -v ' .loom/summary.md$' | grep -q .
}

# git in an agent's tree, run by the runner: never run hooks or an fsmonitor
# the tree might name, whatever ended up in its config. (`timeout` cannot run
# a shell function, so the push spells the same options out.)
safe_git() {
  git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
}

# The repo's validation contract: .loom/validation.conf, the same interpreter,
# submodules, setup and check its CI uses. Lines are "key: value":
#   interpreter: python3.12     a command that must be on the lane's PATH
#   submodules: yes             check out the pinned submodules first
#   setup: <shell command>      prepares the tree (a venv, locked deps, lint)
#   check: <shell command>      the required check; exit 0 and no tracked
#                               file changed means it passed
#   affected: <shell command>   optional; runs after check passes on the
#                               committed tree, with the pull request's changed
#                               files (one path per line) on stdin and in
#                               LOOM_CHANGED_FILES; the same pass rule as
#                               check, and a failure blocks the push the same
#                               way. It runs the per-directory suites a repo's
#                               path-scoped CI jobs run (loom#1773).
# setup, check and affected run as shell commands, like the run: steps of a CI
# workflow, so they are only ever read from the branch the pull request
# merges into: the base revision of a new run, and the base branch (fetched
# for it) of a fix run, whose own branch holds unmerged agent edits. Nothing
# an agent edits changes what is checked. A repo without the file is not
# validated by the runner (its CI still is).
CONTRACT_FILE=.loom/validation.conf
# Read the key: value lines bus.contract printed into C_INTERP, C_SUBMODULES,
# C_SETUP, C_CHECK and C_AFFECTED. Returns 1 with SETUP_REASON when a line, the check,
# submodules, the interpreter or a command is unusable.
parse_contract() {
  local canon="$1" line key val
  C_INTERP="" C_SUBMODULES=no C_SETUP="" C_CHECK="" C_AFFECTED=""
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%:*}" val="${line#*:}"
    val="${val#"${val%%[![:space:]]*}"}"
    case "$key" in
      interpreter) C_INTERP="$val" ;;
      submodules) C_SUBMODULES="$val" ;;
      setup) C_SETUP="$val" ;;
      check) C_CHECK="$val" ;;
      affected) C_AFFECTED="$val" ;;
      *) SETUP_REASON="$CONTRACT_FILE has an unknown line '${line:0:40}'"; return 1 ;;
    esac
  done <<< "$canon"
  [ -n "$C_CHECK" ] || { SETUP_REASON="$CONTRACT_FILE has no check: line"; return 1; }
  case "$C_SUBMODULES" in yes|no) ;; *) SETUP_REASON="$CONTRACT_FILE submodules: must be yes or no"; return 1 ;; esac
  [ -z "$C_INTERP" ] || [[ "$C_INTERP" =~ ^[A-Za-z0-9._+-]+$ ]] ||
    { SETUP_REASON="$CONTRACT_FILE interpreter: must be a command name"; return 1; }
  for val in "$C_SETUP" "$C_CHECK"; do
    contract_command_ok "$val" ||
      { SETUP_REASON="$CONTRACT_FILE setup: and check: must be printable ASCII, at most 1000 characters"; return 1; }
  done
  contract_command_ok "$C_AFFECTED" ||
    { SETUP_REASON="$CONTRACT_FILE affected: must be printable ASCII, at most 1000 characters"; return 1; }
}

# A fix round's base branch, fetched into the shallow tree for its contract.
# Tried a second time after a pause: two fetches on one shallow tree race
# ("shallow file has changed since we read it"), and the first such failure
# filed a factory incident while nothing was wrong (2026-10-22).
fetch_base_for_contract() {
  local try
  for try in 1 2; do
    timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
      fetch -q --depth 1 origin "${BASE_BRANCH:-HEAD}" >> "$LOG" 2>&1 && return 0
    [ "$try" -eq 2 ] || sleep 3
  done
  return 1
}

load_contract() {
  local rev="$BASE" mode canon
  HAS_CONTRACT="" C_INTERP="" C_SUBMODULES=no C_SETUP="" C_CHECK="" C_AFFECTED=""
  if [ -n "$FIX_PR" ] && ! { fetch_base_for_contract &&
       rev="$(safe_git rev-parse --verify -q FETCH_HEAD)"; }; then
    HAS_CONTRACT=1 SETUP_REASON="could not fetch the base branch for $CONTRACT_FILE"
    note_factory_incident environment "could not fetch the base branch for $CONTRACT_FILE" SETUP_REASON
    return 1
  fi
  mode="$(safe_git ls-tree "$rev" -- "$CONTRACT_FILE" | cut -d' ' -f1)"
  [ -n "$mode" ] || return 0
  HAS_CONTRACT=1
  [ "$mode" = 100644 ] || [ "$mode" = 100755 ] || { SETUP_REASON="$CONTRACT_FILE is not a regular file"; return 1; }
  # bus/contract.py reads every contract form repos use and prints the
  # key: value lines below; a form it cannot read is a factory incident:
  # the runner's reader is behind the repos, not the ticket's work.
  canon="$(safe_git cat-file blob "$rev:$CONTRACT_FILE" | PYTHONPATH="$LOOM_ROOT" python3 -B -m bus.contract)" || {
    SETUP_REASON="$CONTRACT_FILE ${canon:0:120}"
    note_factory_incident contract "contract: $canon" SETUP_REASON
    return 1
  }
  parse_contract "$canon"
}

# setup and check are shell by design (like a CI run: step), so their content
# is trusted as the base branch's. Shell syntax is allowed because the base
# branch authors control these CI commands. The guard requires one line of
# printable ASCII, of bounded length, so no control byte or odd encoding
# hides part of the command from the record, the log or a reader.
# This format guard is for readable records; trust comes from loading the
# contract from the base revision, never from the agent's editable tree.
# load_contract applies it when reading the file, and contract_run again
# right before bash -c, so no caller can hand it an unchecked string.
contract_command_ok() {
  [ "${#1}" -le 1000 ] && [ "$1" = "$(printf '%s' "$1" | LC_ALL=C tr -cd ' -~')" ]
}

# Remove untracked virtual environments left by the agent before the final
# check. Their interpreter symlinks can point outside the check sandbox.
# A .venv containing any tracked path is retained intact.
remove_untracked_venvs() {
  local candidate relative tracked
  while IFS= read -r -d '' candidate; do
    relative="${candidate#"$TREE"/}"
    tracked="$(safe_git ls-files -- "$relative")" || return 1
    [ -z "$tracked" ] || continue
    rm -rf -- "$candidate" || return 1
  done < <(find "$TREE" -type d -name .venv -prune -print0)
}

validation_interpreter_denied() {
  local status="$1" output="$2"
  [ "$status" -ne 0 ] && [[ "$output" == *"Failed to query Python interpreter"* &&
    "$output" == *"Operation not permitted"* ]]
}

# One line of $RECORD per step: phase, command, exit code, the tree revision
# it ran on, and whether it passed. Only a final line with "ok": true lets the
# runner open (or push to) a pull request.
json_str() {
  # shellcheck disable=SC1003 # escaping a literal backslash
  printf '"%s"' "$(printf '%s' "$1" | tr '[:cntrl:]' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g')"
}
record() {
  printf '{"ticket": %s, "phase": "%s", "command": %s, "exit": %s, "revision": "%s", "ok": %s, "detail": %s, "at": "%s"}\n' \
    "$(json_str "$TICKET")" "$1" "$(json_str "$2")" "$3" "$4" "$5" "$(json_str "${6:-}")" \
    "$(date -u +%FT%TZ)" >> "$RECORD"
}

# What failed in a check, from the log past byte $1: the tests it named (unittest
# FAIL:/ERROR:, pytest FAILED/ERROR, go --- FAIL:) and the tools the shell could
# not find, else its last line, so a validation record says why (loom#2418).
check_failures() {  # log offset
  local out names
  out="$(tail -c +"$(( $1 + 1 ))" "$LOG" 2>/dev/null)"
  # LC_ALL=C: a check's output may be any bytes, and BSD sed and cut stop at
  # one that is not UTF-8.
  names="$(printf '%s\n' "$out" | LC_ALL=C sed -nE \
    -e 's/^(FAIL|ERROR): [^ ]+ \(([^)]+)\).*/\2/p' \
    -e 's/^(FAIL|ERROR): ([^ ]+)$/\2/p' \
    -e 's/^(FAILED|ERROR) ([^ ]+::[^ ]+).*/\2/p' \
    -e 's/^--- FAIL: ([^ ]+).*/\1/p' \
    -e 's/^.*: ([^ :]+): (command )?not found$/missing \1/p' |
    awk '!seen[$0]++' | head -n 20 | paste -sd ',' - | sed 's/,/, /g')"
  [ -n "$names" ] || names="$(printf '%s\n' "$out" | LC_ALL=C grep -av '^[[:space:]]*$' | tail -n 1)"
  printf '%s' "$names" | LC_ALL=C cut -c1-1000
}
# The non-empty arguments, joined for a record's detail.
join_detail() {
  local part out=""
  for part in "$@"; do
    [ -n "$part" ] && out="${out:+$out; }$part"
  done
  printf '%s' "$out"
}
log_size() { wc -c < "$LOG" 2>/dev/null | tr -d ' '; }

# Base check verdicts survive the per-brief scratch HOME. A green result is
# reusable for a day; a red result briefly stops more tickets for this repo.
BASE_VERDICT_DIR="$LOOM_HOME/state/base-verdicts"
BASE_VERDICT_TTL_GREEN=86400
BASE_VERDICT_TTL_RED=300
base_verdict_key() {
  printf '%s\n%s\n%s\n%s\n' "$REPO" "$1" "$C_CHECK" "$2" | sha256sum | cut -d' ' -f1
}
base_verdict_read() {  # key, now
  local file="$BASE_VERDICT_DIR/$1" at exit ttl
  [ -f "$file" ] || return 1
  IFS=' ' read -r at exit < "$file" || return 1
  case "$at:$exit" in *[!0-9:]*|:*) return 1 ;; esac
  if [ "$exit" -eq 0 ]; then ttl="$BASE_VERDICT_TTL_GREEN"; else ttl="$BASE_VERDICT_TTL_RED"; fi
  [ "$2" -ge "$at" ] && [ "$(( $2 - $at ))" -lt "$ttl" ] || return 1
  BASELINE_EXIT="$exit"
  return 0
}
base_verdict_write() {  # key, exit
  mkdir -p "$BASE_VERDICT_DIR" || return 1
  printf '%s %s\n' "$(date +%s)" "$2" > "$BASE_VERDICT_DIR/$1" || return 1
  # Every revision mints a new file; sweep the ones no read can use. A
  # failed sweep never fails the write.
  find "$BASE_VERDICT_DIR" -type f -mmin +"$(( BASE_VERDICT_TTL_GREEN / 60 ))" -delete 2>/dev/null || true
}

# A validation failure the repo did not cause is a factory incident, not a
# [work] failure: the contract cannot be read, the environment cannot run
# the check (exit 125/126/127), or the check is already red on the
# unmodified base. The ticket keeps its rung: the brain holds it and
# re-queues it when the incident closes. file_factory_incident files or
# updates one incident issue on the factory repo and prints "factory
# incident <owner>/<repo>#<number>". The record names the repo, the exact
# failing line or command, and the exit code.
FACTORY_REPO=quadseven/loom
INCIDENT_MARKER="<!-- loom:factory-incident v1 -->"
# The marker on the body of the needs:human ask an expired key files.
KEY_ASK_MARKER="<!-- loom:key-ask v1 -->"
# The markers on the one comment that counts repeats on an open ask or incident.
KEY_REPEAT_MARKER="<!-- loom:key-repeat v1 -->"
INCIDENT_REPEAT_MARKER="<!-- loom:incident-repeat v1 -->"
# The number of the open issue on the factory repo whose title is exactly $1,
# empty when there is none: one open issue per incident kind, per expired key.
open_issue() {
  TITLE="$1" gh search issues --repo "$FACTORY_REPO" --state open \
    --json number,title --jq '.[] | select(.title == env.TITLE) | .number' \
    -- "$1" 2>>"$LOG" | head -1
}
# Each open incident this machine filed or updated, one file per repo and
# kind holding its number, so a run whose check passes can close it without
# a search. Nothing closed incidents: 16 tickets sat held behind five whose
# checks had passed again for hours (2026-10-19).
INCIDENT_DIR="$LOOM_HOME/state/incidents"
incident_file() { printf '%s/%s-%s' "$INCIDENT_DIR" "$(printf '%s' "$REPO" | tr '/' '_')" "$1"; }
# Close this repo's environment, contract and base-check incidents once the
# check has passed on the unmodified base; the station then releases tickets.
resolve_factory_incidents() {
  local kind file number
  for kind in environment contract base-check; do
    file="$(incident_file "$kind")"
    [ -f "$file" ] || continue
    number="$(tr -cd '0-9' < "$file")"
    if [ -n "$number" ]; then
      # Shadow mode (bus/shadow.py): the guard reads the issue and lets a
      # factory incident's close through; anything else it records and holds.
      PYTHONPATH="$LOOM_ROOT" python3 -B -m bus.shadow gh-guard --lane "$PWD" \
        issue close --repo "$FACTORY_REPO" "$number" >>"$LOG" 2>&1 || continue
      gh issue close --repo "$FACTORY_REPO" "$number" \
        --comment "Cleared: the check passed on the unmodified base in $(incident_ticket)." >>"$LOG" 2>&1 || continue
    fi
    rm -f "$file"
  done
}
# The factory repo is public, and an incident for a private repo once put
# that repo's name, ticket and check command in it (loom#1040, loom#1273).
# Only a repo known to be public (REPO_PRIVATE=false) is named; any other is
# "private repo <alias>", where the alias is a random id this machine keeps
# per repo, so its incidents still dedupe by title and close on their own.
# Its ticket and failing line stay in the lane log and on the ticket.
INCIDENT_WITHHELD="withheld: the lane log and the ticket have the detail"
repo_is_public() { [ "${REPO_PRIVATE:-}" = false ]; }
incident_ticket() {
  if repo_is_public; then printf '%s' "$TICKET"; else printf 'a private repo ticket'; fi
}
incident_subject() {
  local file alias
  if repo_is_public; then printf '%s' "$REPO"; return 0; fi
  file="$INCIDENT_DIR/$(printf '%s' "$REPO" | tr '/' '_').alias"
  if [ ! -s "$file" ]; then
    mkdir -p "$INCIDENT_DIR" && od -An -N4 -tx1 /dev/urandom | tr -cd '0-9a-f' > "$file" || return 1
  fi
  alias="$(tr -cd '0-9a-f' < "$file")"
  [ "${#alias}" -eq 8 ] || return 1
  printf 'private repo %s' "$alias"
}
# The public repo leak scan over a public repo's incident detail, the one
# part of the text a check or contract supplies. A refused or unavailable
# scan withholds it: the rest is fixed text, the repo and the ticket id.
incident_text_clean() {  # text
  local text_file rc=0
  text_file="$(mktemp "${TMPDIR:-/tmp}/loom-incident.XXXXXX")" || return 1
  printf '%s\n' "$1" > "$text_file"
  leak_scan --text-file "$text_file" \
    --allow-repo-ref "$FACTORY_REPO" --allow-repo-ref "$REPO" >> "$LOG" 2>&1 || rc=1
  rm -f "$text_file"
  return "$rc"
}
file_factory_incident() {  # kind, detail
  local kind="$1" detail="$2" title body number existing subject ticket
  if ! subject="$(incident_subject)"; then
    echo "could not name the incident for a private repo; nothing filed" >> "$LOG"
    printf 'factory incident filing failed'
    return 0
  fi
  ticket="$(incident_ticket)"
  if ! repo_is_public; then
    detail="$INCIDENT_WITHHELD"
  elif ! incident_text_clean "$detail"; then
    echo "the incident detail failed the public repo leak scan; it was withheld" >> "$LOG"
    detail="$INCIDENT_WITHHELD"
  fi
  title="Factory incident: $subject validation $kind"
  body="$(printf '%s\n\nRepo: %s\nKind: %s\nTicket: %s\n\n%s\n' \
    "$INCIDENT_MARKER" "$subject" "$kind" "$ticket" "$detail")"
  existing="$(open_issue "$title")"
  if [ -n "$existing" ]; then
    number="$existing"
    # One comment on the incident with a running count, never one per run.
    printf '%s' "$detail" | upsert_sticky_comment "$FACTORY_REPO" "$number" "$INCIDENT_REPEAT_MARKER" repeat \
      --headline "Still failing" --items-label "Tickets seen" --item "$ticket" || true
  else
    number="$(gh issue create --repo "$FACTORY_REPO" --title "$title" \
      --body "$body" --label loom:factory 2>>"$LOG" || \
      gh issue create --repo "$FACTORY_REPO" --title "$title" \
      --body "$body" 2>>"$LOG")"
    number="${number##*/}"
    case "$number" in ''|*[!0-9]*) number="" ;; esac
  fi
  if [ -n "$number" ]; then
    mkdir -p "$INCIDENT_DIR" && printf '%s\n' "$number" > "$(incident_file "$kind")"
    printf 'factory incident %s#%s' "$FACTORY_REPO" "$number"
  else
    printf 'factory incident filing failed'
  fi
}

# File or update the factory incident for the current failure, append its
# reference to the named reason variable, and park the brief as [factory]
# so the ticket keeps its rung. (Assigns via printf -v, never in a
# subshell, so PARK_CLASS survives.)
note_factory_incident() {  # kind, detail, reason_var_name
  local ref
  ref="$(file_factory_incident "$1" "$2")"
  PARK_CLASS=factory
  printf -v "$3" '%s; %s' "${!3}" "$ref"
}

# A red base check, before the agent. On a cluster runner (LOOM_CLUSTER_RUNNER,
# set by lanes/k8s/lane_job.py) it is often the runner image, and the station
# retries the ticket on another lane, which files the incident if the base is
# red there too: filed from the runner, it held nothing and waited for a
# person to close it.
note_base_check_failure() {  # kind, detail, reason_var_name
  if [ "${LOOM_CLUSTER_RUNNER:-}" = 1 ]; then
    PARK_CLASS=factory
    printf -v "$3" '%s; %s' "${!3}" "no incident filed on a cluster runner: another lane runs the base check"
    echo "base check failed on a cluster runner; no incident filed" >> "$LOG"
    return 0
  fi
  note_factory_incident "$@"
}

# Run one contract command in the tree the way an API-lane agent runs: an
# emptied environment with a scratch HOME, under the sandbox profile (its
# second argument is the interpreter, whose directory stays readable). The
# check after the agent runs the agent's code, so it must never see the
# runner's HOME, keys or SSH agent. Output goes through a pipe to the log.
# The command passed to bash -c is deliberately shell syntax: load_contract
# reads it from the base branch's trusted CI contract, never the agent's
# branch, and contract_command_ok refuses (exit 125, nothing run) any string
# that is not one bounded line of printable ASCII. An optional second
# argument names a file of changed paths (the affected step): it is the
# command's stdin and its text is LOOM_CHANGED_FILES; otherwise stdin is
# /dev/null. The runner opens the file, so the sandbox never reads it.
contract_run() {
  local profile tool uv_bin uv_dir uv_path_dirs="" uv_bins="" changed_list="${2:-}"
  contract_command_ok "$1" || { echo "contract_run: refusing a command that is not printable ASCII" >> "$LOG"; return 125; }
  # A contract that runs `uvx` failed every check with 127 on the unmodified
  # base: the lane machine keeps uv and uvx in the runner's .local/bin,
  # outside the runner's PATH, and the check sandbox refused to read them.
  # Find both with command -v, falling back to the runner's .local/bin,
  # put their directories on the check's PATH, and name their exact paths
  # in CHECK_UV_BINS so the check profile allows those two files only.
  for tool in uv uvx; do
    uv_bin="$(command -v "$tool" 2>/dev/null)" || uv_bin=""
    [ -x "$uv_bin" ] || uv_bin="$HOME/.local/bin/$tool"
    [ -x "$uv_bin" ] || continue
    uv_bin="$(realpath "$uv_bin" 2>/dev/null)" && [ -n "$uv_bin" ] || continue
    uv_dir="$(dirname "$uv_bin")"
    case ":$uv_path_dirs:" in *":$uv_dir:"*) ;; *)
      uv_path_dirs="${uv_path_dirs:+$uv_path_dirs:}$uv_dir" ;; esac
    uv_bins="${uv_bins:+$uv_bins }$uv_bin"
  done
  profile="$(AGENT_HOME="$CHECK_HOME" CHECK_UV_BINS="$uv_bins" sandbox_profile "$TREE" "$CHECK_CLI" check)" || return 125
  sandbox_command "$profile" || return 125
  # shellcheck disable=SC2030,SC2031 # deliberately local to the check's subshell
  (
    cd "$TREE" || exit 125
    keep_path="$PATH" keep_lang="${LANG:-en_US.UTF-8}" home="$CHECK_HOME"
    for v in $(compgen -e); do unset "$v"; done
    # UV_MANAGED_PYTHON: uv uses only the Pythons it installs, never one
    # found on PATH. A PATH interpreter it cannot run (an x86_64 Homebrew
    # python3 on an arm64 host without Rosetta) made uv exit 2 before any
    # test, so every muster lane run failed on the unmodified base
    # (2026-10-16).
    # UV_CACHE_DIR and UV_PYTHON_INSTALL_DIR name uv's own defaults under
    # the scratch home, where agent_uv_cache copies them for a codex agent.
    export PATH="${uv_path_dirs:+$uv_path_dirs:}$keep_path" HOME="$home" TMPDIR="$home/tmp" TERM=dumb LANG="$keep_lang" UV_MANAGED_PYTHON=1
    export UV_CACHE_DIR="$home/.cache/uv" UV_PYTHON_INSTALL_DIR="$home/.local/share/uv/python"
    input=/dev/null
    if [ -n "${changed_list:-}" ]; then
      input="$changed_list"
      LOOM_CHANGED_FILES="$(cat "$input")" || exit 125
      export LOOM_CHANGED_FILES
    fi
    exec timeout "$CHECK_TIMEOUT" "${SANDBOX_CMD[@]}" /bin/bash -c "$1" < "$input"
  ) 2>&1 | timeout "$((CHECK_TIMEOUT + 60))" cat >> "$LOG"
}

tracked_changes() {
  safe_git status --porcelain --untracked-files=no --ignore-submodules=dirty | head -5 | tr '\n' ' '
}

# The first submodule whose tracked files the agent changed, as "path (file
# file ...)"; empty when there is none. A push of this repo carries only the
# submodule's commit, never edits on top of it, so a check run on those edits
# passes on code CI never sees: a pull request passed its
# lane check and failed CI on the C++ it needed (2026-10-21).
submodule_edits() {
  local line path files
  while read -r line; do
    path="$(printf '%s' "$line" | awk '{print $2}')"
    # Not checked out: git in its empty directory would read this repo. A
    # moved commit (a pin bump) is pushed and CI checks it out, so it passes.
    case "$line" in -*) continue ;; esac
    files="$(safe_git -C "$path" status --porcelain --untracked-files=no 2>> "$LOG" |
             cut -c4- | head -3 | sed "s|^|$path/|" | tr '\n' ' ')"
    [ -z "$files" ] || { printf '%s (%s)' "$path" "${files% }"; return 0; }
  done < <(safe_git submodule status --recursive 2>> "$LOG")
}

# Check the trusted default branch in its own worktree when a fix branch is
# red or has merge conflicts. The merged branch and its setup files remain
# available for the agent. DEFAULT_EXIT is the default branch's check result.
check_default_branch() {
  local branch_tree="$TREE" branch_home="$CHECK_HOME" default_tree="$WORKDIR/$TICKET-default-check" rc=0
  DEFAULT_EXIT="" DEFAULT_CHANGED=""
  DEFAULT_REV="$(safe_git rev-parse "origin/$FIX_DEFAULT")" || {
    SETUP_REASON="could not read the default branch revision"; return 1;
  }
  # A local clone, not a worktree: a worktree's .git is a file naming the
  # fix tree's .git, which the check sandbox cannot read, so every check that
  # runs git failed there (loom's metrics tests, a false base-check incident
  # on each loom fix round, 2026-10-19). The clone hardlinks its objects and
  # takes the real origin, so relative submodule URLs still resolve. The fix
  # tree is shallow, so git ignores --local and copies only its branches:
  # the fetch brings origin's default branch across, which a conflicting
  # round has not merged (2026-10-20). A checkout that still fails is the
  # runner's fault, not the ticket's: a factory incident, so the round
  # spends no ladder attempt and never counts toward the runaway hold.
  if ! { timeout "$NET_TIMEOUT" git -c core.hooksPath=/dev/null -c core.fsmonitor=false \
           clone -q --local --no-checkout "$branch_tree" "$default_tree" >> "$LOG" 2>&1 &&
         timeout "$NET_TIMEOUT" git -C "$default_tree" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
           fetch -q --update-shallow "$branch_tree" "+refs/remotes/origin/$FIX_DEFAULT:refs/remotes/origin/$FIX_DEFAULT" >> "$LOG" 2>&1 &&
         git -C "$default_tree" -c core.hooksPath=/dev/null remote set-url origin \
           "$(safe_git remote get-url origin)" >> "$LOG" 2>&1 &&
         git -C "$default_tree" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
           checkout -q --detach "$DEFAULT_REV" >> "$LOG" 2>&1; }; then
    rm -rf "$default_tree"
    SETUP_REASON="could not check out the default branch for validation"
    note_factory_incident environment "could not check out $FIX_DEFAULT at $DEFAULT_REV for validation" SETUP_REASON
    return 1
  fi
  TREE="$default_tree" CHECK_HOME="$branch_home-default"
  if ! mkdir -p "$CHECK_HOME/tmp"; then
    SETUP_REASON="could not prepare the default branch check home"; rc=1
  fi
  if [ "$rc" -eq 0 ] && [ "$C_SUBMODULES" = yes ] && ! timeout "$CHECK_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null \
         -c core.fsmonitor=false submodule update --init --recursive >> "$LOG" 2>&1; then
    SETUP_REASON="default branch submodule checkout failed"; rc=1
  fi
  if [ "$rc" -eq 0 ] && [ -n "$C_SETUP" ]; then
    contract_run "$C_SETUP"; rc=$?
    [ "$rc" -eq 0 ] || SETUP_REASON="default branch setup exited $rc"
  fi
  if [ "$rc" -eq 0 ]; then
    local check_start failures=""
    check_start="$(log_size)"
    contract_run "$C_CHECK"; DEFAULT_EXIT=$?
    DEFAULT_CHANGED="$(tracked_changes)"
    [ "$DEFAULT_EXIT" -eq 0 ] || failures="$(check_failures "$check_start")"
    record default-baseline "$C_CHECK" "$DEFAULT_EXIT" "$DEFAULT_REV" \
      "$([ "$DEFAULT_EXIT" -eq 0 ] && [ -z "$DEFAULT_CHANGED" ] && echo true || echo false)" \
      "$(join_detail "${DEFAULT_CHANGED:+changed tracked files: $DEFAULT_CHANGED}" "$failures")"
  fi
  TREE="$branch_tree" CHECK_HOME="$branch_home"
  rm -rf "$branch_home-default"
  if ! rm -rf "$default_tree"; then
    SETUP_REASON="could not remove the default branch validation tree"; return 1
  fi
  [ "$rc" -eq 0 ]
}

# What setup and the first check leave untracked (a venv, caches) stays out
# of the agent's commit: each path goes into .git/info/exclude, which an
# API-lane agent cannot write.
exclude_untracked() {
  local p
  mkdir -p "$TREE/.git/info"
  while IFS= read -r -d '' p; do
    [[ "$p" =~ ^[A-Za-z0-9._@+/-]+$ ]] || { SETUP_REASON="setup left a file whose name the runner will not exclude"; return 1; }
    printf '/%s\n' "$p" >> "$TREE/.git/info/exclude"
  done < <(safe_git ls-files -z --others --exclude-standard --directory)
}

# Before the agent: interpreter, submodules, setup, then the check on the
# current tree. A fix round checks the branch after merging the default
# branch; a red result is compared with the default branch's own head.
# Only a red default branch check is a base-check factory incident. Returns
# 1 with SETUP_REASON when the environment cannot be prepared.
prepare_validation() {
  local rc interp="" subs="" changed what baseline_rev tool="" verdict_key interpreter_id now cached_verdict=0
  local check_start=0 failures=""
  SETUP_REASON=""
  load_contract || { record prepare "$CONTRACT_FILE" 1 "$BASE" false "$SETUP_REASON"; return 1; }
  [ -n "$HAS_CONTRACT" ] || return 0
  what="interpreter ${C_INTERP:-none}; submodules $C_SUBMODULES"
  if ! sandbox_ready; then
    SETUP_REASON="$SANDBOX_WHY"
    note_factory_incident environment "$SANDBOX_WHY" SETUP_REASON
  elif ! tool="$(command -v sha256sum)" || ! tool_runs "$tool"; then
    # The host's fault, whatever the repo: a check that hashes a file would
    # read as a red default branch and file a base-check incident.
    SETUP_REASON="lane host fault: sha256sum${tool:+ at $tool} cannot run"
    note_factory_incident environment "sha256sum: ${tool:-not on PATH}" SETUP_REASON
  elif [ -n "$C_INTERP" ] && ! interp="$(command -v "$C_INTERP")"; then
    SETUP_REASON="interpreter $C_INTERP is not on PATH"
    note_factory_incident environment "interpreter: $C_INTERP" SETUP_REASON
  elif [ "$C_SUBMODULES" = yes ] && ! timeout "$CHECK_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null \
         -c core.fsmonitor=false submodule update --init --recursive >> "$LOG" 2>&1; then
    SETUP_REASON="submodule checkout failed"
    note_factory_incident environment "submodule checkout failed" SETUP_REASON
  fi
  if [ -n "$SETUP_REASON" ]; then record prepare "$what" 1 "$BASE" false "$SETUP_REASON"; return 1; fi
  [ -n "$interp" ] && interp="$interp $("$interp" --version 2>&1 | head -1)"
  [ "$C_SUBMODULES" = yes ] && subs="$(safe_git submodule status --recursive | tr '\n' ';')"
  record prepare "$what" 0 "$BASE" true "interpreter: ${interp:-none}; submodules: ${subs:-none}"
  CHECK_CLI="${C_INTERP:-bash}"
  rm -rf "$CHECK_HOME"
  mkdir -p "$CHECK_HOME/tmp"
  if [ -n "$CONFLICTS" ]; then
    check_default_branch || return 1
    BASELINE_EXIT="$DEFAULT_EXIT"
    if [ -n "$DEFAULT_CHANGED" ]; then
      SETUP_REASON="default branch check changed tracked files: $DEFAULT_CHANGED"
      return 1
    fi
    if [ "$BASELINE_EXIT" -ne 0 ]; then
      SETUP_REASON="base check exited $BASELINE_EXIT on the default branch"
      note_base_check_failure base-check "check: $C_CHECK (exit $BASELINE_EXIT)" SETUP_REASON
      return 1
    fi
    return 0
  fi
  # C_SETUP and C_CHECK are shell on purpose, like a CI run: step. They are
  # validated in load_contract: read only from the base branch (never the
  # agent's), one line of printable ASCII, at most 1000 characters; and
  # contract_run runs them sandboxed with an emptied environment.
  if [ -n "$C_SETUP" ]; then
    check_start="$(log_size)"
    contract_run "$C_SETUP"; rc=$?
    [ "$rc" -eq 0 ] || failures="$(check_failures "$check_start")"
    record setup "$C_SETUP" "$rc" "$BASE" "$([ "$rc" -eq 0 ] && echo true || echo false)" "$failures"
    if [ "$rc" -eq 143 ]; then
      PARK_CLASS=transient
      SETUP_REASON="setup interrupted by SIGTERM"
      return 1
    fi
    [ "$rc" -eq 0 ] || { SETUP_REASON="setup exited $rc"; return 1; }
  fi
  baseline_rev="$(safe_git rev-parse HEAD)"
  # interp already holds "path version" (set above), so it is the identity as
  # it stands; running it again would hand its own output to the key.
  interpreter_id="${interp:-${C_INTERP:-none}}"
  verdict_key="$(base_verdict_key "$baseline_rev" "$interpreter_id")"
  now="$(date +%s)"
  if base_verdict_read "$verdict_key" "$now"; then
    cached_verdict=1
    changed=""
    echo "base check reused cached verdict for $baseline_rev" >> "$LOG"
    record baseline "$C_CHECK" "$BASELINE_EXIT" "$baseline_rev" "$([ "$BASELINE_EXIT" -eq 0 ] && echo true || echo false)" "cached verdict"
  else
    check_start="$(log_size)"
    contract_run "$C_CHECK"; BASELINE_EXIT=$?
    changed="$(tracked_changes)"
    [ "$BASELINE_EXIT" -eq 0 ] || failures="$(check_failures "$check_start")"
    record baseline "$C_CHECK" "$BASELINE_EXIT" "$baseline_rev" "$([ "$BASELINE_EXIT" -eq 0 ] && [ -z "$changed" ] && echo true || echo false)" \
      "$(join_detail "${changed:+changed tracked files: $changed}" "$failures")"
  fi
  if [ "$BASELINE_EXIT" -eq 143 ]; then
    PARK_CLASS=transient
    SETUP_REASON="base check interrupted by SIGTERM"
    return 1
  fi
  # A check that cannot run, or is already red, on the unmodified base is
  # the factory's problem, not the ticket's: file the incident and stop
  # before the agent burns a run it can never validate.
  case "$BASELINE_EXIT" in
    125|126|127)
      SETUP_REASON="validation environment unavailable: the check exited $BASELINE_EXIT on the unmodified base"
      note_base_check_failure environment "check: $C_CHECK (exit $BASELINE_EXIT)" SETUP_REASON
      return 1 ;;
  esac
  # One red run does not make the base red: Loom's own check timed out
  # under a busy station's load and passed on the next ticket's run, and
  # each such flake held its ticket behind a factory incident (56 by
  # 2026-10-25). A red base check runs once more; only a second red files.
  # A cached red skips the rerun; a fresh red always reruns. The flag is set
  # once above, so a verdict another lane writes meanwhile cannot change it.
  if [ "$BASELINE_EXIT" -ne 0 ] && [ "$cached_verdict" -eq 0 ]; then
    if [ -n "$changed" ] && ! safe_git reset --hard HEAD >> "$LOG" 2>&1; then
      SETUP_REASON="could not restore tracked files changed by the baseline check"
      return 1
    fi
    local first_exit="$BASELINE_EXIT"
    check_start="$(log_size)" failures=""
    contract_run "$C_CHECK"; BASELINE_EXIT=$?
    changed="$(tracked_changes)"
    [ "$BASELINE_EXIT" -eq 0 ] || failures="$(check_failures "$check_start")"
    record baseline "$C_CHECK" "$BASELINE_EXIT" "$baseline_rev" "$([ "$BASELINE_EXIT" -eq 0 ] && [ -z "$changed" ] && echo true || echo false)" \
      "$(join_detail "second run after exit $first_exit" "${changed:+changed tracked files: $changed}" "$failures")"
    if [ "$BASELINE_EXIT" -eq 143 ]; then
      PARK_CLASS=transient
      SETUP_REASON="base check interrupted by SIGTERM"
      return 1
    fi
    [ "$BASELINE_EXIT" -ne 0 ] || echo "base check exited $first_exit, then passed on a second run" >> "$LOG"
  fi
  if [ "$cached_verdict" -eq 0 ] && [ -z "$changed" ] && [ "$BASELINE_EXIT" -ne 143 ] && [ "$BASELINE_EXIT" -ne 125 ] &&
     [ "$BASELINE_EXIT" -ne 126 ] && [ "$BASELINE_EXIT" -ne 127 ]; then
    base_verdict_write "$verdict_key" "$BASELINE_EXIT" ||
      echo "could not store base check verdict for $baseline_rev" >> "$LOG"
  fi
  # Only a pass on the trusted base clears an incident: a fix round's tree
  # holds the agent's unmerged edits, so it counts only once the default
  # branch itself passes (below).
  [ -z "$FIX_PR" ] && [ "$BASELINE_EXIT" -eq 0 ] && [ -z "$changed" ] && resolve_factory_incidents
  if [ "$BASELINE_EXIT" -ne 0 ]; then
    if [ -n "$FIX_PR" ]; then
      check_default_branch || return 1
      if [ "$DEFAULT_EXIT" -eq 0 ] && [ -z "$DEFAULT_CHANGED" ]; then
        resolve_factory_incidents
        if [ -n "$changed" ] && ! safe_git reset --hard HEAD >> "$LOG" 2>&1; then
          SETUP_REASON="could not restore tracked files changed by the baseline check"
          return 1
        fi
        exclude_untracked
        return $?
      fi
      if [ "$DEFAULT_EXIT" -eq 0 ]; then
        SETUP_REASON="default branch check changed tracked files: $DEFAULT_CHANGED"
        return 1
      fi
      BASELINE_EXIT="$DEFAULT_EXIT"
    fi
    SETUP_REASON="base check exited $BASELINE_EXIT on the unmodified repo"
    note_base_check_failure base-check "check: $C_CHECK (exit $BASELINE_EXIT)" SETUP_REASON
    return 1
  fi
  [ -z "$changed" ] || { SETUP_REASON="setup or check changed tracked files on the base: $changed"; return 1; }
  exclude_untracked
}

# One agent run over the tree, in its own process group (see the note at the
# call), with its credentials set up and taken away again. Sets STATUS to the
# agent's exit code. Returns 1 with LAUNCH_WHY set when the run could not be
# set up or cleaned up.
launch_agent() {
  LAUNCH_WHY=""
  LOG_START="$(log_size)"
  if ! agent_gh_config; then LAUNCH_WHY="$AGENT_GH_WHY"; return 1; fi
  rm -f "$LOCK/watchdog-limit"
  # The job's proof that an agent started: without it a cluster run fails in
  # Actions (lanes/k8s/lane_job.py run), whatever this runner exits.
  [ "${LOOM_CLUSTER_RUNNER:-}" != 1 ] || : > "$LOGDIR/$TICKET.agent-ran"
  ( set -m
    (cd "$TREE" && unset GH_TOKEN GITHUB_TOKEN "${!LOOM_GITHUB_APP_@}" "${!GIT_CONFIG_@}" &&
      { [ -z "$AGENT_GH_CONFIG" ] || export GH_CONFIG_DIR="$AGENT_GH_CONFIG"; } &&
      run_agent "$1") >> "$LOG" 2>&1 &
    agent_pid=$!
    echo $agent_pid > "$LOCK/pgid"
    ( agent_watchdog "$agent_pid" "$LOG_START" "$LOG" ) &
    watchdog_pid=$!
    wait $agent_pid
    agent_status=$?
    kill -TERM -- "-$watchdog_pid" 2>/dev/null || kill -TERM "$watchdog_pid" 2>/dev/null || true
    wait $watchdog_pid 2>/dev/null || true
    exit $agent_status ) &
  wait $!
  STATUS=$?
  AGENT_GH_CALLS="$((${AGENT_GH_CALLS:-0} + $(agent_gh_calls)))"
  # The token in the store outlives the agent by up to an hour: remove it now.
  if [ -n "$AGENT_GH_CONFIG" ] && ! rm -rf "$AGENT_GH_CONFIG"; then
    LAUNCH_WHY="could not remove the agent's gh credential store"; return 1
  fi
  is_api_lane && archive_transcripts
  if [ "$MODEL_KIND" = paid ] && [ -f "$WORKDIR/$TICKET.opencode-events" ]; then
    # An unknown spend fails closed: the marker stops paid picks for an hour.
    if ! model_pick settle "$MODEL" < "$WORKDIR/$TICKET.opencode-events" 2>> "$LOG"; then
      date +%s > "$LOOM_HOME/state/usage/go-budget.unsettled"
      echo "[$(date -u +%FT%TZ)] $TICKET could not settle $MODEL: paid models stop for an hour" | tee -a "$LOG" >&2
    fi
    rm -f "$WORKDIR/$TICKET.opencode-events"
  fi
  if ! lane_app_token; then LAUNCH_WHY="$APP_TOKEN_WHY"; return 1; fi
}

# The range a public repo's leak scan reads. A fix round that merged the
# default branch scans what the pull request adds to it: BASE..HEAD would hold
# all the default branch gained since, text that branch's own CI accepts and
# the scan of a lane's change flags.
scan_range() {
  if [ -n "$FIX_UPSTREAM" ]; then echo "$FIX_UPSTREAM...HEAD"; else echo "$BASE..HEAD"; fi
}

# The commit the review compares the branch with, as CI sees it: BASE on a new
# run, the merge base with the default branch on a fix round.
review_base() {
  local upstream="${FIX_UPSTREAM:-}"
  if [ -z "$upstream" ] && [ -n "${FIX_PR:-}" ] && [ -n "${FIX_DEFAULT:-}" ]; then
    upstream="$(safe_git rev-parse --verify -q "origin/$FIX_DEFAULT^{commit}")" || upstream=""
  fi
  if [ -n "$upstream" ]; then safe_git merge-base "$upstream" HEAD; else printf '%s\n' "$BASE"; fi
}

# The checks that refuse a push, run on the committed head. Sets PREPUSH_CLASS
# and PREPUSH_TEXT and returns 1 on a refusal, the leak scan first. A check
# that could not run refuses nothing here: the leak scan's own gate before the
# push still decides, and the review bot reads the complexity.
prepush_refusal() {
  local out rc base
  PREPUSH_CLASS="" PREPUSH_TEXT=""
  if [ "$REPO_PRIVATE" = false ]; then
    rc=0
    out="$(cd "$TREE" && leak_scan --diff "$(scan_range)" --allow-repo-ref "$REPO" 2>&1)" || rc=$?
    if [ "$rc" -eq 1 ]; then
      PREPUSH_CLASS="leak scan"
      PREPUSH_TEXT="$out
Next action: edit each line named above so it no longer matches. Build a cross-repo issue reference in the test at run time, \"%s#%d\" % (repo, number), or write NAME#N; use a documentation-range address for an IP. A line that must keep the text carries leak-guard-allow."
      return 1
    fi
  fi
  base="$(review_base 2>> "$LOG")" || base=""
  [ -n "$base" ] || return 0
  rc=0
  out="$(PYTHONPATH="$REPO_ROOT" timeout "$NET_TIMEOUT" python3 -B -m bus.complexity_gate \
    --tree "$TREE" --base "$base" 2>> "$LOG")" || rc=$?
  # Exit 1 with no text is a crash of the check, not a refusal.
  if [ "$rc" -eq 1 ] && [ -n "$out" ]; then
    PREPUSH_CLASS="complexity"
    PREPUSH_TEXT="$out"
    return 1
  fi
}

# Commit what the retry run left, as the first run's edits were, and put the
# branch at that commit. The Intent summary and replies of the first run stand,
# and what it dropped stays named. Edits inside a submodule are not committed:
# a push of this repo does not carry them.
prepush_commit() {
  local head dropped="$DROPPED_FILES" scratch="$DROPPED_SCRATCH_FILES"
  rm -f "$TREE/$SUMMARY_FILE" "$TREE/.loom/replies.json"
  [ -z "$(submodule_edits)" ] || return 1
  if [ -n "$(safe_git status --porcelain)" ]; then
    safe_git add -A && safe_git commit -qm "${TITLE:-$TICKET}: fix the pre-push refusal" || return 1
  fi
  head="$(safe_git rev-parse HEAD)" || return 1
  safe_git update-ref "refs/heads/$BRANCH" "$head" && safe_git checkout -q "$BRANCH" &&
    drop_agent_files || return 1
  DROPPED_FILES="$(printf '%s\n%s\n' "$dropped" "$DROPPED_FILES" | sed '/^$/d' | sort -u)"
  DROPPED_SCRATCH_FILES="$(printf '%s\n%s\n' "$scratch" "$DROPPED_SCRATCH_FILES" | sed '/^$/d' | sort -u)"
}

# One agent run on the refusal: the brief, then the failure text, under the
# short timeout. Returns 1 when the brief could not be written or the run
# could not be set up (LAUNCH_WHY says why).
prepush_retry_run() {
  local retry_brief="$WORKDIR/$TICKET.run.md" keep_timeout="$AGENT_TIMEOUT" rc=0
  if ! { { cat "$RUN_BRIEF"
           printf '\n\n## Pre-push refusal\n\nYour work is committed on this branch, and the %s check refused it before any push:\n\n%s\n\nDo only that, then stop.\n' "$PREPUSH_CLASS" "$PREPUSH_TEXT"
         } > "$retry_brief.new" && mv "$retry_brief.new" "$retry_brief"; }; then
    LAUNCH_WHY="could not write the pre-push retry brief"
    return 1
  fi
  RUN_BRIEF="$retry_brief"
  AGENT_TIMEOUT="$PREPUSH_TIMEOUT"
  launch_agent "$RUN_BRIEF" || rc=1
  AGENT_TIMEOUT="$keep_timeout"
  return "$rc"
}

# What the agent gets for what would be refused after the push, in the same
# run, so a refusal costs a local retry and not a whole round
# (LOOM_PREPUSH_RETRIES). A retry that fails leaves the first commit, which
# meets the same gates as before. A leak refusal that stands parks here, before
# the long check, as transient when the retry never finished; a complexity one
# is pushed, since the review bot stays its judge.
prepush_gate() {
  local tries=0 before class=""
  while ! prepush_refusal; do
    before="$(safe_git rev-parse HEAD)"
    record prepush "$PREPUSH_CLASS" 1 "$before" false "$PREPUSH_TEXT"
    if [ "$tries" -ge "$PREPUSH_RETRIES" ]; then
      echo "[$(date -u +%FT%TZ)] $TICKET pre-push refusal ($PREPUSH_CLASS) stands after the retry" >> "$LOG"
      [ "$PREPUSH_CLASS" != "leak scan" ] && return 0
      park "$BRIEF" "public repo leak scan failed; nothing pushed: $PREPUSH_TEXT" "$class"
      return 1
    fi
    tries=$((tries + 1))
    echo "[$(date -u +%FT%TZ)] $TICKET pre-push refusal ($PREPUSH_CLASS): retry $tries of $PREPUSH_RETRIES" >> "$LOG"
    if ! prepush_retry_run; then park "$BRIEF" "$LAUNCH_WHY" transient; return 1; fi
    if [ "$STATUS" -ne 0 ] || ! prepush_commit >> "$LOG" 2>&1; then
      [ "$STATUS" -eq 0 ] || class=transient
      echo "[$(date -u +%FT%TZ)] $TICKET pre-push retry ended without a commit (exit $STATUS): the first commit stands" >> "$LOG"
      if ! { safe_git reset -q --hard "$before" && safe_git clean -qfd; } >> "$LOG" 2>&1; then
        park "$BRIEF" "could not restore the first commit after the pre-push retry" transient
        return 1
      fi
    fi
  done
  [ "$tries" -eq 0 ] || echo "[$(date -u +%FT%TZ)] $TICKET pre-push refusal cleared in the same run" >> "$LOG"
}

# After the agent, on the committed branch head: the same check again. Sets
# VALIDATION_REASON when it did not pass.
final_validation() {
  local rc first_exit rev changed ok=false
  VALIDATION_REASON=""
  safe_git checkout -q "$BRANCH" >> "$LOG" 2>&1
  rev="$(safe_git rev-parse HEAD)"
  if ! remove_untracked_venvs; then
    VALIDATION_REASON="validation environment unavailable: could not remove untracked virtual environments"
    note_factory_incident environment "$VALIDATION_REASON" VALIDATION_REASON
    record final "$C_CHECK" 125 "$rev" false "$VALIDATION_REASON"
    drop_check_home
    return 1
  fi
  contract_run "$C_CHECK"; rc=$?
  if validation_interpreter_denied "$rc" "$(tail -c +$((LOG_START + 1)) "$LOG")"; then
    VALIDATION_REASON="validation environment unavailable: Python interpreter query was denied by the sandbox"
    note_factory_incident environment "$VALIDATION_REASON" VALIDATION_REASON
    record final "$C_CHECK" "$rc" "$rev" false "$VALIDATION_REASON"
    drop_check_home
    return 1
  fi
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && [ "$rc" -ne 143 ] &&
     [ "$rc" -ne 125 ] && [ "$rc" -ne 126 ] && [ "$rc" -ne 127 ]; then
    first_exit="$rc"
    record final "$C_CHECK" "$rc" "$rev" false
    contract_run "$C_CHECK"; rc=$?
    if validation_interpreter_denied "$rc" "$(tail -c +$((LOG_START + 1)) "$LOG")"; then
      VALIDATION_REASON="validation environment unavailable: Python interpreter query was denied by the sandbox"
      note_factory_incident environment "$VALIDATION_REASON" VALIDATION_REASON
      record final "$C_CHECK" "$rc" "$rev" false "second run after exit $first_exit"
      drop_check_home
      return 1
    fi
    echo "final check exited $first_exit, then $( [ "$rc" -eq 0 ] && echo passed || printf 'exited %s' "$rc") on a second run" >> "$LOG"
  else
    first_exit=""
  fi
  changed="$(tracked_changes)"
  case "$rc" in
    0) [ -z "$changed" ] && ok=true || VALIDATION_REASON="validation failed: the check changed tracked files ($changed)" ;;
    124) VALIDATION_REASON="validation failed: the check timed out after ${CHECK_TIMEOUT}s" ;;
    143)
      PARK_CLASS=transient
      VALIDATION_REASON="validation interrupted by SIGTERM"
      ;;
    125|126|127)
      # The environment broke between the baseline and the final check;
      # the agent's work is not the cause.
      VALIDATION_REASON="validation environment unavailable: the check exited $rc"
      note_factory_incident environment "check: $C_CHECK (exit $rc)" VALIDATION_REASON
      ;;
    *) VALIDATION_REASON="validation failed: the check exited $rc" ;;
  esac
  record final "$C_CHECK" "$rc" "$rev" "$ok" "${first_exit:+second run after exit $first_exit}${changed:+${first_exit:+; }changed tracked files: $changed}"
  [ "$ok" != true ] || [ -z "${C_AFFECTED:-}" ] || final_affected "$rev" || ok=false
  # The last check of this brief has run; the next one starts a fresh home.
  drop_check_home
  [ "$ok" = true ] || { VALIDATION_REASON="$VALIDATION_REASON at ${rev:0:12} (base check exited $BASELINE_EXIT)"; return 1; }
  VALIDATED="check exited 0 at ${rev:0:12}"
  [ -z "$first_exit" ] || VALIDATED="final check passed on a second run at ${rev:0:12}"
  [ -z "${C_AFFECTED:-}" ] || VALIDATED="$VALIDATED; affected check exited 0"
}

# The paths the pull request changes, one per line: the lane's commits on a
# new run; on a fix round, everything the pull request adds to the default
# branch (the round's commits and the earlier ones), as CI sees it. A round
# whose branch already held the default branch sets no FIX_UPSTREAM, so the
# fetched default branch is the other end.
lane_changed_files() {
  local range="$BASE..HEAD" upstream="${FIX_UPSTREAM:-}"
  if [ -z "$upstream" ] && [ -n "${FIX_PR:-}" ] && [ -n "${FIX_DEFAULT:-}" ]; then
    upstream="$(safe_git rev-parse --verify -q "origin/$FIX_DEFAULT^{commit}")" || upstream=""
  fi
  [ -z "$upstream" ] || range="$upstream...HEAD"
  safe_git diff --name-only "$range"
}

# The contract's affected command after the check passed (loom#1773): the
# per-directory suites path-scoped CI jobs run, which a repo-wide check
# leaves out. A lane pull request in the deploy repo passed its check and
# failed two such suites in CI (a generated file not regenerated, a pinned
# test in another directory). It gets the changed files on stdin and in
# LOOM_CHANGED_FILES, and passes as check does: exit 0, no tracked file
# changed. Sets VALIDATION_REASON and returns 1 when it does not pass.
final_affected() {
  local rev="$1" rc changed list ok=false
  list="$(mktemp "$LOOM_HOME/run/$SLOT-changed.XXXXXX")" || {
    VALIDATION_REASON="validation environment unavailable: could not write the changed file list"
    record affected "$C_AFFECTED" 125 "$rev" false "$VALIDATION_REASON"
    return 1
  }
  if ! lane_changed_files > "$list" 2>> "$LOG"; then
    rm -f "$list"
    VALIDATION_REASON="validation environment unavailable: could not list the changed files"
    record affected "$C_AFFECTED" 125 "$rev" false "$VALIDATION_REASON"
    return 1
  fi
  contract_run "$C_AFFECTED" "$list"; rc=$?
  rm -f "$list"
  changed="$(tracked_changes)"
  case "$rc" in
    0) [ -z "$changed" ] && ok=true ||
         VALIDATION_REASON="validation failed: the affected check changed tracked files ($changed)" ;;
    124) VALIDATION_REASON="validation failed: the affected check timed out after ${CHECK_TIMEOUT}s" ;;
    143)
      PARK_CLASS=transient
      VALIDATION_REASON="validation interrupted by SIGTERM"
      ;;
    125|126|127)
      VALIDATION_REASON="validation environment unavailable: the affected check exited $rc"
      note_factory_incident environment "affected: $C_AFFECTED (exit $rc)" VALIDATION_REASON
      ;;
    *) VALIDATION_REASON="validation failed: the affected check exited $rc" ;;
  esac
  record affected "$C_AFFECTED" "$rc" "$rev" "$ok" "${changed:+changed tracked files: $changed}"
  [ "$ok" = true ]
}

# Run the check of the contract the lane commits (C_* hold it) on the lane's
# own tree, in the check sandbox. Its setup runs only when it differs from
# the base setup in $1, which prepare_validation already ran. Sets CONTRACT_REFUSAL
# and returns 1 when the interpreter is missing, a step fails or the check
# changes tracked files; 2 when the runner cannot run a check at all.
run_committed_contract() {
  local base_setup="$1" rc changed rev
  sandbox_ready || { CONTRACT_REFUSAL="$SANDBOX_WHY"; return 2; }
  if [ -n "$C_INTERP" ] && ! command -v "$C_INTERP" > /dev/null; then
    CONTRACT_REFUSAL="the contract names interpreter $C_INTERP, which is not on the lane machine's PATH"
    return 1
  fi
  CHECK_CLI="${C_INTERP:-bash}"
  mkdir -p "$CHECK_HOME/tmp" || { CONTRACT_REFUSAL="could not prepare the check home"; return 2; }
  rev="$(safe_git rev-parse HEAD)"
  if [ -n "$C_SETUP" ] && [ "$C_SETUP" != "$base_setup" ]; then
    contract_run "$C_SETUP"; rc=$?
    record contract-setup "$C_SETUP" "$rc" "$rev" "$([ "$rc" -eq 0 ] && echo true || echo false)"
    [ "$rc" -eq 0 ] || { CONTRACT_REFUSAL="the setup in $CONTRACT_FILE exited $rc on the lane's own tree"; return 1; }
  fi
  contract_run "$C_CHECK"; rc=$?
  changed="$(tracked_changes)"
  record contract-check "$C_CHECK" "$rc" "$rev" "$([ "$rc" -eq 0 ] && [ -z "$changed" ] && echo true || echo false)" \
    "${changed:+changed tracked files: $changed}"
  if [ "$rc" -ne 0 ]; then
    CONTRACT_REFUSAL="the check in $CONTRACT_FILE exited $rc on the lane's own tree"
  elif [ -n "$changed" ]; then
    CONTRACT_REFUSAL="the check in $CONTRACT_FILE changed tracked files on the lane's own tree ($changed)"
  fi
  [ -z "$CONTRACT_REFUSAL" ]
}

# A lane change that adds or edits the validation contract must read and
# pass under itself (loom#1290): the runner checks a change against the
# base's contract, so a contract the lane wrote was never read or run before
# it merged, and a broken one failed every later lane run on the repo. When
# the lane's commits (the pull request's own changes on a fix round) touch
# the file, the runner reads the committed blob with bus.contract and runs
# its check on the lane's tree. Returns 0 when the file is untouched, gone or
# good, 1 with CONTRACT_REFUSAL when it is not, 2 when the check could not
# run. The base contract's settings are restored either way.
check_contract_change() {
  local range="$BASE..HEAD" touched mode canon rc=0
  local b_interp="$C_INTERP" b_subs="$C_SUBMODULES" b_setup="$C_SETUP" b_check="$C_CHECK" b_cli="${CHECK_CLI:-}"
  local b_affected="${C_AFFECTED:-}"
  CONTRACT_REFUSAL=""
  [ -z "$FIX_UPSTREAM" ] || range="$FIX_UPSTREAM...HEAD"
  touched="$(safe_git diff --name-only "$range" -- "$CONTRACT_FILE" 2>> "$LOG")" ||
    { CONTRACT_REFUSAL="could not read the lane's changes"; return 2; }
  [ -n "$touched" ] || return 0
  mode="$(safe_git ls-tree HEAD -- "$CONTRACT_FILE" | cut -d' ' -f1)"
  [ -n "$mode" ] || return 0
  if [ "$mode" != 100644 ] && [ "$mode" != 100755 ]; then
    CONTRACT_REFUSAL="$CONTRACT_FILE is not a regular file"; return 1
  fi
  if ! canon="$(safe_git cat-file blob "HEAD:$CONTRACT_FILE" | PYTHONPATH="$LOOM_ROOT" python3 -B -m bus.contract)"; then
    CONTRACT_REFUSAL="$CONTRACT_FILE is one the runner cannot read: ${canon:0:160}"; return 1
  fi
  if ! parse_contract "$canon"; then
    CONTRACT_REFUSAL="$CONTRACT_FILE is one the runner cannot use: ${SETUP_REASON#"$CONTRACT_FILE "}"; rc=1
  else
    run_committed_contract "$b_setup" || rc=$?
  fi
  C_INTERP="$b_interp" C_SUBMODULES="$b_subs" C_SETUP="$b_setup" C_CHECK="$b_check" CHECK_CLI="$b_cli"
  C_AFFECTED="$b_affected"
  [ -n "$HAS_CONTRACT" ] || drop_check_home
  return "$rc"
}

# Push a commit of the lane tree to a side branch of the ticket's branch and
# say where the edits are. Never touches the ticket's branch or an open pull
# request. A side branch on a public repo is as public as a pull request, so
# the same leak scan runs first, and any repo not known to be private counts
# as public; a refused or unavailable scan keeps no branch.
push_side() {
  local from="$1" side="$2" range
  if [ "${REPO_PRIVATE:-}" != true ]; then
    range="$BASE..$from"
    [ -n "${FIX_UPSTREAM:-}" ] && range="$FIX_UPSTREAM...$from"
    if ! (cd "$TREE" && leak_scan --diff "$range" --allow-repo-ref "$REPO") >> "$LOG" 2>&1; then
      printf 'the public repo leak scan refused or could not check the edits, so no branch was kept; the edits are committed in the lane tree'
      return 1
    fi
  fi
  if timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
       push -q origin "$from:refs/heads/$side" >> "$LOG" 2>&1; then
    printf 'the edits are on branch %s' "$side"
  else
    printf 'the push of %s failed; the edits are committed in the lane tree' "$side"
  fi
}

# A run that failed validation keeps its edits: they are committed and pushed
# to a side branch, never to the ticket's branch or an open pull request.
preserve_unvalidated() {
  push_side "$BRANCH" "$BRANCH-unvalidated-$(date +%s)"
}

# A run that timed out, or hit a limit, after editing keeps its edits the same
# way, on a -timedout- branch the station names to the next attempt. The agent's tree
# may hold uncommitted files, so they are committed first.
preserve_timedout() {
  local tree_status
  if ! rm -f "$TREE/$SUMMARY_FILE" 2>> "$LOG"; then
    printf 'the lane tree could not be prepared; the edits were not preserved'
    return 1
  fi
  if ! tree_status="$(safe_git status --porcelain 2>> "$LOG")"; then
    printf 'could not read the lane tree; the edits were not preserved'
    return 1
  fi
  if [ -n "$tree_status" ] &&
     ! { safe_git add -A && safe_git commit -qm "$TICKET: edits before the timeout"; } >> "$LOG" 2>&1; then
    printf 'the edits could not be committed in the lane tree'
    return 1
  fi
  if ! drop_stray_from_kept >> "$LOG" 2>&1; then
    printf 'the edits hold files outside the brief'"'"'s paths that could not be left out, so no branch was kept'
    return 1
  fi
  push_side HEAD "$BRANCH-timedout-$(date +%s)"
}

# The next attempt merges the kept branch, and its commit is refused for any
# file the brief does not allow, so those files are left off the branch.
# A scope check that cannot run keeps every file: nothing is known wrong, and
# the later refusal still guards the push. Fails, logged, when a stray file
# cannot be removed or the removal committed: a branch that still holds it
# would only get the next attempt refused, so it is not pushed.
drop_stray_from_kept() {
  local f stamp
  stamp="$(date -u +%FT%TZ)"
  if ! check_scope "$BRIEF"; then
    echo "[$stamp] $TICKET: could not check the kept edits against the brief; the kept branch keeps every file"
    return 0
  fi
  [ -n "$STRAY_FILES" ] || return 0
  while IFS= read -r f; do
    [ -z "$f" ] || safe_git rm -q -f -- "$f" && continue
    echo "[$stamp] $TICKET: could not remove $f from the kept edits"
    return 1
  done <<EOF
$STRAY_FILES
EOF
  if ! safe_git commit -qm "$TICKET: leave out files outside the brief's paths"; then
    echo "[$stamp] $TICKET: could not commit the removal of $(stray_list) from the kept edits"
    return 1
  fi
  echo "[$stamp] $TICKET: left off the kept branch: $(stray_list)"
}

# A fix round's commit that adds files outside the brief's paths is not
# refused: those files (STRAY_FILES, and only those, so the round's real
# work stays) are removed in a commit on top, which keeps the push
# fast-forward, and named in the run log. Without this the round retried the
# identical refusal until the runaway hold caught it (loom#1476). Returns 1,
# logged, when a file cannot be removed or the removal committed.
drop_stray_files() {
  local f stamp
  stamp="$(date -u +%FT%TZ)"
  while IFS= read -r f; do
    [ -z "$f" ] || safe_git rm -q -f -- "$f" && continue
    echo "[$stamp] $TICKET: could not remove $f from the fix round's commits"
    return 1
  done <<EOF
$STRAY_FILES
EOF
  if ! safe_git commit -qm "Drop files outside the brief's paths: $(stray_list)"; then
    echo "[$stamp] $TICKET: could not commit the removal of $(stray_list)"
    return 1
  fi
  echo "[$stamp] $TICKET: left off the fix round's commits: $(stray_list)"
}

# What the agent is told about the contract, after its brief.
validation_note() {
  [ -n "$HAS_CONTRACT" ] || return 0
  printf '\n\n## Validation environment\nThe runner prepared this tree from %s (interpreter: %s, submodules: %s, setup: %s). After you finish it runs the check below on your committed tree; the pull request opens only if it exits 0 and changes no tracked file. On the base it exited %s.\n\n    %s\n' \
    "$CONTRACT_FILE" "${C_INTERP:-any}" "$C_SUBMODULES" "${C_SETUP:-none}" "$BASELINE_EXIT" "$C_CHECK"
  [ -z "${C_AFFECTED:-}" ] || printf '\nAfter the check passes it runs this affected command with your changed files on stdin (and in LOOM_CHANGED_FILES); it must pass the same way:\n\n    %s\n' "$C_AFFECTED"
  [ "$C_SUBMODULES" != yes ] || printf '\nNever edit files inside a submodule: the push carries only its pinned commit, so the runner refuses a run that does and pushes nothing.\n'
}

# Keep the agent's session transcripts (Claude Code .jsonl; opencode's SQLite
# session store, older session json, and diagnostic log directory) in
# logs/<lane>/<ticket>/ before the next run wipes its scratch home.
# The agent controls everything in that home, so: enter each parent directory
# and refuse it unless its real path is the expected one (no link on the way;
# once inside, a later swap of the name cannot move us), then hand rsync the
# leaf by name without a trailing slash, so --no-links skips it if it is a link.
# rsync walks the tree itself and never re-parses names, so a name built with
# newlines and ".." cannot reach outside the home or the archive either.
# A failed copy is written to the lane log; it never fails the run.
archive_transcripts() {
  local dest="$LOGDIR/$TICKET" home_real spec parent leaf
  home_real="$(cd "$AGENT_HOME" 2> /dev/null && pwd -P)" || return 0
  for spec in .claude:projects .local/share:opencode; do
    parent="${spec%%:*}" leaf="${spec##*:}"
    (
      cd "$home_real/$parent" 2> /dev/null || exit 0
      [ "$(pwd -P)" = "$home_real/$parent" ] || exit 0
      # No such leaf (an opencode run's .claude holds only skills): nothing to keep.
      [ -e "$leaf" ] || [ -L "$leaf" ] || exit 0
      mkdir -p "$dest/$parent"
      if ! rsync -a --no-links --prune-empty-dirs --include='*/' --include='*.json' \
          --include='*.jsonl' --include='opencode.db' --include='opencode.db-wal' \
          --include='opencode.db-shm' --include='log/***' --exclude='*' \
          "$leaf" "$dest/$parent/" >> "$LOG" 2>&1; then
        echo "[$(date -u +%FT%TZ)] $TICKET: archiving $parent/$leaf failed; transcript may be incomplete" >> "$LOG"
      fi
    )
  done
  return 0
}

# A fresh scratch home for every API-lane run. The agent can write inside it,
# so anything left there (a link into the real HOME, say) must never meet the
# runner, which is not sandboxed. rm -rf removes links without following them.
# The skill snapshot is linked in where Claude Code, the cross-tool folder and
# opencode look for skills; the sandbox lets the agent read it, never write it.
fresh_agent_home() {
  local skills d
  rm -rf "$AGENT_HOME"
  mkdir -p "$AGENT_HOME/tmp"
  skills="$(cd "$SKILLS" 2> /dev/null && pwd -P)" || return 0
  for d in .claude .agents .config/opencode; do
    mkdir -p "$AGENT_HOME/$d" && ln -s "$skills" "$AGENT_HOME/$d/skills"
  done
}

# The operator's skills, for the lanes that cannot read the operator's home:
# a snapshot at $LOOM_HOME/skills of every child folder holding a SKILL.md in
# the folders LOOM_SKILL_DIRS names (the first folder wins a name both hold),
# links followed, refreshed at most hourly by whichever lane gets there
# first. It is built beside the snapshot and swapped in only when the whole
# copy succeeded, so a failed refresh keeps the last good snapshot and logs
# one line. A missing folder is skipped; with none present nothing changes.
SKILLS="$LOOM_HOME/skills"
refresh_skills() {
  local stamp="$LOOM_HOME/run/skills.stamp" lock="$LOOM_HOME/run/skills.lock"
  local next="$LOOM_HOME/skills.next" old="$LOOM_HOME/skills.old" src d name found="" err=""
  local -a dirs
  [ -n "$(find "$stamp" -mmin -60 2> /dev/null)" ] && return 0
  if ! mkdir "$lock" 2> /dev/null; then
    # Another lane is refreshing. A refresher killed mid-copy leaves its lock.
    [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2> /dev/null)" ] && rm -rf "$lock"
    return 0
  fi
  touch "$stamp"
  # shellcheck disable=SC2031 # HOME here is this shell's, not api_agent's subshell's
  IFS=: read -ra dirs <<< "${LOOM_SKILL_DIRS:-$HOME/.agents/skills:$HOME/.claude/skills}"
  for src in ${dirs[@]+"${dirs[@]}"}; do [ -d "$src" ] && found=1; done
  if [ -n "$found" ]; then
    chmod -R u+w "$next" "$old" 2> /dev/null; rm -rf "$next" "$old"
    mkdir "$next" || err="cannot create $next"
    for src in ${dirs[@]+"${dirs[@]}"}; do
      [ -z "$err" ] && [ -d "$src" ] || continue
      for d in "$src"/*/; do
        d="${d%/}" name="${d##*/}"
        [ -f "$d/SKILL.md" ] && [ ! -e "$next/$name" ] || continue
        rsync -aL --delete "$d/" "$next/$name/" > /dev/null 2>&1 ||
          { err="copying $name from $src failed"; break; }
      done
    done
    # Read-only all through, top folder included, so nothing can add, rename
    # or change a skill. The swap renames within $LOOM_HOME and needs no write
    # on the snapshot; removing an old one takes chmod -R u+w first.
    if [ -z "$err" ]; then
      chmod -R a-w "$next" || err="cannot make $next read-only"
    fi
    if [ -z "$err" ]; then
      if ! { { [ ! -e "$SKILLS" ] || mv "$SKILLS" "$old"; } && mv "$next" "$SKILLS"; }; then
        err="cannot swap in the new snapshot"
        [ -e "$SKILLS" ] || mv "$old" "$SKILLS" 2> /dev/null
      fi
    fi
    chmod -R u+w "$next" "$old" 2> /dev/null; rm -rf "$next" "$old"
    if [ -n "$err" ]; then
      mkdir -p "$LOOM_HOME/logs"
      echo "[$(date -u +%FT%TZ)] skill refresh failed, keeping the last snapshot: $err" |
        tee -a "$LOOM_HOME/logs/skills.log" >&2
    fi
  fi
  rm -rf "$lock"
}

# The agy lane's agent is another user and cannot read the snapshot, so the
# runner copies it each run into the folder agy reads global skills from
# (~/.gemini/config/skills), streamed as tar to a shell running as that user.
# The sandbox keeps ~/.gemini/config read-only for the agent. A failed copy is
# logged and the run goes on with whatever skills are there.
agy_skills() {
  [ -d "$SKILLS" ] || return 0
  # shellcheck disable=SC2016 # expanded by the lane user's shell
  tar -C "$SKILLS" -cf - . | "${AGY_RUNAS[@]}" /bin/bash -c '
    d="$1"; mkdir -p "${d%/*}" || exit 1
    # Remove a link without following it; make a copy deletable, then delete.
    if [ -L "$d" ]; then rm -f "$d" || exit 1
    elif [ -e "$d" ]; then { chmod -R u+w "$d" && rm -rf "$d"; } || exit 1; fi
    mkdir "$d" && tar -xf - -C "$d" && chmod -R a-w "$d"' agy-skills "$AGY_HOME/.gemini/config/skills" ||
    echo "could not copy the skill snapshot to the agy lane user" >&2
}

# Lanes and models whose provider may train on prompts (docs/LANES.md lists
# them). Their briefs must name a public repo.
trains_on_prompts() {
  case "$LANE:$MODEL" in zen:*|zai:*|agy:*|copilot:*|opencode-go:opencode/*|opencode-go:*contributor*) return 0 ;; esac
  # Free provider slots that may train on prompts (bus/factory.py PUBLIC_ONLY;
  # tests/test_bus_station.py keeps the two lists equal).
  case "$SLOT" in free-nvidia|free-kilo|free-groq|free-openrouter) return 0 ;; esac
  return 1
}

# Lanes whose agent runs sandboxed in a fresh scratch home (api_agent).
is_api_lane() {
  case "$LANE" in openrouter|opencode-go|anthropic|zen|zai|workersai|spark|free|copilot) return 0 ;; esac
  return 1
}

# A fix brief's branch, brought up to date with the repository's default
# branch before the agent runs, so a lane can resolve a conflict: the agent's
# sandbox cannot write .git, so it can never fetch or merge itself (#145).
# A clean merge stays as a commit. A conflict is left in the tree with its
# markers, CONFLICTS names the files, and the runner completes the merge after
# the agent. UPDATE_REASON is set when the branch could not be updated.
# Every git call below, the fetch and the unshallow fetch included, runs under
# `timeout "$NET_TIMEOUT"`, written on the line before its continuation.
update_fix_branch() {
  local default
  UPDATE_REASON=""; CONFLICTS=""
  default="${BASE_BRANCH:-$(timeout "$NET_TIMEOUT" git -C "$TREE" ls-remote --symref origin HEAD 2>> "$LOG" |
    sed -n 's#^ref: refs/heads/\(.*\)[[:space:]]HEAD$#\1#p')}"
  if ! [[ "$default" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ ]]; then
    UPDATE_REASON="could not read the default branch"; return 1
  fi
  FIX_DEFAULT="$default"
  if ! timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
       fetch -q --depth 200 origin "+refs/heads/$default:refs/remotes/origin/$default" >> "$LOG" 2>&1; then
    UPDATE_REASON="could not fetch $default"; return 1
  fi
  if ! timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
       merge-base HEAD "origin/$default" > /dev/null 2>&1; then
    timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
      fetch -q --unshallow origin >> "$LOG" 2>&1 || true
  fi
  timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
    merge-base --is-ancestor "origin/$default" HEAD 2> /dev/null && return 0
  # The commit, not the ref: the scope check and the leak scan read it after
  # the agent ran.
  if ! FIX_UPSTREAM="$(git -C "$TREE" rev-parse --verify -q "origin/$default^{commit}")" ||
     [ -z "$FIX_UPSTREAM" ]; then
    UPDATE_REASON="could not read origin/$default"; return 1
  fi
  if timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
       merge --no-edit "origin/$default" >> "$LOG" 2>&1; then
    echo "[$(date -u +%FT%TZ)] $TICKET: merged $default into $BRANCH" >> "$LOG"
    return 0
  fi
  # One path a line, never split on spaces or globbed.
  CONFLICTS="$(safe_git -c core.quotePath=false diff --name-only --diff-filter=U)"
  if [ -z "$CONFLICTS" ]; then
    safe_git merge --abort >> "$LOG" 2>&1 || true
    UPDATE_REASON="merging $default into $BRANCH failed"; return 1
  fi
  echo "[$(date -u +%FT%TZ)] $TICKET: merging $default conflicts in $(conflict_list); the agent resolves it" >> "$LOG"
  return 0
}

# An earlier attempt that timed out or hit a limit after editing kept its
# edits on a side branch, which the brief's Earlier attempt section names. The agent cannot
# fetch it: the sandbox keeps .git read-only, and two codex runs on one ticket
# started over and ended with no commit (2026-10-21). So the runner merges it
# into the tree before the agent starts. A branch that will not fetch or merge
# cleanly is left out and logged: the agent starts from the base, as before.
resume_earlier_attempt() {
  local side
  [ -z "$FIX_PR" ] || return 0
  side="$(grep -oE 'kept its edits on branch [A-Za-z0-9._/-]+-timedout-[0-9]+' "$BRIEF" | head -1 | awk '{print $NF}')"
  [ -n "$side" ] || return 0
  if timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
       fetch -q origin "+refs/heads/$side:refs/remotes/origin/$side" >> "$LOG" 2>&1 &&
     safe_git merge --no-edit "origin/$side" >> "$LOG" 2>&1; then
    echo "[$(date -u +%FT%TZ)] $TICKET: merged the earlier attempt's branch $side" >> "$LOG"
    return 0
  fi
  safe_git merge --abort >> "$LOG" 2>&1 || true
  echo "[$(date -u +%FT%TZ)] $TICKET: could not merge the earlier attempt's branch $side; the agent starts from the base" >> "$LOG"
}

# CONFLICTS (one path a line) as one comma-separated line.
conflict_list() {
  printf '%s\n' "$CONFLICTS" | sed '/^$/d' | paste -sd ',' - | sed 's/,/, /g'
}

# Paths of CONFLICTS that still hold a conflict marker line, comma-separated.
# A fix brief's agent may answer a finding instead of fixing it, one file a
# thread in .loom-answers/ (bus/tickets.py says how), and any agent may say
# so for a run that changes nothing. The answers never go in the commit:
# they move to the inbox, where the station posts each on its thread. The
# copy opens the folder and each file without following links, through the
# folder's descriptor, and keeps only a regular file with one
# link, at most 8 KB, named for a thread: a symlink, a hard link to a file
# outside the tree, a FIFO or a folder swapped for a link is dropped unread,
# so an answer cannot carry a file the agent could not read itself.
keep_answers() {
  local dir="$TREE/.loom-answers" dst="$INBOX/answers/$TICKET"
  # A brief with no pull request keeps them aside: they go to the inbox only
  # when the run ends answered (post_no_change).
  if [ -z "$FIX_PR" ]; then dst="$WORKDIR/$TICKET.answers"; rm -rf "$dst"; fi
  [ -e "$dir" ] || [ -L "$dir" ] || return 0
  if ! python3 - "$dir" "$dst" <<'PY' >> "$LOG" 2>&1
import os, re, stat, sys
src, dst = sys.argv[1], sys.argv[2]
nofollow = os.O_RDONLY | os.O_NOFOLLOW
try:
    folder = os.open(src, nofollow | os.O_DIRECTORY)
except OSError:
    sys.exit(0)
try:
    for name in os.listdir(folder):
        if not re.fullmatch(r"PRRT_[A-Za-z0-9_-]{1,100}[.]md", name):
            continue
        try:
            fd = os.open(name, nofollow | os.O_NONBLOCK, dir_fd=folder)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            ok = stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= 8192
            data = os.read(fd, 8193) if ok else b""
        finally:
            os.close(fd)
        if ok and len(data) <= 8192:
            os.makedirs(dst, exist_ok=True)
            with open(os.path.join(dst, name), "wb") as out:
                out.write(data)
finally:
    os.close(folder)
PY
  then
    echo "[$(date -u +%FT%TZ)] $TICKET: the answers in .loom-answers could not be kept" >> "$LOG"
  fi
  rm -rf "$dir"
}

markers_left() {
  local f left=()
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$TREE/$f" ] &&
      grep -qE '^(<<<<<<<|=======|>>>>>>>)( |$)' -- "$TREE/$f" && left+=("$f")
  done <<< "$CONFLICTS"
  [ "${#left[@]}" -gt 0 ] && { local IFS=,; echo "${left[*]}" | sed 's/,/, /g'; }
  return 0
}

# A codex agent has no network, so its tests could not fetch the packages
# the base check had already downloaded (loom#1665). Give it a copy of the
# check's uv cache (a clone on APFS, so it costs no space): uv must write
# the cache it reads, and a copy keeps the agent from changing the cache the
# final check reads. Sets AGENT_UV_DIR and AGENT_UV_PYTHON for agent_cli, or
# leaves them empty when this ticket's base check left no uv cache.
agent_uv_cache() {
  local src="$CHECK_HOME/.cache/uv" dir="$CHECK_HOME/agent-uv"
  AGENT_UV_DIR="" AGENT_UV_PYTHON=""
  [ "$LANE" = codex ] && [ -n "$HAS_CONTRACT" ] && [ -d "$src" ] || return 0
  rm -rf "$dir"
  if mkdir -p "$dir" && { cp -Rc "$src" "$dir/cache" 2> /dev/null ||
       { rm -rf "$dir/cache"; cp -R "$src" "$dir/cache"; }; }; then
    AGENT_UV_DIR="$dir" AGENT_UV_PYTHON="$CHECK_HOME/.local/share/uv/python"
  else
    echo "could not copy the base check's uv cache for the agent; its uv runs without one"
  fi
}

run_agent() {
  local brief_text
  brief_text="$(cat "$1")$(work_note)$(validation_note)$(summary_note)"
  if [ "$LANE" = spark ]; then
    brief_text="$brief_text

## Spark repository search
Do not search the whole filesystem (for example, find /). Search within the checkout and read the repository's build files to learn how it builds and tests."
  fi
  AGENT_EXTRA_ENV=""
  AGENT_LAST_MESSAGE=""
  [ "$LANE" = codex ] && AGENT_LAST_MESSAGE="$WORKDIR/$TICKET.last-message"
  agent_uv_cache
  is_api_lane && fresh_agent_home
  case "$LANE" in
    codex|claude) agent_cli "$LANE" lane "$brief_text" ;;
    openrouter) opencode_agent_continuing OPENROUTER_API_KEY text "$brief_text" ;;
    opencode-go) opencode_agent_continuing OPENCODE_API_KEY text "$brief_text" ;;
    zen) opencode_agent_continuing - text "$brief_text" ;;
    spark) opencode_agent_continuing - raw "$brief_text" ;;
    free) opencode_agent_continuing - text "$brief_text" ;;
    zai) opencode_agent_continuing ZHIPU_API_KEY text "$brief_text" ;;
    workersai) AGENT_EXTRA_ENV="CLOUDFLARE_ACCOUNT_ID=$LANE_ACCOUNT"
      opencode_agent_continuing CLOUDFLARE_API_KEY text "$brief_text" ;;
    anthropic) anthropic_agent "$brief_text" ;;
    agy) agy_agent "$brief_text" ;;
    copilot) api_agent COPILOT_GITHUB_TOKEN copilot -p "$brief_text" --allow-all-tools ;;
    *) echo "unknown lane $LANE" >&2; return 2 ;;
  esac
}

# A watchdog for the running agent wrapper: tails the run log through
# provider_text, and stops the agent early on a terminal limit line
# (AI_RetryError after limit text, not a single retryable 429).
agent_watchdog() {
  local agent="$1" start="${2:-0}" log="$3" seen_limit=0 line
  shopt -s nocasematch
  tail -c +$((start + 1)) -f "$log" 2>/dev/null | provider_text | while IFS= read -r line; do
    if [[ "$line" =~ $LIMIT_RE ]] || [[ "$line" =~ $LIMIT_CODE_RE ]] ||
       [[ "$line" =~ 429 ]] || [[ "$line" =~ GoUsageLimitError ]]; then
      seen_limit=1
    fi
    if [ "$seen_limit" -eq 1 ] && [[ "$line" =~ AI_RetryError ]]; then
      # A picked model is paused after the run, and the slot stays available.
      if [ -z "$MODEL_PICKED" ]; then
        provider_at_limit || true
        hold_provider_limit
      fi
      touch "$LOCK/watchdog-limit"
      kill -TERM -- "-$agent" 2>/dev/null || kill -TERM "$agent" 2>/dev/null || true
      sleep 1
      kill -KILL -- "-$agent" 2>/dev/null || kill -KILL "$agent" 2>/dev/null || true
      break
    fi
  done
}

spend() { PYTHONPATH="$LOOM_ROOT" python3 -B -m bus.spend "$@"; }

# The anthropic lanes spend API credits. The CLI's JSON result carries the
# run's cost: the lane log gets the human-readable result, and
# state/usage/spend.jsonl one line per run, with cost null (unknown, never
# zero) when the run left no result.
anthropic_agent() {
  local out="$LOGDIR/$TICKET.result" status
  api_agent ANTHROPIC_API_KEY claude -p --output-format json --model "$MODEL" "$1" > "$out"
  status=$?
  if ! spend record --output "$out" --slot "$SLOT" --ticket "$TICKET" --model "$MODEL"; then
    cat "$out"
    echo "could not read this run's cost; recording it as unknown" >&2
    printf '{"at": %s, "slot": "%s", "ticket": "%s", "model": "%s", "cost_usd": null}\n' \
      "$(date +%s)" "$SLOT" "$TICKET" "$MODEL" >> "$LOOM_HOME/state/usage/spend.jsonl"
  fi
  rm -f "$out"
  return "$status"
}

# With LOOM_ANTHROPIC_BUDGET_USD set, credit under the floor (or a budget
# setting that does not parse) makes the anthropic slots unavailable.
anthropic_credit_ok() {
  [ -n "${LOOM_ANTHROPIC_BUDGET_USD:-}" ] || return 0
  local why
  why="$(spend floor 2>&1)" && return 0
  READY_REASON="${why:-could not check the anthropic credit}"
  return 1
}

# The lane's CLI and, for API lanes, its key. Sets API_KEY.
lane_ready() {
  local cli key_ssm
  # A limit an earlier run hit holds for every lane, not only spark.
  limit_held && return 1
  # A key the provider refused holds the lane until it is renewed.
  key_held && return 1
  if [ "$LANE" = agy ]; then
    command -v sandbox-exec > /dev/null || { READY_REASON="sandbox-exec not available"; return 1; }
    "${AGY_RUNAS[@]}" test -x "$AGY_BIN" > /dev/null 2>&1 ||
      { READY_REASON="cannot run $AGY_BIN as $AGY_USER"; return 1; }
    agy_key || return 1
    agy_unlock
    return
  fi
  case "$LANE" in
    codex) cli=codex ;; claude|anthropic) cli=claude ;; copilot) cli=copilot ;;
    openrouter|opencode-go|zen|zai|workersai|spark|free) cli="$(opencode_bin)" ;;
    *) READY_REASON="unknown lane"; return 1 ;;
  esac
  command -v "$cli" > /dev/null || { READY_REASON="$cli not on PATH"; return 1; }
  [ "$LANE" = anthropic ] && ! anthropic_credit_ok && return 1
  case "$LANE" in codex|claude) return 0 ;; esac
  sandbox_ready || { READY_REASON="$SANDBOX_WHY"; return 1; }
  case "$LANE" in zen) return 0 ;; spark|free) spark_ready; return ;; esac
  key_ssm="$(lane_setting KEY_SSM)"
  [ -n "$key_ssm" ] || { READY_REASON="$(lane_var KEY_SSM) is not set"; return 1; }
  API_KEY="$(ssm_value "$key_ssm")"
  [ -n "$API_KEY" ] || { READY_REASON="no key readable at $(lane_var KEY_SSM)"; return 1; }
  lane_extra_ready
}

# The agy lane's "key" is its keychain password (LOOM_AGY_KEY_SSM).
agy_key() {
  local key_ssm
  key_ssm="$(lane_setting KEY_SSM)"
  [ -n "$key_ssm" ] || { READY_REASON="$(lane_var KEY_SSM) is not set"; return 1; }
  API_KEY="$(ssm_value "$key_ssm")"
  [ -n "$API_KEY" ] || { READY_REASON="no keychain password readable at $(lane_var KEY_SSM)"; return 1; }
}

# Unlock the lane user's keychain (it locks again on reboot), only when a
# brief is waiting. `security -i` reads its command from stdin, so the
# password never appears in any process's argv. A keychain that will not
# unlock makes the lane unavailable; its briefs wait.
agy_unlock() {
  compgen -G "$INBOX/*.md" > /dev/null || return 0
  # security -i splits its command line on whitespace and treats quotes and
  # backslashes specially; only a password free of them unlocks as written.
  # printf is a builtin, so the password reaches no process's argv.
  case "$API_KEY" in *[!A-Za-z0-9._~+=/@%:,^-]*)
    READY_REASON="the agy lane keychain password has characters security -i would split; use letters, digits and ._~+=/@%:,^-"; return 1 ;;
  esac
  printf 'unlock-keychain -p %s %s\n' "$API_KEY" "$AGY_KEYCHAIN" | "${AGY_RUNAS[@]}" security -i > /dev/null 2>&1 ||
    { READY_REASON="could not unlock the agy lane keychain $AGY_KEYCHAIN"; return 1; }
}

lane_extra_ready() {
  # Cloudflare Workers AI also needs the account the token belongs to.
  if [ "$LANE" = workersai ]; then
    local account_ssm
    account_ssm="$(lane_setting ACCOUNT_SSM)"
    [ -n "$account_ssm" ] || { READY_REASON="$(lane_var ACCOUNT_SSM) is not set"; return 1; }
    LANE_ACCOUNT="$(ssm_value "$account_ssm")"
    [[ "$LANE_ACCOUNT" =~ ^[A-Za-z0-9]+$ ]] || { READY_REASON="no account id readable at $(lane_var ACCOUNT_SSM)"; return 1; }
  fi
}

# Timeouts must be positive integers: `timeout 0` means no limit at all, and a
# wedged agent would then hold the lane lock forever.
for t in "AGENT_TIMEOUT:$AGENT_TIMEOUT:60" "NET_TIMEOUT:$NET_TIMEOUT:1" "CHECK_TIMEOUT:$CHECK_TIMEOUT:60" \
         "PREPUSH_RETRIES:$PREPUSH_RETRIES:0" "PREPUSH_TIMEOUT:$PREPUSH_TIMEOUT:60"; do
  IFS=: read -r t_name t_val t_min <<< "$t"
  if ! [[ "$t_val" =~ ^[0-9]+$ ]] || [ "$t_val" -lt "$t_min" ]; then
    heartbeat false "LOOM_$t_name must be an integer >= $t_min, got '$t_val'"
    exit 0
  fi
done

# An opencode-go lane runs only bus/tickets.py OPENCODE_GO_MODELS: Go's free
# models and the paid ones allowed at least 10,000 requests per 5 hours.
# Kimi K2.7 Code (1,350) spent the plan's allowance in hours (2026-10-23).
# Fail closed: a lane whose model, or any model of its ladder, is off the
# list (or the list cannot be read) does not start.
OPENCODE_GO_ALLOWED=""
if [ "$LANE" = opencode-go ]; then
  OPENCODE_GO_ALLOWED=" $(PYTHONPATH="$LOOM_ROOT" python3 -B -m bus.tickets opencode-go-models 2>/dev/null | tr '\n' ' ')"
  for candidate in $(lane_setting MODEL) $(lane_setting MODELS); do
    if [[ "$OPENCODE_GO_ALLOWED" != *" $candidate "* ]]; then
      echo "[$(date -u +%FT%TZ)] REFUSED: lane $SLOT model $candidate is not on the OpenCode Go allow-list (bus/tickets.py OPENCODE_GO_MODELS):${OPENCODE_GO_ALLOWED:- unreadable}" >&2
      heartbeat false "model $candidate is not on the OpenCode Go allow-list (bus/tickets.py OPENCODE_GO_MODELS); set $(setting_var "$SLOT" MODEL) to one of:${OPENCODE_GO_ALLOWED:- (list unreadable)}"
      exit 0
    fi
  done
fi

if REST_UNTIL="$(transient_rest_until)"; then
  heartbeat false "limit: $TRANSIENT_STRIKES transient failures in a row; lane rests until $(utc "$REST_UNTIL")" "$REST_UNTIL"
  exit 0
fi
if REST_UNTIL="$(signin_rest_until)"; then
  heartbeat false "limit: sign-in lapsed: sign in the $LANE lane CLI again; lane rests until $(utc "$REST_UNTIL")" "$REST_UNTIL"
  exit 0
fi

# A provider limit hit by an earlier pass holds until its retry time. A
# reason that says "limit:" (which limit, until when) is kept as it was.
LIMIT_UNTIL="$(sed -nE 's/.*"retry_at": *([0-9]+).*/\1/p' "$BEAT" 2>/dev/null | head -1)"
CONFIGURED_MODELS="$(lane_setting MODELS)"
# A Go allowance hold never binds a slot on a free model (limit_group): a
# hold written before the slot moved to one, or by an older runner, is
# dropped. On 2026-10-23 the Beam Free slots kept a monthly hold Kimi had
# taken, until 2026-10-28.
if [ "$LANE" = opencode-go ] && [ -n "$LIMIT_UNTIL" ] && free_model "$(slot_model "$SLOT")" &&
  grep -q "$GO_LIMIT_ERROR" "$BEAT" 2>/dev/null; then
  echo "[$(date -u +%FT%TZ)] lane $SLOT runs free model $(slot_model "$SLOT"), outside the Go allowance: dropping its Go limit hold" >&2
  rm -f "$LOOM_HOME/state/usage/$SLOT.provider-limit.json"
  LIMIT_UNTIL=""
fi
if [ -z "$CONFIGURED_MODELS" ] && [ -n "$LIMIT_UNTIL" ] && [ "$LIMIT_UNTIL" -gt "$(date +%s)" ]; then
  HELD_REASON="$(sed -nE 's/.*"reason": "([^"]*)".*/\1/p' "$BEAT" 2>/dev/null | head -1)"
  case "$HELD_REASON" in limit:*) ;; *) HELD_REASON="provider limit reached; lane waits until $(utc "$LIMIT_UNTIL")" ;; esac
  # The provider's allowance refills while the lane sleeps, so the lane asks
  # it again rather than waiting out the whole window (opencode-go,
  # 2026-10-20: the lane sat idle for days while the provider served it).
  if ! provider_limit_lifted; then
    # The re-check may have found a reset of its own: the beat the group hold
    # wrote says how long the lane waits now.
    NEW_UNTIL="$(sed -nE 's/.*"retry_at": *([0-9]+).*/\1/p' "$BEAT" 2>/dev/null | head -1)"
    [ -n "$NEW_UNTIL" ] && LIMIT_UNTIL="$NEW_UNTIL"
    heartbeat false "$HELD_REASON" "$LIMIT_UNTIL"
    exit 0
  fi
fi

# A model-gone hold that has run out: the lane claims again only when the agent
# CLI lists the model, else it holds another LIMIT_WAIT with no ticket round.
if [ -z "$CONFIGURED_MODELS" ] && [ -n "$LIMIT_UNTIL" ] && [ "$LIMIT_UNTIL" -le "$(date +%s)" ]; then
  GONE_MODEL="$(sed -nE 's/.*"reason": *"limit: model ([A-Za-z0-9._:\/@-]+) not found by the agent CLI.*/\1/p' "$BEAT" 2>/dev/null | head -1)"
  # A hold for a model the lane no longer runs says nothing of the one it runs.
  [ -z "$(lane_setting MODEL)" ] || [ "$GONE_MODEL" = "$(lane_setting MODEL)" ] || GONE_MODEL=""
  if [ -n "$GONE_MODEL" ] && ! model_listed "$GONE_MODEL"; then
    GONE_UNTIL=$(( $(date +%s) + LIMIT_WAIT ))
    heartbeat false "limit: model $GONE_MODEL not found by the agent CLI; lane waits until $(utc "$GONE_UNTIL")" "$GONE_UNTIL"
    exit 0
  fi
fi

AGENT_HOME="$LOOM_HOME/run/$SLOT-home"
CHECK_HOME="$LOOM_HOME/run/$SLOT-check"
API_KEY=""
LANE_ACCOUNT=""
READY_REASON=""
if ! lane_ready; then
  heartbeat false "$READY_REASON"
  # A limited lane, or one whose key expired, still passes over its inbox, to
  # hand every brief on.
  [ -n "$LIMITED" ] || [ -n "$AUTH_HELD" ] || exit 0
elif [ -n "$CONFIGURED_MODELS" ] && ALL_LIMITED_UNTIL="$(all_models_limited_until)"; then
  # Every model of the lane's ladder is limited: the lane is unavailable
  # until the last lifts. Said available, the station claimed a ticket here
  # every few minutes that failed at once on "all models limited" (agy, an
  # account quota on all four of its models, 2026-10-19).
  LIMITED=1
  heartbeat false "limit: all models limited: $(ladder_reason "$CONFIGURED_MODELS")" "$ALL_LIMITED_UNTIL"
elif picks_model && pick_held; then
  # Every model is paused. Said available, the station claimed a ticket here
  # each hour that failed at once (zen, a free limit of 8.6 hours, 2026-11-01).
  LIMITED=1
  LIMIT_WHY="no model can run: $PICK_WHY"
  heartbeat false "limit: $LIMIT_WHY; unavailable until $(utc "$PICK_UNTIL")" "$PICK_UNTIL"
else
  heartbeat true ""
  lane_meter
fi
# Only the sandboxed lanes need the snapshot (codex and claude read the
# operator's own folders), and only when there is work.
if { is_api_lane || [ "$LANE" = agy ]; } && compgen -G "$INBOX/*.md" > /dev/null; then
  refresh_skills
fi

for BRIEF in "$INBOX"/*.md; do
  [ -e "$BRIEF" ] || continue
  drop_build_output
  drop_run_scratch
  AGENT_GH_CALLS=""
  PR_BODY=""
  PLAN_GAPS=""
  DROPPED_SCRATCH_FILES=""
  TICKET="$(basename "$BRIEF" .md)"
  REPO="$(field Repo "$BRIEF")"
  BRANCH="$(field Branch "$BRIEF")"
  # Optional: the branch work starts from and the PR targets. Default is the
  # remote's default branch, which is not always where work lands.
  BASE_BRANCH="$(field Base "$BRIEF" || true)"
  DELIVERABLE="$(field Deliverable "$BRIEF" || true)"
  ISSUE_REF="$(field Issue "$BRIEF" || true)"
  ISSUE_NUMBER="${ISSUE_REF##*#}"
  [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]] || ISSUE_NUMBER="$(printf '%s' "$TICKET" | sed -nE 's/^[^0-9]*([0-9]+).*/\1/p')"
  [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]] && ISSUE_NUMBER="$((10#$ISSUE_NUMBER))"
  LOG="$LOGDIR/$TICKET.log"

  # Ticket names become paths in the sandbox profile and the clone.
  if ! [[ "$TICKET" =~ ^[A-Za-z0-9._-]+$ ]]; then
    park "$BRIEF" "ticket name must be letters, digits, dot, underscore or dash"; continue
  fi
  if [ -n "$LIMITED" ]; then park "$BRIEF" "limit: $LIMIT_WHY" limit; continue; fi
  if [ -n "$AUTH_HELD" ]; then park "$BRIEF" "$AUTH_WHY" auth; continue; fi
  if ! lane_app_token; then park "$BRIEF" "$APP_TOKEN_WHY" transient; continue; fi
  # Briefs are input: the runner only ever pushes a loom-owned branch.
  if ! [[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ && "$BRANCH" =~ ^loom/[A-Za-z0-9._/-]+$ ]]; then
    park "$BRIEF" "brief needs Repo: owner/name and Branch: loom/..."; continue
  fi
  if [ -n "$BASE_BRANCH" ] && ! [[ "$BASE_BRANCH" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ ]]; then
    park "$BRIEF" "brief Base: is not a branch name"; continue
  fi
  if [ -n "$DELIVERABLE" ] && [ "$DELIVERABLE" != answer ] && [ "$DELIVERABLE" != verify ] \
     && [ "$DELIVERABLE" != review ]; then
    park "$BRIEF" "brief Deliverable: must be answer, verify or review"; continue
  fi
  if [ "$DELIVERABLE" = answer ] && ! [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
    park "$BRIEF" "answer brief needs Issue: owner/name#number"; continue
  fi
  if [ "$DELIVERABLE" = review ] && ! [[ "$(field Review-PR "$BRIEF" || true)" =~ ^[0-9]+$ ]]; then
    park "$BRIEF" "review brief needs Review-PR: <number>"; continue
  fi
  # A brief that opens a pull request needs criteria for its body: without
  # them the body's ## Acceptance criteria is empty, the review bot's plan
  # check fails it, and no fix round can change that (they edit code). Like
  # every other brief the runner cannot build, it is parked before the agent
  # runs. Fix briefs edit an open pull request, and answer and verify briefs
  # open none.
  if [ -z "$DELIVERABLE" ] && [ -z "$(field Fix-PR "$BRIEF" || true)" ] &&
     [ -z "$(criteria_section "$BRIEF")" ]; then
    park "$BRIEF" "ticket has no acceptance criteria: add an ## Acceptance criteria section with - [ ] checkboxes; the agent did not run"
    continue
  fi
  BASE_ARGS=()
  [ -n "$BASE_BRANCH" ] && BASE_ARGS=(--branch "$BASE_BRANCH")
  PR_BASE_ARGS=()
  [ -n "$BASE_BRANCH" ] && PR_BASE_ARGS=(--base "$BASE_BRANCH")
  # The model: the brief's Model: line, else the lane default. Passed to the
  # CLI explicitly so the PR names what actually ran.
  PINNED_MODEL="$(field Model "$BRIEF" || true)"
  # LOOM_<LANE>_ONLY_MODEL (codex, agy): the model family a lane runs. A pin to
  # another model the lane's provider serves is dropped, and the lane runs its
  # own model or ladder. The operator's rule (2026-10-21) is codex on
  # gpt-6-luna and agy on gemini-3.8-flash: gpt-6-sol and gemini-4-argon pins
  # spent both weekly limits in a day. A model of another family is still
  # refused below.
  ONLY_MODEL="$(lane_setting ONLY_MODEL)"
  if [ -n "$ONLY_MODEL" ] && [ -n "$PINNED_MODEL" ] && [[ "$PINNED_MODEL" != "$ONLY_MODEL"* ]]; then
    case "$LANE:$PINNED_MODEL" in
      codex:gpt-*|agy:gemini-*|agy:claude-*|agy:gpt-oss-*)
        echo "[$(date -u +%FT%TZ)] $TICKET ignores the brief's Model: $PINNED_MODEL: lane $SLOT runs $ONLY_MODEL models only" >> "$LOG"
        PINNED_MODEL="" ;;
    esac
  fi
  if [ -n "$PINNED_MODEL" ] && picks_model; then
    echo "[$(date -u +%FT%TZ)] $TICKET ignores the brief's Model: $PINNED_MODEL: lane $SLOT picks free models first" >> "$LOG"
    PINNED_MODEL=""
  fi
  MODEL="$PINNED_MODEL"
  if [ -z "$MODEL" ]; then
    MODEL="$(lane_setting MODEL)"
    [ -n "$MODEL" ] || { [ "$LANE" = copilot ] && MODEL="copilot-free"; }
    if [ -n "$CONFIGURED_MODELS" ]; then
      MODEL=""
      for candidate in $CONFIGURED_MODELS; do
        if ! model_limit_until "$candidate" >/dev/null; then MODEL="$candidate"; break; fi
      done
      if [ -z "$MODEL" ]; then
        WHY="$(ladder_reason "$CONFIGURED_MODELS")"
        MAX_UNTIL="$(awk 'BEGIN{m=0} {if ($2>m)m=$2} END{print m}' "$MODEL_LIMIT_FILE" 2>/dev/null)"
        heartbeat false "limit: all models limited: $WHY" "$MAX_UNTIL"
        park "$BRIEF" "limit: all models limited: $WHY" limit; continue
      fi
    fi
  elif [ -n "$CONFIGURED_MODELS" ] && [ -n "$PINNED_MODEL" ]; then
    # Not a lane-wide hold: the lane still serves its other models. The
    # heartbeat's limited_models carries this limit, so the station does not
    # claim the ticket here again before it lifts (write_beat).
    if PINNED_UNTIL="$(model_limit_until "$PINNED_MODEL")"; then
      park "$BRIEF" "limit: requested model $PINNED_MODEL limited until $(utc "$PINNED_UNTIL")" limit
      continue
    fi
  fi
  if ! [[ "$MODEL" =~ ^[A-Za-z0-9._:][A-Za-z0-9._:/@-]*$ ]]; then
    park "$BRIEF" "no usable model '$MODEL': set Model: in the brief or $(lane_var MODEL)"; continue
  fi
  case "$LANE:$MODEL" in
    codex:claude*|codex:opus*|codex:sonnet*|codex:haiku*|codex:fable*)
      park "$BRIEF" "model $MODEL is not a codex model"; continue ;;
    claude:claude*|claude:opus*|claude:sonnet*|claude:haiku*|claude:fable*) ;;
    anthropic:claude*) ;;
    claude:*|anthropic:*) park "$BRIEF" "model $MODEL is not a claude model"; continue ;;
    openrouter:openrouter/*:free) ;;
    openrouter:*) park "$BRIEF" "openrouter lane runs free models only (openrouter/...:free), not $MODEL"; continue ;;
    opencode-go:opencode-go/*)
      # A pin off the allow-list runs the lane's own model, which the start
      # check above passed.
      if [[ "$OPENCODE_GO_ALLOWED" != *" $MODEL "* ]]; then
        echo "[$(date -u +%FT%TZ)] $TICKET REFUSED the brief's Model: $MODEL: not on the OpenCode Go allow-list; lane $SLOT runs $(lane_setting MODEL)" | tee -a "$LOG" >&2
        MODEL="$(lane_setting MODEL)" PINNED_MODEL=""
        [[ "$OPENCODE_GO_ALLOWED" == *" $MODEL "* ]] ||
          { park "$BRIEF" "model $MODEL is not on the OpenCode Go allow-list" transient; continue; }
      fi ;;
    opencode-go:*) park "$BRIEF" "model $MODEL is not an opencode-go model" transient; continue ;;
    zen:opencode/*-free) ;;
    zen:*) park "$BRIEF" "zen lane runs free models only (opencode/...-free), not $MODEL"; continue ;;
    zai:zai/glm-4.7-flash|zai:zai/glm-4.5-flash) ;;
    zai:*) park "$BRIEF" "zai lane runs the free Flash models only (zai/glm-4.7-flash, zai/glm-4.5-flash), not $MODEL"; continue ;;
    workersai:cloudflare-workers-ai/@cf/*) ;;
    copilot:*) ;;
    spark:spark/?*) ;;
    spark:*) park "$BRIEF" "model $MODEL is not a spark model (spark/...)"; continue ;;
    free:free/?*) ;;
    free:*) park "$BRIEF" "model $MODEL is not a free provider model (free/...)"; continue ;;
    agy:gemini-*|agy:claude-*|agy:gpt-oss-*)
      case "$MODEL" in
        *-low|*-medium|*-high) ;;
        *) case "${LOOM_AGY_EFFORT:-high}" in
             low|medium|high) MODEL="$MODEL-${LOOM_AGY_EFFORT:-high}" ;;
             *) park "$BRIEF" "agy effort must be low, medium, or high" transient; continue ;;
           esac ;;
      esac ;;
    agy:*) park "$BRIEF" "model $MODEL is not in the agy catalog (gemini-*, claude-*, gpt-oss-*)" transient; continue ;;
    workersai:*) park "$BRIEF" "workersai lane runs Cloudflare Workers AI models only (cloudflare-workers-ai/@cf/...), not $MODEL"; continue ;;
  esac
  if [ -n "$CONFIGURED_MODELS" ] && [ -z "$PINNED_MODEL" ]; then
    WHY="$(ladder_reason "$CONFIGURED_MODELS")"
    if [ -n "$WHY" ]; then heartbeat true "model $MODEL in use; limited: $WHY"
    else heartbeat true "model $MODEL in use"; fi
  fi
  if [[ "$REPO_OWNERS" != *" ${REPO%%/*} "* ]]; then
    park "$BRIEF" "owner ${REPO%%/*} is not in LOOM_REPO_OWNERS"; continue
  fi
  if ! REPO_PRIVATE="$(timeout "$NET_TIMEOUT" gh api "repos/$REPO" --jq .private)"; then
    echo "$TICKET: gh api repos/$REPO failed; will retry next pass" >&2; continue
  fi
  case "$REPO_PRIVATE" in
    true|false) ;;
    *) echo "$TICKET: unexpected privacy answer '$REPO_PRIVATE' for $REPO; will retry next pass" >&2; continue ;;
  esac
  # Providers that train on prompts take public repos only. Asked before the
  # clone; a failed lookup proves nothing, so the brief waits for the next pass.
  if trains_on_prompts; then
    case "$REPO_PRIVATE" in
      false) ;;
      true) park "$BRIEF" "$REPO is private; lane $LANE with $MODEL trains on prompts and takes public repos only"; continue ;;
      *) echo "$TICKET: unexpected privacy answer '$REPO_PRIVATE' for $REPO; will retry next pass" >&2; continue ;;
    esac
  fi

  FIX_PR="$(field Fix-PR "$BRIEF" || true)"
  REVIEW_PR="$(field Review-PR "$BRIEF" || true)"
  # A review brief (Deliverable: review) takes its pull request's branch the
  # same way a fix brief does: the tree under review is the PR's own head.
  if [ -n "$FIX_PR" ] || [ -n "$REVIEW_PR" ]; then
    if [ -n "$FIX_PR" ] && ! [[ "$FIX_PR" =~ ^[0-9]+$ ]]; then
      park "$BRIEF" "brief Fix-PR: is not a pull request number"; continue
    fi
    if ! fix_pr_open "${FIX_PR:-$REVIEW_PR}"; then
      [ -n "$FIX_REASON" ] && { park "$BRIEF" "$FIX_REASON"; continue; }
      echo "$TICKET: gh pr view failed; will retry next pass" >&2; continue
    fi
    # The PR's own branch, as it is on the remote; nothing is created.
    CLONE_ARGS=(--branch "$BRANCH")
  else
    # gh unreachable or hung says nothing about the branch: leave the brief
    # for the next pass rather than risk a second run.
    if ! EXISTING="$(timeout "$NET_TIMEOUT" gh pr list -R "$REPO" --head "$BRANCH" --state all --json number --jq '.[0].number // empty')"; then
      echo "$TICKET: gh pr list failed; will retry next pass" >&2; continue
    fi
    if [ -n "$EXISTING" ]; then
      echo "$TICKET: $REPO#$EXISTING already exists for $BRANCH; retiring brief"
      mv "$BRIEF" "$INBOX/done/"
      continue
    fi
    CLONE_ARGS=(${BASE_ARGS[@]+"${BASE_ARGS[@]}"})
  fi

  # A provider at its usage limit gets no run: the lane is held and the
  # brief handed on before anything is cloned. A slot that picks its model
  # asks the picker, which probes each model it tries.
  MODEL_PICKED="" MODEL_KIND="" MODEL_NOTE=""
  if picks_model; then
    if ! pick_run_model; then
      # The paid models may still serve build work: only that kind of brief
      # holds the slot.
      if [ -n "$DELIVERABLE" ]; then
        park "$BRIEF" "limit: no free model can run: $PICK_WHY" limit; continue
      fi
      LIMITED=1
      LIMIT_WHY="no model can run: $PICK_WHY"
      heartbeat false "limit: $LIMIT_WHY; unavailable until $(utc "$PICK_UNTIL")" "$PICK_UNTIL"
      park "$BRIEF" "limit: $LIMIT_WHY" limit; continue
    fi
    heartbeat true "model $MODEL in use ($MODEL_KIND)"
  elif provider_at_limit; then
    hold_provider_limit
    park "$BRIEF" "limit: $LIMIT_WHY" limit; continue
  fi

  TREE="$WORKDIR/$TICKET"
  rm -rf "$TREE"
  rm -f "$WORKDIR/$TICKET.described-step" "$WORKDIR/$TICKET.opencode-events" "$WORKDIR/$TICKET.last-message"
  rm -f "$LOGDIR/$TICKET.answered" "$LOGDIR/$TICKET.answered.tmp" "$LOGDIR/$TICKET.pushed"
  echo "[$(date -u +%FT%TZ)] $TICKET start, lane $SLOT" >> "$LOG"
  if ! { timeout "$NET_TIMEOUT" git clone -q --depth 50 ${CLONE_ARGS[@]+"${CLONE_ARGS[@]}"} "${CLONE_BASE}${REPO}.git" "$TREE" &&
         { [ -n "$FIX_PR" ] || [ -n "$REVIEW_PR" ] || git -C "$TREE" checkout -q -b "$BRANCH"; }; } >> "$LOG" 2>&1; then
    park "$BRIEF" "clone or branch failed" transient; continue
  fi
  if ! BASE="$(git -C "$TREE" rev-parse HEAD)" || [ -z "$BASE" ]; then
    park "$BRIEF" "could not read the base commit" transient; continue
  fi
  RECORD="$LOGDIR/$TICKET.validation.jsonl"
  rm -f "$RECORD"
  HAS_CONTRACT="" BASELINE_EXIT="" VALIDATED="" PARK_CLASS=""
  RUN_BRIEF="$BRIEF"
  CONFLICTS=""
  FIX_UPSTREAM=""
  FIX_DEFAULT=""
  if [ -n "$FIX_PR" ]; then
    # BASE is taken first, so a clean merge alone counts as a change and an
    # out-of-date pull request is pushed up to date.
    if ! update_fix_branch; then park "$BRIEF" "$UPDATE_REASON"; continue; fi
    if [ -n "$CONFLICTS" ]; then
      RUN_BRIEF="$WORKDIR/$TICKET.run.md"
      { cat "$BRIEF"
        printf '\n## Merge conflicts\n\nThe runner merged the default branch into this branch and it conflicts in: %s\n' "$(conflict_list)"
        printf 'Resolve every conflict in those files first: keep both sides'"'"' intent, remove every conflict marker, and keep the tests green. Do not run git; the runner completes the merge.\n'
      } > "$RUN_BRIEF"
    fi
  fi
  # A verify or review run changes no code, so there is nothing to validate.
  if [ "$DELIVERABLE" != verify ] && [ "$DELIVERABLE" != review ] && ! prepare_validation >> "$LOG" 2>&1; then
    park "$BRIEF" "setup failed: $SETUP_REASON; the agent did not run" "$PARK_CLASS"; continue
  fi
  resume_earlier_attempt
  LOG_START="$(wc -c < "$LOG" | tr -d ' ')"

  # The agent runs as a background job of a job-control subshell, which puts
  # it in its own process group (id = its pid) and records that id. If this
  # runner is killed (a reinstall's bootout), the agent survives in its group,
  # and the next pass's stale-lock recovery above kills exactly that group.
  if ! launch_agent "$RUN_BRIEF"; then park "$BRIEF" "$LAUNCH_WHY" transient; continue; fi
  # A spark run that ended because the box went hot is a limit, not a failure.
  if [ "$STATUS" -ne 0 ] && { [ "$LANE" = spark ] || [ "$LANE" = free ]; } && { spark_probe; [ $? -eq 1 ]; }; then
    mark_limit
    park "$BRIEF" "limit: $LIMIT_WHY (agent exited $STATUS)" limit; continue
  fi
  # A run that timed out may have been retrying a usage limit all along.
  if [ -z "$MODEL_PICKED" ] && { [ "$STATUS" -eq 124 ] || [ -f "$LOCK/watchdog-limit" ]; } && provider_at_limit; then
    hold_provider_limit
    park "$BRIEF" "limit: $LIMIT_WHY (agent exited $STATUS)" limit; continue
  fi
  if ! TREE_STATUS="$(safe_git status --porcelain 2>> "$LOG")" ||
     ! TREE_HEAD="$(safe_git rev-parse HEAD 2>> "$LOG")"; then
    park "$BRIEF" "could not read the agent's git state" transient; continue
  fi
  EDITED=""
  { [ -n "$TREE_STATUS" ] || [ "$TREE_HEAD" != "$BASE" ]; } && EDITED=edited
  # Only a run that failed or changed nothing is classed: a finished run's
  # summary may well mention rate limits.
  FAIL_CLASS=work
  # The whole run output (from LOG_START), for the limit and credit patterns;
  # the short tail (its last five non-blank lines), kept for the reason shown
  # to people and for the auth/transient patterns (loom#934).
  # The limit patterns read only what the provider said (provider_text).
  RUN_OUTPUT="$(tail -c +$((LOG_START + 1)) "$LOG" | grep -v '^[[:space:]]*$' | provider_text)"
  FAILURE_OUT="$(printf '%s\n' "$RUN_OUTPUT" | tail -n 5)"
  if [ "$STATUS" -ne 0 ] || [ -z "$EDITED" ]; then
    classify_failure "$STATUS" "$RUN_OUTPUT" "$EDITED" "$FAILURE_OUT"
  fi
  [ -f "$LOCK/watchdog-limit" ] && FAIL_CLASS=limit
  # What ran and how it ended, so a green run that did nothing shows.
  if [ -n "$MODEL_PICKED" ]; then
    echo "[$(date -u +%FT%TZ)] $TICKET model report: $MODEL ($MODEL_KIND), fallback: $MODEL_NOTE; exit $STATUS, ${EDITED:-no edits}, class $FAIL_CLASS" >> "$LOG"
  fi
  # A picked model that failed is paused for every slot. With nothing to keep,
  # the brief stays in the inbox and the next pass picks another model.
  ROTATIONS_FILE="$LOGDIR/$TICKET.rotations"
  if [ -n "$MODEL_PICKED" ] && { [ "$FAIL_CLASS" = limit ] || [ "$FAIL_CLASS" = transient ] || [ "$STATUS" -eq 124 ]; }; then
    MODEL_PAUSED="$(printf '%s\n' "$RUN_OUTPUT" | tail -n 40 | model_pick failed "$MODEL" 2>> "$LOG")" || MODEL_PAUSED="$MODEL could not be paused"
    MODEL_PAUSED="$(printf '%s' "$MODEL_PAUSED" | tr -cd '[:alnum:] ._:;()/-')"
    echo "[$(date -u +%FT%TZ)] $TICKET $MODEL_PAUSED" >> "$LOG"
    ROTATIONS="$(cat "$ROTATIONS_FILE" 2>/dev/null)"
    [[ "$ROTATIONS" =~ ^[0-9]+$ ]] || ROTATIONS=0
    if [ -z "$EDITED" ] && [ "$ROTATIONS" -lt "$MODEL_ROTATIONS" ]; then
      printf '%s\n' "$((ROTATIONS + 1))" > "$ROTATIONS_FILE"
      rm -f "$LOCK/watchdog-limit"
      echo "[$(date -u +%FT%TZ)] $TICKET rotation $((ROTATIONS + 1)) of $MODEL_ROTATIONS: the next pass picks another model" >> "$LOG"
      heartbeat true "$MODEL_PAUSED"
      break
    fi
  fi
  rm -f "$ROTATIONS_FILE"
  # A timeout after editing keeps its edits whatever the failure is classed as,
  # and so does a run cut short by a limit, an expired key or a transient error
  # (a free pool runs dry 20 minutes in): every park below names the branch.
  # A work failure keeps nothing, so its next rung starts from the base.
  TIMEDOUT_NOTE=""
  if [ -n "$EDITED" ] && { [ "$STATUS" -eq 124 ] ||
       { [ "$STATUS" -ne 0 ] && [ "$FAIL_CLASS" != work ]; }; }; then
    TIMEDOUT_NOTE="; $(preserve_timedout)"
  fi
  if [ "$FAIL_CLASS" != transient ] || [ -n "$MODEL_PICKED" ]; then
    transient_clear
  elif REST_UNTIL="$(transient_strike)"; then
    heartbeat false "limit: $TRANSIENT_STRIKES transient failures in a row; lane rests until $(utc "$REST_UNTIL")" "$REST_UNTIL"
    park "$BRIEF" "agent exited $STATUS$TIMEDOUT_NOTE" transient
    break
  fi
  if [ "$FAIL_CLASS" = limit ]; then
    # Unavailable before the brief is parked, so the station never sends
    # this lane the retry. The rest of the inbox waits for the reset.
    LIMIT_REASON="provider limit reached"
    [[ "$RUN_OUTPUT" =~ $MODEL_GONE_RE ]] && LIMIT_REASON="model $MODEL not found by the agent CLI"
    if [ -n "$MODEL_PICKED" ]; then
      # The model is paused; the slot's other models still run.
      LIMIT_REASON="$MODEL_PAUSED"
      heartbeat true "$MODEL_PAUSED"
    elif [ -n "$OUT_OF_CREDIT" ]; then
      LIMIT_REASON="out of credit"
      hold_shared_credit "$RETRY_AT"
      heartbeat false "limit: out of credit; lane waits until $(utc "$RETRY_AT")" "$RETRY_AT"
    elif [ -n "$CONFIGURED_MODELS" ] &&
         [[ "$(printf '%s' "$RUN_OUTPUT" | tr '[:upper:]' '[:lower:]')" =~ $ACCOUNT_QUOTA_RE ]]; then
      LIMIT_REASON="account quota reached"
      for candidate in $CONFIGURED_MODELS $MODEL; do
        record_model_limit "$candidate" "$RETRY_AT" "$LIMIT_REASON"
      done
      heartbeat false "limit: $LIMIT_REASON; lane waits until $(utc "$RETRY_AT")" "$RETRY_AT"
    elif [ -n "$CONFIGURED_MODELS" ]; then
      record_model_limit "$MODEL" "$RETRY_AT" "$LIMIT_REASON"
      WHY="$(ladder_reason "$CONFIGURED_MODELS")"
      MAX_UNTIL="$(awk 'BEGIN{m=0} {if ($2>m)m=$2} END{print m}' "$MODEL_LIMIT_FILE")"
      AVAILABLE_MODEL=""
      for candidate in $CONFIGURED_MODELS; do
        if ! model_limit_until "$candidate" >/dev/null; then AVAILABLE_MODEL="$candidate"; break; fi
      done
      if [ -n "$AVAILABLE_MODEL" ]; then
        heartbeat true "model $AVAILABLE_MODEL available; limited: $WHY"
      else
        heartbeat false "limit: all models limited: $WHY" "$MAX_UNTIL"
      fi
    else
      heartbeat false "limit: $LIMIT_REASON; lane waits until $(utc "$RETRY_AT")" "$RETRY_AT"
    fi
    park "$BRIEF" "agent exited $STATUS: $LIMIT_REASON; lane $SLOT waits until $(utc "$RETRY_AT")$TIMEDOUT_NOTE" limit
    break
  fi
  if [ "$FAIL_CLASS" = auth ]; then
    # The provider refused the key, not the lane's work, so the brief is handed
    # on rather than spent and the rest of the inbox waits for the operator.
    if printf '%s\n' "$FAILURE_OUT" | grep -qiE "$SIGNIN_RE"; then
      # The CLI's own sign-in lapsed: no key parameter to renew or wait on,
      # even when the lane names one (agy names the lane user's password).
      AUTH_WHY="sign-in lapsed: sign in the $LANE lane CLI again"
      REST_UNTIL=$(( $(date +%s) + SIGNIN_WAIT ))
      printf '%s\n' "$REST_UNTIL" > "$SIGNIN_FILE"
      heartbeat false "limit: $AUTH_WHY; lane rests until $(utc "$REST_UNTIL")" "$REST_UNTIL"
      ask_sign_in "$AUTH_WHY"
      park "$BRIEF" "agent exited $STATUS: $AUTH_WHY$TIMEDOUT_NOTE" auth
      break
    fi
    AUTH_WHY="$(key_renew_reason)"
    if key_renewable; then
      # Every slot that bills that key goes unavailable until the value at the
      # parameter changes. A lane that signs in with the CLI's own file has no
      # such value to wait for, and is left to fail again on its next pass.
      AUTH_HELD=1
      hold_expired_key "$AUTH_WHY"
    fi
    ask_renew_key "$AUTH_WHY"
    park "$BRIEF" "agent exited $STATUS: $AUTH_WHY$TIMEDOUT_NOTE" auth
    break
  fi
  if [ "$STATUS" -ne 0 ]; then park "$BRIEF" "agent exited $STATUS$TIMEDOUT_NOTE" "$FAIL_CLASS"; continue; fi
  if [ "$DELIVERABLE" = verify ]; then
    # The station reads the result from the log folder and applies it; the
    # runner moves no label and opens no pull request.
    RESULT="$TREE/verify.json"
    # Only a file the agent made counts: one the repo tracks was there before.
    if [ ! -f "$RESULT" ] || [ -L "$RESULT" ] || [ "$(wc -c < "$RESULT")" -gt 65536 ] ||
       [ -n "$(safe_git ls-files ':(icase)verify.json')" ]; then
      park "$BRIEF" "no verify.json written"; continue
    fi
    cp "$RESULT" "$LOGDIR/$TICKET.verify.json"
    printf '%s\n' "$MODEL" > "$LOGDIR/$TICKET.verify.model"
    echo "[$(date -u +%FT%TZ)] $TICKET done, verify result recorded" >> "$LOG"
    mv "$BRIEF" "$INBOX/done/"
    continue
  fi
  if [ "$DELIVERABLE" = review ]; then
    # The station reads the result from the log folder and posts the verdict;
    # the runner moves no label and opens no pull request.
    RESULT="$TREE/review.json"
    # Only a file the agent made counts: one the repo tracks was there before.
    if [ ! -f "$RESULT" ] || [ -L "$RESULT" ] || [ "$(wc -c < "$RESULT")" -gt 65536 ] ||
       [ -n "$(safe_git ls-files ':(icase)review.json')" ]; then
      park "$BRIEF" "no review.json written"; continue
    fi
    cp "$RESULT" "$LOGDIR/$TICKET.review.json"
    printf '%s\n' "$MODEL" > "$LOGDIR/$TICKET.review.model"
    echo "[$(date -u +%FT%TZ)] $TICKET done, review result recorded" >> "$LOG"
    mv "$BRIEF" "$INBOX/done/"
    continue
  fi
  if [ "$DELIVERABLE" = answer ]; then
    ANSWER="$TREE/.loom/answer.md"
    if [ ! -d "$TREE/.loom" ] || [ -L "$TREE/.loom" ] || [ ! -f "$ANSWER" ] || [ -L "$ANSWER" ]; then
      park "$BRIEF" "no answer written"; continue
    fi
    if ! ANSWER_TEXT="$(checked_text "$ANSWER" 2> "$LOGDIR/$TICKET.answer-error")"; then
      REASON="$(cat "$LOGDIR/$TICKET.answer-error" 2>/dev/null)"
      [ -n "$REASON" ] || REASON="answer could not be read"
      park "$BRIEF" "answer refused: $REASON"; continue
    fi
    rm -f "$LOGDIR/$TICKET.answer-error"
    if ! timeout "$NET_TIMEOUT" gh issue comment "$ISSUE_NUMBER" -R "$REPO" --body "$ANSWER_TEXT" >> "$LOG" 2>&1; then
      park "$BRIEF" "answer comment failed" transient; continue
    fi
    if ! mark_answered; then
      park "$BRIEF" "answer posted but ticket label update failed" transient; continue
    fi
    echo "[$(date -u +%FT%TZ)] $TICKET done, answer posted" >> "$LOG"
    mv "$BRIEF" "$INBOX/done/"
    continue
  fi
  TITLE="$(sed -n 's/^# T[0-9]*: //p' "$BRIEF" | head -1)"
  [ -n "$FIX_PR" ] && TITLE="${TITLE:-$TICKET}: address the findings on #$FIX_PR"
  keep_answers
  if [ -n "$CONFLICTS" ]; then
    LEFT="$(markers_left)"
    if [ -n "$LEFT" ]; then park "$BRIEF" "conflict markers left in $LEFT"; continue; fi
  fi
  # A sandboxed agent cannot write .git, so it leaves its work in the tree.
  REPLIES_FILE=""
  if [ -n "$FIX_PR" ]; then capture_replies; else rm -f "$TREE/.loom/replies.json"; fi
  # The Intent summary is runner metadata, never part of the change: keep it
  # out of the commit so it cannot skew the change class.
  rm -f "$WORKDIR/$TICKET.summary.md"
  if [ -f "$TREE/$SUMMARY_FILE" ] && [ ! -L "$TREE/$SUMMARY_FILE" ]; then
    cp "$TREE/$SUMMARY_FILE" "$WORKDIR/$TICKET.summary.md" 2>/dev/null || true
    rm -f "$TREE/$SUMMARY_FILE"
  else
    rm -f "$TREE/$SUMMARY_FILE"
  fi
  SUBMODULE_EDITS="$(submodule_edits)"
  if [ -n "$SUBMODULE_EDITS" ]; then
    park "$BRIEF" "the change edits files inside submodule $SUBMODULE_EDITS, which a push of this repo does not carry; that repo needs the change first. nothing pushed"; continue
  fi
  # During a merge the branch is already checked out, and the commit
  # completes the merge.
  if [ -n "$(safe_git status --porcelain)" ] || [ -n "$CONFLICTS" ]; then
    if ! { { [ -n "$CONFLICTS" ] || safe_git checkout -q "$BRANCH"; } && safe_git add -A &&
           safe_git commit -qm "${TITLE:-$TICKET}"; } >> "$LOG" 2>&1; then
      park "$BRIEF" "could not commit the agent's changes" transient; continue
    fi
  fi
  # A non-sandboxed agent may commit while HEAD is detached. Adopt that clean
  # commit onto the lane branch before validation and push.
  if ! TREE_STATUS="$(safe_git status --porcelain 2>> "$LOG")" ||
     ! TREE_HEAD="$(safe_git rev-parse HEAD 2>> "$LOG")" ||
     ! BRANCH_HEAD="$(safe_git rev-parse "$BRANCH" 2>> "$LOG")"; then
    park "$BRIEF" "could not read the committed git state" transient; continue
  fi
  if [ -z "$TREE_STATUS" ] && [ "$TREE_HEAD" != "$BRANCH_HEAD" ] && [ "$TREE_HEAD" != "$BASE" ]; then
    if ! safe_git update-ref "refs/heads/$BRANCH" "$TREE_HEAD" >> "$LOG" 2>&1; then
      park "$BRIEF" "could not record the agent's commit on $BRANCH" transient; continue
    fi
  fi
  if ! safe_git checkout -q "$BRANCH" >> "$LOG" 2>&1; then
    park "$BRIEF" "could not check out $BRANCH after the agent run" transient; continue
  fi
  COMMITTED=0
  # Before validation, push and PR, drop sandbox probes and qualifying
  # out-of-path scratch files from the agent's commits and name each one
  # in the run log and pull request text.
  if ! drop_agent_files >> "$LOG" 2>&1; then
    park "$BRIEF" "could not drop agent scratch files" transient; continue
  fi
  if ! BRANCH_HEAD="$(safe_git rev-parse "$BRANCH" 2>> "$LOG")" || [ -z "$BRANCH_HEAD" ]; then
    park "$BRIEF" "could not read the lane branch after validation" transient; continue
  fi
  if [ "$BRANCH_HEAD" != "$BASE" ]; then
    COMMITTED=1
  fi
  # The checks that refuse a push run first, while the agent can still act on
  # them in this run and before the long validation.
  if [ "$COMMITTED" -eq 1 ] && ! prepush_gate; then continue; fi
  # A commit that adds files outside the brief is refused before validation
  # and push: nothing is pushed, and the reason names every stray file. A fix
  # round removes them instead (drop_stray_files) and checks again, so it
  # pushes its work rather than retrying the same refusal (loom#1476).
  if [ "$COMMITTED" -eq 1 ]; then
    if ! check_scope "$BRIEF" 2>> "$LOG"; then
      park "$BRIEF" "could not check the committed files against the brief" transient; continue
    fi
    if [ -n "$STRAY_FILES" ] && [ -n "$FIX_PR" ]; then
      if ! drop_stray_files >> "$LOG" 2>&1; then
        park "$BRIEF" "commit refused: it adds files outside the brief's paths that could not be removed: $(stray_list); nothing pushed"; continue
      fi
      if ! check_scope "$BRIEF" 2>> "$LOG"; then
        park "$BRIEF" "could not check the committed files against the brief" transient; continue
      fi
    fi
    if [ -n "$STRAY_FILES" ]; then
      park "$BRIEF" "commit refused: it adds files outside the brief's paths: $(stray_list); nothing pushed"; continue
    fi
  fi
  # A contract the lane adds or edits must read and pass under itself before
  # anything is pushed (loom#1290).
  if [ "$COMMITTED" -eq 1 ]; then
    check_contract_change; CONTRACT_RC=$?
    case "$CONTRACT_RC" in
      0) ;;
      1) park "$BRIEF" "commit refused: $CONTRACT_REFUSAL; nothing pushed"; continue ;;
      *) park "$BRIEF" "could not check the committed validation contract: $CONTRACT_REFUSAL" transient; continue ;;
    esac
  fi
  # An agent's exit code is not evidence: only a passing check on the
  # committed head hands the work on.
  if [ -n "$HAS_CONTRACT" ] && ! final_validation; then
    HANDOFF="no pull request opened"
    [ -n "$FIX_PR" ] && HANDOFF="nothing pushed to #$FIX_PR"
    park "$BRIEF" "$VALIDATION_REASON; $(preserve_unvalidated); $HANDOFF" "$PARK_CLASS"; continue
  fi
  if [ "$REPO_PRIVATE" = false ]; then
    keep_scanner_current || true
    if [ ! -f "$LEAK_SCANNER" ]; then
      # A scanner that still cannot be had is the machine's fault, not the
      # ticket's: a factory incident holds the ticket and files one issue,
      # instead of a transient failure the station re-briefs endlessly.
      SCANNER_REASON=""
      note_factory_incident scanner "leak scanner not found at $LEAK_SCANNER" SCANNER_REASON
      park "$BRIEF" "public repo leak scan unavailable: scanner not found; nothing pushed$SCANNER_REASON" "$PARK_CLASS"; continue
    fi
    SCAN_RANGE="$(scan_range)"
    # The repo's own issue refs (OWNER/NAME#N and NAME#N in code or tests)
    # name nothing private; the scanner exempts exactly this repo for them.
    # Exit 2 is no scan (the deny-list could not be read), not a hit in the
    # lane's work: transient, so another lane may take the ticket.
    LEAK_RC=0
    LEAK_OUT="$(cd "$TREE" && leak_scan --diff "$SCAN_RANGE" --allow-repo-ref "$REPO" 2>&1)" || LEAK_RC=$?
    if [ "$LEAK_RC" -eq 2 ]; then
      park "$BRIEF" "public repo leak scan could not run; nothing pushed: $LEAK_OUT" transient
      continue
    elif [ "$LEAK_RC" -ne 0 ]; then
      park "$BRIEF" "public repo leak scan failed; nothing pushed: $LEAK_OUT"
      continue
    fi
    printf '%s\n' "$LEAK_OUT" >> "$LOG"
    if [ -z "$FIX_PR" ]; then
      INTENT_CLASS=""
      INTENT_TEXT=""
      prepare_intent "$BRIEF" >> "$LOG" 2>&1
      if [ -n "$DROPPED_FILES" ]; then
        INTENT_TEXT="${INTENT_TEXT}
- Dropped sandbox probe files: ${DROPPED_FILES//$'\n'/, }"
      fi
      if [ -n "$DROPPED_SCRATCH_FILES" ]; then
        INTENT_TEXT="${INTENT_TEXT}
- Dropped scratch files: ${DROPPED_SCRATCH_FILES//$'\n'/, }"
      fi
      compose_pr_body
      PR_BODY="$(printf '%s' "$PR_BODY" | PYTHONPATH="$REPO_ROOT" python3 -B -c \
        'import sys; from bus.factory import SCAN_REF_RE; owner, name = sys.argv[1].lower().split("/", 1); f = lambda m: m.group(0) if m.group(2).lower() == name and (m.group(1) or owner).lower() == owner else "issue %s of another repo" % m.group(3); print("\n".join(SCAN_REF_RE.sub(f, x) for x in sys.stdin.read().splitlines()))' "$REPO")" || {
        park "$BRIEF" "could not prepare public repo PR text; nothing pushed" transient; continue
      }
      BODY_FILE="$(mktemp "$LOOM_HOME/run/$SLOT-leak-body.XXXXXX")"
      # The body cites its own repo's tickets ("Refs OWNER/NAME#N", and NAME#N
      # from the ticket text), which name nothing private. An OWNER/NAME
      # --allow-repo-ref exempts exactly those two forms, as for the diff:
      # rewriting only OWNER/NAME#N to #N left NAME#N flagged (2026-10-21).
      printf '%s\n' "$PR_BODY" > "$BODY_FILE"
      if ! LEAK_OUT="$(leak_scan --text-file "$BODY_FILE" --allow-repo-ref "$REPO" 2>&1)"; then
        rm -f "$BODY_FILE"
        park "$BRIEF" "public repo PR body leak scan failed; nothing pushed: $LEAK_OUT"
        continue
      fi
      rm -f "$BODY_FILE"
      printf '%s\n' "$LEAK_OUT" >> "$LOG"
    fi
  fi
  # Node labels, SSM parameters and hosts a manifest change names must exist
  # live before anything is pushed (bus/live_cluster.py, loom#1832). It runs
  # after the leak scan: it looks up names the agent wrote, outside its
  # sandbox. A miss is the lane's work to fix; a check that cannot run is a
  # note, not a stop.
  LIVE_NOTES=""
  if [ "$COMMITTED" -eq 1 ]; then live_cluster_check || continue; fi
  # A PR closed or merged during the run gets nothing pushed: a push would
  # bring its deleted branch back.
  if [ -n "$FIX_PR" ] && ! fix_pr_open; then
    park "$BRIEF" "${FIX_REASON:-could not check pull request #$FIX_PR before pushing}"; continue
  fi
  # A fix round that commits nothing leaves the checks it was asked to fix
  # failing on the very head CI failed them on: it is a failed round, not
  # "done", however it answered the threads or repaired the text. On
  # 2026-10-26 four such rounds of one ticket each reported done in 10 to 27
  # minutes with both checks still red (bus/converge.py).
  if [ -n "$FIX_PR" ]; then
    FIX_CHECKS="$(fix_code_checks "$BRIEF")"
    if [ "$COMMITTED" -eq 0 ] && [ -n "$FIX_CHECKS" ] && [ "$(fix_head "$BRIEF")" = "$BASE" ]; then
      park "$BRIEF" "fix round made no commit, so the checks it was asked to fix still fail on ${BASE:0:12}, the head CI failed them on: $FIX_CHECKS; nothing pushed"
      continue
    fi
    [ -z "$(fix_checks "$BRIEF")" ] || echo "[$(date -u +%FT%TZ)] $TICKET: $(fix_contract_line "$BRIEF")" >> "$LOG"
  fi
  # For fix-mode briefs, reconcile generated PR sections with the approved
  # ticket revision. A metadata-only repair is a valid outcome.
  METADATA_CHANGED=0
  REPAIR_RC=""
  if [ -n "$FIX_PR" ]; then
    repair_pr_text >> "$LOG" 2>&1
    REPAIR_RC=$?
    case "$REPAIR_RC" in
      0) METADATA_CHANGED=1 ;;
      1) ;; # text is current
      2)
        # Refused text is parked only when there is no code commit or reply to post.
        if [ "$COMMITTED" -eq 0 ] && [ -z "$REPLIES_FILE" ]; then
          park "$BRIEF" "PR text repair refused on #$FIX_PR"
          continue
        fi ;;
      3)
        # A helper error is parked only when there is no code commit to push.
        if [ "$COMMITTED" -eq 0 ] && [ -z "$REPLIES_FILE" ]; then
          park "$BRIEF" "PR text repair failed on #$FIX_PR"
          continue
        fi ;;
      *)
        if [ "$COMMITTED" -eq 0 ] && [ -z "$REPLIES_FILE" ]; then
          park "$BRIEF" "PR text repair returned $REPAIR_RC on #$FIX_PR"
          continue
        fi ;;
    esac
  fi
  if [ "$COMMITTED" -eq 0 ] && [ "$METADATA_CHANGED" -eq 0 ] && [ -z "$REPLIES_FILE" ]; then
    if [ -n "$FIX_PR" ]; then
      park "$BRIEF" "agent made no commit and PR text is current on $BRANCH"; continue
    fi
    # A correct "nothing to change" is an answer, not a failure: on
    # 2026-10-13 five settle-findings tickets failed 4 to 6 times each, every
    # model rightly finding their one finding no longer held.
    if ! no_change_reason; then
      [ -z "$NO_CHANGE_WHY" ] || { park "$BRIEF" "agent made no commit and its reason was refused: $NO_CHANGE_WHY"; continue; }
      if [ -f "$WORKDIR/$TICKET.described-step" ]; then
        park "$BRIEF" "ended on a described step without taking it"; continue
      fi
      final_said
      if [ -n "$FINAL_SAID" ]; then
        park "$BRIEF" "agent made no commit; it said: $FINAL_SAID (on $BRANCH)"; continue
      fi
      if [ -n "$FINAL_SAID_DUMP" ]; then
        park "$BRIEF" "$NO_REASON: the agent ended without explaining why, on a tool's output; see the run log (on $BRANCH)"; continue
      fi
      park "$BRIEF" "$NO_REASON on $BRANCH"; continue
    fi
    if [[ ! "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
      park "$BRIEF" "agent made no commit and its reason has no ticket number to go on"; continue
    fi
    if ! WHY="$(post_no_change)"; then
      rm -f "$LOGDIR/$TICKET.answered.tmp"; park "$BRIEF" "$WHY" transient; continue
    fi
    # The station reads this marker, not the log, which carries agent output.
    mv "$LOGDIR/$TICKET.answered.tmp" "$LOGDIR/$TICKET.answered"
    echo "[$(date -u +%FT%TZ)] $TICKET done, answered: nothing to change" >> "$LOG"
    printf '[%s] %s agent gh calls: %s\n' "$(date -u +%FT%TZ)" "$TICKET" "$AGENT_GH_CALLS" >> "$LOG"
    mv "$BRIEF" "$INBOX/done/"
    continue
  fi
  # Never forced: a branch that moved on the remote refuses the push. A fix
  # brief's branch moved by someone else during the run (another lane's
  # round, a person's commit) is not this run's failure: it is parked as
  # "branch moved", and the station hands the new head back to the factory.
  if [ "$COMMITTED" -eq 1 ]; then
    if ! PUSH_OUT="$(timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false push -q -u origin "$BRANCH" 2>&1)"; then
      printf '%s\n' "$PUSH_OUT" >> "$LOG"
      if [ -n "$FIX_PR" ] && printf '%s\n' "$PUSH_OUT" | grep -qE '\[rejected\].*\((fetch first|non-fast-forward)\)'; then
        NOW_HEAD="$(timeout "$NET_TIMEOUT" git -C "$TREE" ls-remote origin "refs/heads/$BRANCH" 2>> "$LOG" | cut -f1)"
        if [ -n "$NOW_HEAD" ] && [ "$NOW_HEAD" != "$BASE" ]; then
          park "$BRIEF" "branch moved: $BRANCH is at ${NOW_HEAD:0:12} on the remote, not ${BASE:0:12} as when the run started"; continue
        fi
      fi
      # GitHub refuses an App's push that touches .github/workflows unless
      # the App holds the workflows permission. No lane or model can change
      # that: retried as [transient], five runs climbed to claude on it
      # (2026-10-21). The ticket keeps its rung until the incident closes.
      # shellcheck disable=SC2016 # GitHub's literal backquotes
      if REFUSED="$(printf '%s\n' "$PUSH_OUT" | grep -m1 'without `workflows` permission')"; then
        PUSH_REASON="push refused: the GitHub App has no workflows permission"
        # The agent names the workflow file, so only a path-safe, capped
        # copy of it reaches the public incident, never a mention or Markdown.
        REFUSED="$(printf '%s' "$REFUSED" | sed -n 's/.*workflow `\([^`]*\)`.*/\1/p' | tr -cd 'A-Za-z0-9._/-' | cut -c1-120)"
        note_factory_incident workflows-permission "The push of $BRANCH was refused: it changes \`${REFUSED:-a workflow file}\`, and the lanes' GitHub App has no workflows permission.

Grant the lanes' GitHub App the workflows permission (read and write), or land the workflow change from an operator session, then close this issue. Its held tickets return to the queue when it closes." PUSH_REASON
        park "$BRIEF" "$PUSH_REASON" "$PARK_CLASS"; continue
      fi
      park "$BRIEF" "push failed" transient; continue
    fi
    [ -z "$PUSH_OUT" ] || printf '%s\n' "$PUSH_OUT" >> "$LOG"
    # The station reads this marker, not the log, to say the round pushed.
    : > "$LOGDIR/$TICKET.pushed"
  fi
  if [ -n "$FIX_PR" ]; then
    UNANSWERED="$(unanswered_threads)"
    post_replies
    ITEMS="$(fix_items "$BRIEF")"
    if [ "$COMMITTED" -eq 1 ]; then
      # 2026-10-20: "pushed fixes for: the findings on the ticket" told the
      # reader nothing; the comment says what went up and what happens next.
      PUSHED="$(safe_git rev-list --count "$BASE..HEAD" 2>> "$LOG")"
      CHECKED="CI and review run again on the new head; the factory lands the pull request once they pass."
      [ -n "$VALIDATED" ] && CHECKED="The repo's check passed at ${VALIDATED##* }. $CHECKED"
      COMMENT="Lane $SLOT ($MODEL) pushed ${PUSHED:-the} commit$([ "$PUSHED" = 1 ] || echo s) for: ${ITEMS:-the fix round on the ticket}

$(pushed_commits)

$CHECKED"
    elif [ "$METADATA_CHANGED" -eq 0 ]; then
      COMMENT="Lane $SLOT answered the review threads for: ${ITEMS:-the findings on the ticket}"
    else
      COMMENT="Lane $SLOT repaired the PR text for: ${ITEMS:-the findings on the ticket}"
    fi
    if [ -n "$LIVE_NOTES" ]; then
      COMMENT="$COMMENT

$LIVE_NOTES"
    fi
    if [ -n "$DROPPED_SCRATCH_FILES" ]; then
      COMMENT="$COMMENT

Dropped scratch files: ${DROPPED_SCRATCH_FILES//$'\n'/, }"
    fi
    if [ -n "$UNANSWERED" ]; then
      echo "[$(date -u +%FT%TZ)] $TICKET: review threads left without a reply: $UNANSWERED" >> "$LOG"
      COMMENT="$COMMENT

Review threads this round left without a reply: $UNANSWERED"
    fi
    # One comment per pull request, edited each round; the push and the body
    # edit are already on the pull request's timeline.
    if ! printf '%s' "$COMMENT" | upsert_sticky_comment "$REPO" "$FIX_PR" "$ROUND_MARKER" rounds; then
      echo "[$(date -u +%FT%TZ)] $TICKET: fixes are pushed but the comment on #$FIX_PR failed" >> "$LOG"
    fi
    if [ "$COMMITTED" -eq 1 ]; then
      echo "[$(date -u +%FT%TZ)] $TICKET done, fixes pushed to #$FIX_PR" >> "$LOG"
    elif [ "$METADATA_CHANGED" -eq 0 ]; then
      echo "[$(date -u +%FT%TZ)] $TICKET done, review replies posted on #$FIX_PR" >> "$LOG"
    else
      echo "[$(date -u +%FT%TZ)] $TICKET done, PR text repaired on #$FIX_PR" >> "$LOG"
    fi
    mv "$BRIEF" "$INBOX/done/"
    continue
  fi
  if [ "$REPO_PRIVATE" = true ]; then
    INTENT_CLASS=""
    INTENT_TEXT=""
    prepare_intent "$BRIEF" >> "$LOG" 2>&1
    if [ -n "$DROPPED_SCRATCH_FILES" ]; then
      INTENT_TEXT="${INTENT_TEXT}
- Dropped scratch files: ${DROPPED_SCRATCH_FILES//$'\n'/, }"
    fi
  fi
  # The class label goes on after the PR exists: a repo without the label
  # must never cost a finished run its PR (2026-10-14, ten runs lost theirs).
  # Briefs are checked when filed, so a failing body comes from a brief filed
  # before that check, edited by hand, or read differently by the shell and
  # Python readers: compose_pr_body rebuilds it, and marks the pull request
  # when the brief cannot fill it.
  [ -n "${PR_BODY:-}" ] || compose_pr_body
  [ -z "$LIVE_NOTES" ] || PR_BODY="$PR_BODY"$'\n\n'"$LIVE_NOTES"
  [ -z "$PLAN_GAPS" ] || PR_BODY="$PR_BODY"$'\n\n'"$PLAN_HOLD_MARKER"
  if ! PR_URL="$(timeout "$NET_TIMEOUT" gh pr create -R "$REPO" ${PR_BASE_ARGS[@]+"${PR_BASE_ARGS[@]}"} --head "$BRANCH" --title "${TITLE:-$TICKET}" \
       --body "$PR_BODY" 2>> "$LOG")"; then
    park "$BRIEF" "pr create failed; branch $BRANCH is pushed" transient; continue
  fi
  echo "$PR_URL" >> "$LOG"
  [ -z "$PLAN_GAPS" ] || note_plan_hold "$PR_URL"
  if [ -n "$INTENT_CLASS" ] && ! timeout "$NET_TIMEOUT" gh pr edit "$PR_URL" -R "$REPO" --add-label "class:$INTENT_CLASS" >> "$LOG" 2>&1; then
    echo "[$(date -u +%FT%TZ)] could not add label class:$INTENT_CLASS to $PR_URL; the PR stands" >> "$LOG"
  fi
  if [ -n "${INTENT_TIER:-}" ] && ! timeout "$NET_TIMEOUT" gh pr edit "$PR_URL" -R "$REPO" --add-label "$INTENT_TIER" >> "$LOG" 2>&1; then
    echo "[$(date -u +%FT%TZ)] could not add label $INTENT_TIER to $PR_URL; the PR stands" >> "$LOG"
  fi
  echo "[$(date -u +%FT%TZ)] $TICKET done" >> "$LOG"
  printf '[%s] %s agent gh calls: %s\n' "$(date -u +%FT%TZ)" "$TICKET" "$AGENT_GH_CALLS" >> "$LOG"
  mv "$BRIEF" "$INBOX/done/"
done
