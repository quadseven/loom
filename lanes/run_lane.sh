#!/usr/bin/env bash
# Loom lane runner: one pass over a lane's inbox, one brief at a time.
#
#   run_lane.sh <lane>   lane = codex | claude | openrouter | opencode-go | anthropic
#                               | zen | zai | workersai | agy | spark
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
# A repo with a validation contract (.loom/validation.conf) is prepared
# before the agent and checked again on its commit; a failing check parks
# the brief and keeps the edits on a side branch instead of opening a PR.
# A fix brief (a Fix-PR: <number> line, from the station) instead works on
# that open PR's existing branch: it pushes fast-forward only and comments on
# the PR, and never opens a second one.
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
# LOOM_CLONE_BASE (default git@github.com:), for the anthropic lanes
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
LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
export LOOM_HOME
# This checkout, for its python helpers (bus/spend.py).
LOOM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
trap 'rm -rf "$LOCK"' EXIT

AGENT_TIMEOUT="${LOOM_AGENT_TIMEOUT:-3600}"
NET_TIMEOUT="${LOOM_NET_TIMEOUT:-120}"
CHECK_TIMEOUT="${LOOM_CHECK_TIMEOUT:-1800}"
REPO_OWNERS=" ${LOOM_REPO_OWNERS:-} "
CLONE_BASE="${LOOM_CLONE_BASE:-git@github.com:}"
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

field() { grep -m1 "^$1:" "$2" | cut -d' ' -f2-; }

# LOOM_<LANE>_<SUFFIX>, e.g. opencode-go + MODEL -> LOOM_OPENCODE_GO_MODEL.
# A slot's own LOOM_<SLOT>_<SUFFIX> comes first, so one slot can run another
# model on the same provider (opencode-go-3 -> LOOM_OPENCODE_GO_3_MODEL).
setting_var() { printf 'LOOM_%s_%s' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')" "$2"; }
lane_var() { setting_var "$LANE" "$1"; }
lane_setting() {
  local s v
  s="$(setting_var "$SLOT" "$1")" v="$(lane_var "$1")"
  printf '%s' "${!s:-${!v:-}}"
}

# Each pass records whether this lane can take work. The dispatcher routes
# only to lanes with a fresh, available heartbeat. An optional third argument
# is the epoch time a provider limit lifts; until then every pass stays
# unavailable without reading the key or taking a brief.
BEAT="$LOOM_HOME/state/usage/$SLOT.json"
write_beat() {
  local slot="$1" reason retry=""
  # shellcheck disable=SC1003 # a literal backslash is one of the deleted characters
  reason="$(printf '%s' "$3" | tr -d '"\\' | tr '[:cntrl:]' ' ')"
  [[ "${4:-}" =~ ^[0-9]+$ ]] && retry=", \"retry_at\": $4"
  mkdir -p "$LOOM_HOME/state/usage"
  printf '{"lane": "%s", "at": %s, "available": %s, "reason": "%s"%s}\n' \
    "$slot" "$(date +%s)" "$2" "$reason" "$retry" > "$LOOM_HOME/state/usage/$slot.json.tmp" &&
    mv "$LOOM_HOME/state/usage/$slot.json.tmp" "$LOOM_HOME/state/usage/$slot.json"
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

# Body of one "## <name>" section of the brief, blank lines dropped.
section() {
  awk -v h="## $1" '$0 == h {on=1; next} /^#+ / {on=0} on' "$2" | sed '/^[[:space:]]*$/d'
}

# Chief's format: ## Intent (the change class), ## Why, ## Summary,
# ## Acceptance criteria, Size:, ## Out of scope.
INTENT_CLASS=""
INTENT_TEXT=""
SENSITIVE_FILE=.loom/sensitive-paths
SUMMARY_FILE=.loom/summary.md

# What the agent is told about the Intent summary: one line per behavior
# change in .loom/summary.md, else the runner falls back to the ticket's
# What section. The file itself is never committed.
summary_note() {
  printf '\n\n## Intent summary\nWhen you finish, write one line per behavior change to %s at the top of the tree (plain text, one change per line). The runner reads it for the pull request Intent section and never commits it. If there is no behavior change, leave the file out.\n' "$SUMMARY_FILE"
}

# The ## Intent section for the top of a new lane PR body: the change
# class, one line per behavior change, and the counts of files by class.
# Sensitive globs come from SENSITIVE_FILE on the base revision; a missing
# file means no sensitive class. Sets INTENT_CLASS and INTENT_TEXT.
prepare_intent() {
  local brief="$1" files_tmp sens_tmp sum_tmp what_tmp out
  INTENT_CLASS="logic"
  INTENT_TEXT=""
  files_tmp="$(mktemp)" sens_tmp="$(mktemp)" sum_tmp="$(mktemp)" what_tmp="$(mktemp)"
  safe_git diff --name-only "$BASE" HEAD 2>/dev/null | sed '/^[[:space:]]*$/d' > "$files_tmp"
  safe_git cat-file blob "$BASE:$SENSITIVE_FILE" 2>/dev/null > "$sens_tmp"
  if [ -f "$WORKDIR/$TICKET.summary.md" ]; then
    cp "$WORKDIR/$TICKET.summary.md" "$sum_tmp"
  elif [ -f "$TREE/$SUMMARY_FILE" ]; then
    cat "$TREE/$SUMMARY_FILE" > "$sum_tmp" 2>/dev/null
  fi
  section What "$brief" > "$what_tmp" 2>/dev/null
  out="$(PYTHONPATH="$REPO_ROOT" python3 -B -m bus.change_class --files "$files_tmp" --sensitive "$sens_tmp" --summary "$sum_tmp" --what "$what_tmp" 2>/dev/null)"
  rm -f "$files_tmp" "$sens_tmp" "$sum_tmp" "$what_tmp"
  [ -n "$out" ] || return 0
  INTENT_CLASS="$(printf '%s' "$out" | tail -1 | sed 's/^class://')"
  case "$INTENT_CLASS" in sensitive|logic|tests|docs) ;;
    *) INTENT_CLASS="logic" ;;
  esac
  INTENT_TEXT="$(printf '%s' "$out" | sed '$d')"
}

pr_body() {
  local size issue refs=""
  issue="$(field Issue "$1" || true)"
  [[ "$issue" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+$ ]] && refs="$(printf '\nRefs %s' "$issue")"
  # The size as the review bot's letter (Medium -> M), so its plan check
  # passes whatever word the brief used; an unknown size is kept as written.
  size="$(section Size "$1" | head -1)"
  size="$(PYTHONPATH="$REPO_ROOT" python3 -B -m bus.hunt_plan size "$size" 2>/dev/null ||
    printf '%s' "$size" | awk '{print $1}' | tr -d ':')"
  local validated=""
  [ -n "$HAS_CONTRACT" ] && validated="$(printf '\nValidation: %s (%s).' "$VALIDATED" "$CONTRACT_FILE")"
  [ -n "$INTENT_TEXT" ] || prepare_intent "$1"
  printf '%s\n\n## Why\n%s\n\n## Summary\n%s\n\n## Acceptance criteria\n%s\n\nSize: %s\n\n## Out of scope\n%s\n\n---\nLoom ticket %s, lane %s, model %s.%s%s\n' \
    "$INTENT_TEXT" "$(section Why "$1")" "$(section What "$1")" "$(section 'Acceptance criteria' "$1")" \
    "$size" "$(section 'Out of scope' "$1")" "$2" "$LANE" "$MODEL" "$validated" "$refs"
}

# A fix brief's PR must still be open on the brief's branch. Returns 1 with
# FIX_REASON set when it is not, and with FIX_REASON empty when gh could not
# say (the brief then waits for the next pass).
fix_pr_open() {
  local state
  FIX_REASON=""
  state="$(timeout "$NET_TIMEOUT" gh pr view "$FIX_PR" -R "$REPO" --json state,headRefName \
    --jq '.state + " " + .headRefName')" || return 1
  [ "$state" = "OPEN $BRANCH" ] && return 0
  FIX_REASON="pull request #$FIX_PR is no longer open on $BRANCH (${state:-no answer})"
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
post_replies() {
  local entry thread body resolve
  [ -n "$REPLIES_FILE" ] && [ -f "$REPLIES_FILE" ] || return 0
  while IFS= read -r entry; do
    thread="$(jq -r '.thread' <<< "$entry")"
    body="$(jq -r '.reply' <<< "$entry")"
    resolve="$(jq -r '.resolve' <<< "$entry")"
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

# Reconcile the generated title/body sections of an existing PR with the
# approved ticket revision in the brief, preserving reviewer-authored text
# outside those sections. Scope changes are logged with old/new body hashes.
# Exit: 0 = updated, 1 = no change needed, 2 = refused, 3 = gh/helper error.
repair_pr_text() {
  NET_TIMEOUT="$NET_TIMEOUT" PYTHONPATH="$REPO_ROOT" python3 -m bus.pr_repair \
    --apply "$FIX_PR" --repo "$REPO" "$BRIEF" "$LANE" "$MODEL" "$TICKET"
}

# A run that did not end in a PR: say why, park the brief, never retry it.
# The reason starts with the failure's class ($3, default work): the station
# re-briefs limit and transient failures to another lane as a new attempt,
# and leaves work failures to the brain.
park() {
  [ -z "${REPLIES_FILE:-}" ] || rm -f "$REPLIES_FILE"
  echo "[$(date -u +%FT%TZ)] $TICKET failed: [${3:-work}] $2" | tee -a "$LOG" >&2
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
AGY_STATE_KEEP=(settings.json bin builtin installation_id jetski_state.pbtxt updater last_check.timestamp log cli.log crashes cache)
AGY_CACHE_KEEP=(onboarding.json default_project_id.txt)
agy_write_rules() {
  [ "$LANE" = agy ] || return 0
  local h s
  h="$(cd "$AGY_HOME" && pwd -P)" || return 1
  s="$h/.gemini/antigravity-cli"
  printf '(deny file-write* (subpath "%s"))\n(allow file-write* %s (subpath "%s") (subpath "%s/Library/Keychains") (subpath "%s/Library/Caches"))\n(deny file-write* (literal "%s/settings.json") (literal "%s/installation_id") (subpath "%s/bin") (subpath "%s/builtin"))\n' \
    "$h" "$1" "$s" "$h" "$h" "$s" "$s" "$s" "$s"
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
# shell commands and the file tools allowed, web fetching denied. Only
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
SPARK_URL="${LOOM_SPARK_URL:-}"
SPARK_PROBE_TIMEOUT=15
LIMITED=""
LIMIT_WHY=""

# Ask the endpoint for its models. 0: it answered; 1: refused or 503, with
# LIMIT_WHY set; 2: no answer for another reason, with READY_REASON set.
spark_probe() {
  local code status
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$SPARK_PROBE_TIMEOUT" "${SPARK_URL%/}/models" 2>/dev/null)"
  status=$?
  if [ "$status" -eq 7 ]; then LIMIT_WHY="the spark endpoint refused the connection"; return 1; fi
  if [ "$code" = 503 ]; then LIMIT_WHY="the spark endpoint answered 503"; return 1; fi
  [ "$status" -eq 0 ] && return 0
  READY_REASON="the spark endpoint did not answer (curl exit $status)"
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
PROBE_HOLD=43200

# 0 with LIMIT_WHY set when the provider answers 429 GoUsageLimitError for
# MODEL; 1 for any other answer, no answer, or no probe configured.
provider_at_limit() {
  local url header session model body out code limit_name
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
  out="$(printf 'header = "Authorization: Bearer %s"\n' "$API_KEY" |
    curl -s -K - -X POST --max-time "$PROBE_TIMEOUT" -w '\n%{http_code}' \
      -H 'Content-Type: application/json' ${header:+-H "$header: $session"} \
      --data-binary "$body" "$url" 2>/dev/null)" || return 1
  code="${out##*$'\n'}" body="${out%$'\n'*}"
  [ "$code" = 429 ] && [[ "$body" == *GoUsageLimitError* ]] || return 1
  limit_name="$(printf '%s' "$body" | sed -nE 's/.*"limitName" *: *"([A-Za-z0-9_-]+)".*/\1/p' | head -1)"
  LIMIT_WHY="the provider's ${limit_name:-usage} limit (429 GoUsageLimitError)"
  return 0
}

# Hold this lane PROBE_HOLD seconds through the heartbeat's retry_at, and
# hand on every brief still in the inbox this pass.
hold_provider_limit() {
  LIMIT_UNTIL=$(( $(date +%s) + PROBE_HOLD ))
  LIMITED=1
  heartbeat false "limit: $LIMIT_WHY; unavailable until $(utc "$LIMIT_UNTIL")" "$LIMIT_UNTIL"
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

# The opencode binary a slot runs: LOOM_<SLOT>_OPENCODE, else opencode from
# PATH. One slot can try a new major version (its installer would overwrite
# the binary every other opencode lane runs) without moving the rest.
opencode_bin() {
  local b
  b="$(lane_setting OPENCODE)"
  printf '%s' "${b:-opencode}"
}

# Usage: opencode_agent <key_var> <brief text>
opencode_agent() {
  opencode_run "$1" "$(opencode_bin)" "$2"
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
# setup and check run as shell commands, like the run: steps of a CI
# workflow, so they are only ever read from the branch the pull request
# merges into: the base revision of a new run, and the base branch (fetched
# for it) of a fix run, whose own branch holds unmerged agent edits. Nothing
# an agent edits changes what is checked. A repo without the file is not
# validated by the runner (its CI still is).
CONTRACT_FILE=.loom/validation.conf
load_contract() {
  local rev="$BASE" mode line key val
  HAS_CONTRACT="" C_INTERP="" C_SUBMODULES=no C_SETUP="" C_CHECK=""
  if [ -n "$FIX_PR" ] && ! { timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null \
       -c core.fsmonitor=false fetch -q --depth 1 origin "${BASE_BRANCH:-HEAD}" >> "$LOG" 2>&1 &&
       rev="$(safe_git rev-parse --verify -q FETCH_HEAD)"; }; then
    HAS_CONTRACT=1 SETUP_REASON="could not fetch the base branch for $CONTRACT_FILE"; return 1
  fi
  mode="$(safe_git ls-tree "$rev" -- "$CONTRACT_FILE" | cut -d' ' -f1)"
  [ -n "$mode" ] || return 0
  HAS_CONTRACT=1
  [ "$mode" = 100644 ] || [ "$mode" = 100755 ] || { SETUP_REASON="$CONTRACT_FILE is not a regular file"; return 1; }
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%:*}" val="${line#*:}"
    val="${val#"${val%%[![:space:]]*}"}"
    case "$key" in
      interpreter) C_INTERP="$val" ;;
      submodules) C_SUBMODULES="$val" ;;
      setup) C_SETUP="$val" ;;
      check) C_CHECK="$val" ;;
      *) SETUP_REASON="$CONTRACT_FILE has an unknown line '${line:0:40}'"; return 1 ;;
    esac
  done < <(safe_git cat-file blob "$rev:$CONTRACT_FILE")
  [ -n "$C_CHECK" ] || { SETUP_REASON="$CONTRACT_FILE has no check: line"; return 1; }
  case "$C_SUBMODULES" in yes|no) ;; *) SETUP_REASON="$CONTRACT_FILE submodules: must be yes or no"; return 1 ;; esac
  [ -z "$C_INTERP" ] || [[ "$C_INTERP" =~ ^[A-Za-z0-9._+-]+$ ]] ||
    { SETUP_REASON="$CONTRACT_FILE interpreter: must be a command name"; return 1; }
  for val in "$C_SETUP" "$C_CHECK"; do
    contract_command_ok "$val" ||
      { SETUP_REASON="$CONTRACT_FILE setup: and check: must be printable ASCII, at most 1000 characters"; return 1; }
  done
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

# Run one contract command in the tree the way an API-lane agent runs: an
# emptied environment with a scratch HOME, under the sandbox profile (its
# second argument is the interpreter, whose directory stays readable). The
# check after the agent runs the agent's code, so it must never see the
# runner's HOME, keys or SSH agent. Output goes through a pipe to the log.
# The command passed to bash -c is deliberately shell syntax: load_contract
# reads it from the base branch's trusted CI contract, never the agent's
# branch, and contract_command_ok refuses (exit 125, nothing run) any string
# that is not one bounded line of printable ASCII.
contract_run() {
  local profile
  contract_command_ok "$1" || { echo "contract_run: refusing a command that is not printable ASCII" >> "$LOG"; return 125; }
  profile="$(AGENT_HOME="$CHECK_HOME" sandbox_profile "$TREE" "$CHECK_CLI" check)" || return 125
  # shellcheck disable=SC2030,SC2031 # deliberately local to the check's subshell
  (
    cd "$TREE" || exit 125
    keep_path="$PATH" keep_lang="${LANG:-en_US.UTF-8}" home="$CHECK_HOME"
    for v in $(compgen -e); do unset "$v"; done
    export PATH="$keep_path" HOME="$home" TMPDIR="$home/tmp" TERM=dumb LANG="$keep_lang"
    exec timeout "$CHECK_TIMEOUT" sandbox-exec -p "$profile" /bin/bash -c "$1" < /dev/null
  ) 2>&1 | timeout "$((CHECK_TIMEOUT + 60))" cat >> "$LOG"
}

tracked_changes() {
  safe_git status --porcelain --untracked-files=no --ignore-submodules=dirty | head -5 | tr '\n' ' '
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
# base (recorded, so a failure the base already had is told apart from one
# the agent caused; it does not stop the run, since a ticket may exist to fix
# it). Returns 1 with SETUP_REASON when the environment cannot be prepared.
prepare_validation() {
  local rc interp="" subs="" changed what
  SETUP_REASON=""
  load_contract || { record prepare "$CONTRACT_FILE" 1 "$BASE" false "$SETUP_REASON"; return 1; }
  [ -n "$HAS_CONTRACT" ] || return 0
  what="interpreter ${C_INTERP:-none}; submodules $C_SUBMODULES"
  if ! command -v sandbox-exec > /dev/null; then SETUP_REASON="sandbox-exec not available"
  elif [ -n "$C_INTERP" ] && ! interp="$(command -v "$C_INTERP")"; then SETUP_REASON="interpreter $C_INTERP is not on PATH"
  elif [ "$C_SUBMODULES" = yes ] && ! timeout "$CHECK_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null \
         -c core.fsmonitor=false submodule update --init --recursive >> "$LOG" 2>&1; then
    SETUP_REASON="submodule checkout failed"
  fi
  if [ -n "$SETUP_REASON" ]; then record prepare "$what" 1 "$BASE" false "$SETUP_REASON"; return 1; fi
  [ -n "$interp" ] && interp="$interp $("$interp" --version 2>&1 | head -1)"
  [ "$C_SUBMODULES" = yes ] && subs="$(safe_git submodule status --recursive | tr '\n' ';')"
  record prepare "$what" 0 "$BASE" true "interpreter: ${interp:-none}; submodules: ${subs:-none}"
  CHECK_CLI="${C_INTERP:-bash}"
  rm -rf "$CHECK_HOME"
  mkdir -p "$CHECK_HOME/tmp"
  # C_SETUP and C_CHECK are shell on purpose, like a CI run: step. They are
  # validated in load_contract: read only from the base branch (never the
  # agent's), one line of printable ASCII, at most 1000 characters; and
  # contract_run runs them sandboxed with an emptied environment.
  if [ -n "$C_SETUP" ]; then
    contract_run "$C_SETUP"; rc=$?
    record setup "$C_SETUP" "$rc" "$BASE" "$([ "$rc" -eq 0 ] && echo true || echo false)"
    [ "$rc" -eq 0 ] || { SETUP_REASON="setup exited $rc"; return 1; }
  fi
  contract_run "$C_CHECK"; BASELINE_EXIT=$?
  changed="$(tracked_changes)"
  record baseline "$C_CHECK" "$BASELINE_EXIT" "$BASE" "$([ "$BASELINE_EXIT" -eq 0 ] && [ -z "$changed" ] && echo true || echo false)" \
    "${changed:+changed tracked files: $changed}"
  [ -z "$changed" ] || { SETUP_REASON="setup or check changed tracked files on the base: $changed"; return 1; }
  exclude_untracked
}

# After the agent, on the committed branch head: the same check again. Sets
# VALIDATION_REASON when it did not pass.
final_validation() {
  local rc rev changed ok=false
  VALIDATION_REASON=""
  safe_git checkout -q "$BRANCH" >> "$LOG" 2>&1
  rev="$(safe_git rev-parse HEAD)"
  contract_run "$C_CHECK"; rc=$?
  changed="$(tracked_changes)"
  case "$rc" in
    0) [ -z "$changed" ] && ok=true || VALIDATION_REASON="validation failed: the check changed tracked files ($changed)" ;;
    124) VALIDATION_REASON="validation failed: the check timed out after ${CHECK_TIMEOUT}s" ;;
    125|126|127) VALIDATION_REASON="validation environment unavailable: the check exited $rc" ;;
    *) VALIDATION_REASON="validation failed: the check exited $rc" ;;
  esac
  record final "$C_CHECK" "$rc" "$rev" "$ok" "${changed:+changed tracked files: $changed}"
  [ "$ok" = true ] || { VALIDATION_REASON="$VALIDATION_REASON at ${rev:0:12} (base check exited $BASELINE_EXIT)"; return 1; }
  VALIDATED="check exited 0 at ${rev:0:12}"
}

# A run that failed validation keeps its edits: they are committed and pushed
# to a side branch, never to the ticket's branch or an open pull request.
preserve_unvalidated() {
  local side
  side="$BRANCH-unvalidated-$(date +%s)"
  if timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
       push -q origin "$BRANCH:refs/heads/$side" >> "$LOG" 2>&1; then
    printf 'the edits are on branch %s' "$side"
  else
    printf 'the push of %s failed; the edits are committed in the lane tree' "$side"
  fi
}

# What the agent is told about the contract, after its brief.
validation_note() {
  [ -n "$HAS_CONTRACT" ] || return 0
  printf '\n\n## Validation environment\nThe runner prepared this tree from %s (interpreter: %s, submodules: %s, setup: %s). After you finish it runs the check below on your committed tree; the pull request opens only if it exits 0 and changes no tracked file. On the base it exited %s.\n\n    %s\n' \
    "$CONTRACT_FILE" "${C_INTERP:-any}" "$C_SUBMODULES" "${C_SETUP:-none}" "$BASELINE_EXIT" "$C_CHECK"
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
  case "$LANE:$MODEL" in zen:*|zai:*|agy:*|opencode-go:*muse-spark*contributor*) return 0 ;; esac
  return 1
}

# Lanes whose agent runs sandboxed in a fresh scratch home (api_agent).
is_api_lane() {
  case "$LANE" in openrouter|opencode-go|anthropic|zen|zai|workersai|spark) return 0 ;; esac
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

# CONFLICTS (one path a line) as one comma-separated line.
conflict_list() {
  printf '%s\n' "$CONFLICTS" | sed '/^$/d' | paste -sd ',' - | sed 's/,/, /g'
}

# Paths of CONFLICTS that still hold a conflict marker line, comma-separated.
# A fix brief's agent may answer a finding instead of fixing it, one file a
# thread in .loom-answers/ (bus/tickets.py says how). The answers never go in
# the commit: they move to the inbox, where the station posts each on its
# thread. The copy opens the folder and each file without following links,
# through the folder's descriptor, and keeps only a regular file with one
# link, at most 8 KB, named for a thread: a symlink, a hard link to a file
# outside the tree, a FIFO or a folder swapped for a link is dropped unread,
# so an answer cannot carry a file the agent could not read itself.
keep_answers() {
  local dir="$TREE/.loom-answers"
  [ -e "$dir" ] || [ -L "$dir" ] || return 0
  if [ -n "$FIX_PR" ]; then
    if ! python3 - "$dir" "$INBOX/answers/$TICKET" <<'PY' >> "$LOG" 2>&1
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

run_agent() {
  local brief_text
  brief_text="$(cat "$1")$(validation_note)$(summary_note)"
  AGENT_EXTRA_ENV=""
  is_api_lane && fresh_agent_home
  case "$LANE" in
    codex) timeout "$AGENT_TIMEOUT" codex exec --sandbox workspace-write --model "$MODEL" "$brief_text" < /dev/null ;;
    claude) timeout "$AGENT_TIMEOUT" claude -p --model "$MODEL" "$brief_text" < /dev/null ;;
    openrouter) opencode_agent OPENROUTER_API_KEY "$brief_text" ;;
    opencode-go) opencode_agent OPENCODE_API_KEY "$brief_text" ;;
    zen) opencode_agent - "$brief_text" ;;
    spark) opencode_agent - "$brief_text" ;;
    zai) opencode_agent ZHIPU_API_KEY "$brief_text" ;;
    workersai) AGENT_EXTRA_ENV="CLOUDFLARE_ACCOUNT_ID=$LANE_ACCOUNT"
      opencode_agent CLOUDFLARE_API_KEY "$brief_text" ;;
    anthropic) anthropic_agent "$brief_text" ;;
    agy) agy_agent "$brief_text" ;;
    *) echo "unknown lane $LANE" >&2; return 2 ;;
  esac
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
  if [ "$LANE" = agy ]; then
    command -v sandbox-exec > /dev/null || { READY_REASON="sandbox-exec not available"; return 1; }
    "${AGY_RUNAS[@]}" test -x "$AGY_BIN" > /dev/null 2>&1 ||
      { READY_REASON="cannot run $AGY_BIN as $AGY_USER"; return 1; }
    agy_key || return 1
    agy_unlock
    return
  fi
  case "$LANE" in
    codex) cli=codex ;; claude|anthropic) cli=claude ;; openrouter|opencode-go|zen|zai|workersai|spark) cli="$(opencode_bin)" ;;
    *) READY_REASON="unknown lane"; return 1 ;;
  esac
  command -v "$cli" > /dev/null || { READY_REASON="$cli not on PATH"; return 1; }
  [ "$LANE" = anthropic ] && ! anthropic_credit_ok && return 1
  case "$LANE" in codex|claude) return 0 ;; esac
  command -v sandbox-exec > /dev/null || { READY_REASON="sandbox-exec not available"; return 1; }
  case "$LANE" in zen) return 0 ;; spark) spark_ready; return ;; esac
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
for t in "AGENT_TIMEOUT:$AGENT_TIMEOUT:60" "NET_TIMEOUT:$NET_TIMEOUT:1" "CHECK_TIMEOUT:$CHECK_TIMEOUT:60"; do
  IFS=: read -r t_name t_val t_min <<< "$t"
  if ! [[ "$t_val" =~ ^[0-9]+$ ]] || [ "$t_val" -lt "$t_min" ]; then
    heartbeat false "LOOM_$t_name must be an integer >= $t_min, got '$t_val'"
    exit 0
  fi
done

# A provider limit hit by an earlier pass holds until its retry time. A
# reason that says "limit:" (which limit, until when) is kept as it was.
LIMIT_UNTIL="$(sed -nE 's/.*"retry_at": *([0-9]+).*/\1/p' "$BEAT" 2>/dev/null | head -1)"
CONFIGURED_MODELS="$(lane_setting MODELS)"
if [ -z "$CONFIGURED_MODELS" ] && [ -n "$LIMIT_UNTIL" ] && [ "$LIMIT_UNTIL" -gt "$(date +%s)" ]; then
  HELD_REASON="$(sed -nE 's/.*"reason": "([^"]*)".*/\1/p' "$BEAT" 2>/dev/null | head -1)"
  case "$HELD_REASON" in limit:*) ;; *) HELD_REASON="provider limit reached; lane waits until $(utc "$LIMIT_UNTIL")" ;; esac
  heartbeat false "$HELD_REASON" "$LIMIT_UNTIL"
  exit 0
fi

AGENT_HOME="$LOOM_HOME/run/$SLOT-home"
CHECK_HOME="$LOOM_HOME/run/$SLOT-check"
API_KEY=""
LANE_ACCOUNT=""
READY_REASON=""
if ! lane_ready; then
  heartbeat false "$READY_REASON"
  # A limited lane still passes over its inbox, to hand every brief on.
  [ -n "$LIMITED" ] || exit 0
else
  heartbeat true ""
fi
# Only the sandboxed lanes need the snapshot (codex and claude read the
# operator's own folders), and only when there is work.
if { is_api_lane || [ "$LANE" = agy ]; } && compgen -G "$INBOX/*.md" > /dev/null; then
  refresh_skills
fi

for BRIEF in "$INBOX"/*.md; do
  [ -e "$BRIEF" ] || continue
  TICKET="$(basename "$BRIEF" .md)"
  REPO="$(field Repo "$BRIEF")"
  BRANCH="$(field Branch "$BRIEF")"
  # Optional: the branch work starts from and the PR targets. Default is the
  # remote's default branch, which is not always where work lands.
  BASE_BRANCH="$(field Base "$BRIEF" || true)"
  LOG="$LOGDIR/$TICKET.log"

  # Ticket names become paths in the sandbox profile and the clone.
  if ! [[ "$TICKET" =~ ^[A-Za-z0-9._-]+$ ]]; then
    park "$BRIEF" "ticket name must be letters, digits, dot, underscore or dash"; continue
  fi
  if [ -n "$LIMITED" ]; then park "$BRIEF" "limit: $LIMIT_WHY" limit; continue; fi
  # Briefs are input: the runner only ever pushes a loom-owned branch.
  if ! [[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ && "$BRANCH" =~ ^loom/[A-Za-z0-9._/-]+$ ]]; then
    park "$BRIEF" "brief needs Repo: owner/name and Branch: loom/..."; continue
  fi
  if [ -n "$BASE_BRANCH" ] && ! [[ "$BASE_BRANCH" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ ]]; then
    park "$BRIEF" "brief Base: is not a branch name"; continue
  fi
  BASE_ARGS=()
  [ -n "$BASE_BRANCH" ] && BASE_ARGS=(--branch "$BASE_BRANCH")
  PR_BASE_ARGS=()
  [ -n "$BASE_BRANCH" ] && PR_BASE_ARGS=(--base "$BASE_BRANCH")
  # The model: the brief's Model: line, else the lane default. Passed to the
  # CLI explicitly so the PR names what actually ran.
  PINNED_MODEL="$(field Model "$BRIEF" || true)"
  MODEL="$PINNED_MODEL"
  if [ -z "$MODEL" ]; then
    MODEL="$(lane_setting MODEL)"
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
    opencode-go:opencode-go/*) ;;
    opencode-go:*) park "$BRIEF" "model $MODEL is not an opencode-go model" transient; continue ;;
    zen:opencode/*-free) ;;
    zen:*) park "$BRIEF" "zen lane runs free models only (opencode/...-free), not $MODEL"; continue ;;
    zai:zai/glm-4.7-flash|zai:zai/glm-4.5-flash) ;;
    zai:*) park "$BRIEF" "zai lane runs the free Flash models only (zai/glm-4.7-flash, zai/glm-4.5-flash), not $MODEL"; continue ;;
    workersai:cloudflare-workers-ai/@cf/*) ;;
    spark:spark/?*) ;;
    spark:*) park "$BRIEF" "model $MODEL is not a spark model (spark/...)"; continue ;;
    agy:gemini-*|agy:claude-*|agy:gpt-oss-*) ;;
    agy:*) park "$BRIEF" "model $MODEL is not in the agy catalog (gemini-*, claude-*, gpt-oss-*)"; continue ;;
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
  # Providers that train on prompts take public repos only. Asked before the
  # clone; a failed lookup proves nothing, so the brief waits for the next pass.
  if trains_on_prompts; then
    if ! PRIVATE="$(timeout "$NET_TIMEOUT" gh api "repos/$REPO" --jq .private)"; then
      echo "$TICKET: gh api repos/$REPO failed; will retry next pass" >&2; continue
    fi
    case "$PRIVATE" in
      false) ;;
      true) park "$BRIEF" "$REPO is private; lane $LANE with $MODEL trains on prompts and takes public repos only"; continue ;;
      *) echo "$TICKET: unexpected privacy answer '$PRIVATE' for $REPO; will retry next pass" >&2; continue ;;
    esac
  fi

  FIX_PR="$(field Fix-PR "$BRIEF" || true)"
  if [ -n "$FIX_PR" ]; then
    if ! [[ "$FIX_PR" =~ ^[0-9]+$ ]]; then
      park "$BRIEF" "brief Fix-PR: is not a pull request number"; continue
    fi
    if ! fix_pr_open; then
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
  # brief handed on before anything is cloned.
  if provider_at_limit; then
    hold_provider_limit
    park "$BRIEF" "limit: $LIMIT_WHY" limit; continue
  fi

  TREE="$WORKDIR/$TICKET"
  rm -rf "$TREE"
  echo "[$(date -u +%FT%TZ)] $TICKET start, lane $SLOT" >> "$LOG"
  if ! { timeout "$NET_TIMEOUT" git clone -q --depth 50 ${CLONE_ARGS[@]+"${CLONE_ARGS[@]}"} "${CLONE_BASE}${REPO}.git" "$TREE" &&
         { [ -n "$FIX_PR" ] || git -C "$TREE" checkout -q -b "$BRANCH"; }; } >> "$LOG" 2>&1; then
    park "$BRIEF" "clone or branch failed" transient; continue
  fi
  BASE="$(git -C "$TREE" rev-parse HEAD)"
  RECORD="$LOGDIR/$TICKET.validation.jsonl"
  rm -f "$RECORD"
  HAS_CONTRACT="" BASELINE_EXIT="" VALIDATED=""
  if ! prepare_validation >> "$LOG" 2>&1; then
    park "$BRIEF" "setup failed: $SETUP_REASON; the agent did not run"; continue
  fi
  RUN_BRIEF="$BRIEF"
  CONFLICTS=""
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
  LOG_START="$(wc -c < "$LOG" | tr -d ' ')"

  # The agent runs as a background job of a job-control subshell, which puts
  # it in its own process group (id = its pid) and records that id. If this
  # runner is killed (a reinstall's bootout), the agent survives in its group,
  # and the next pass's stale-lock recovery above kills exactly that group.
  LOG_START="$(wc -c < "$LOG" 2>/dev/null | tr -d ' ')"
  ( set -m
    (cd "$TREE" && run_agent "$RUN_BRIEF") >> "$LOG" 2>&1 &
    echo $! > "$LOCK/pgid"
    wait $! ) &
  wait $!
  STATUS=$?
  is_api_lane && archive_transcripts
  # A spark run that ended because the box went hot is a limit, not a failure.
  if [ "$STATUS" -ne 0 ] && [ "$LANE" = spark ] && { spark_probe; [ $? -eq 1 ]; }; then
    mark_limit
    park "$BRIEF" "limit: $LIMIT_WHY (agent exited $STATUS)" limit; continue
  fi
  # A run that timed out may have been retrying a usage limit all along.
  if [ "$STATUS" -eq 124 ] && provider_at_limit; then
    hold_provider_limit
    park "$BRIEF" "limit: $LIMIT_WHY (agent exited $STATUS)" limit; continue
  fi
  EDITED=""
  { [ -n "$(safe_git status --porcelain)" ] || [ "$(safe_git rev-parse HEAD)" != "$BASE" ]; } && EDITED=edited
  # Only a run that failed or changed nothing is classed: a finished run's
  # summary may well mention rate limits.
  FAIL_CLASS=work
  FAILURE_OUT="$(tail -c +$((LOG_START + 1)) "$LOG" | grep -v '^[[:space:]]*$' | tail -n 5)"
  if [ "$STATUS" -ne 0 ] || [ -z "$EDITED" ]; then
    classify_failure "$STATUS" "$FAILURE_OUT" "$EDITED"
  fi
  if [ "$FAIL_CLASS" = limit ]; then
    # Unavailable before the brief is parked, so the station never sends
    # this lane the retry. The rest of the inbox waits for the reset.
    if [[ "$(printf '%s' "$FAILURE_OUT" | tr '[:upper:]' '[:lower:]')" =~ $OUT_OF_CREDIT_RE ]]; then
      LIMIT_REASON="out of credit"
      hold_shared_credit "$RETRY_AT"
      heartbeat false "limit: out of credit; lane waits until $(utc "$RETRY_AT")" "$RETRY_AT"
    elif [ -n "$CONFIGURED_MODELS" ]; then
      LIMIT_REASON="provider limit reached"
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
      LIMIT_REASON="provider limit reached"
      heartbeat false "limit: $LIMIT_REASON; lane waits until $(utc "$RETRY_AT")" "$RETRY_AT"
    fi
    park "$BRIEF" "agent exited $STATUS: $LIMIT_REASON; lane $SLOT waits until $(utc "$RETRY_AT")" limit
    break
  fi
  if [ "$STATUS" -ne 0 ]; then park "$BRIEF" "agent exited $STATUS" "$FAIL_CLASS"; continue; fi
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
  # During a merge the branch is already checked out, and the commit
  # completes the merge.
  if [ -n "$(safe_git status --porcelain)" ] || [ -n "$CONFLICTS" ]; then
    if ! { { [ -n "$CONFLICTS" ] || safe_git checkout -q "$BRANCH"; } && safe_git add -A &&
           safe_git commit -qm "${TITLE:-$TICKET}"; } >> "$LOG" 2>&1; then
      park "$BRIEF" "could not commit the agent's changes"; continue
    fi
  fi
  COMMITTED=0
  if [ "$(safe_git rev-parse "$BRANCH")" != "$BASE" ]; then
    COMMITTED=1
  fi
  # An agent's exit code is not evidence: only a passing check on the
  # committed head hands the work on.
  if [ -n "$HAS_CONTRACT" ] && ! final_validation; then
    HANDOFF="no pull request opened"
    [ -n "$FIX_PR" ] && HANDOFF="nothing pushed to #$FIX_PR"
    park "$BRIEF" "$VALIDATION_REASON; $(preserve_unvalidated); $HANDOFF"; continue
  fi
  # A PR closed or merged during the run gets nothing pushed: a push would
  # bring its deleted branch back.
  if [ -n "$FIX_PR" ] && ! fix_pr_open; then
    park "$BRIEF" "${FIX_REASON:-could not check pull request #$FIX_PR before pushing}"; continue
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
    park "$BRIEF" "agent made no commit and PR text is current on $BRANCH"; continue
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
      park "$BRIEF" "push failed" transient; continue
    fi
    [ -z "$PUSH_OUT" ] || printf '%s\n' "$PUSH_OUT" >> "$LOG"
  fi
  if [ -n "$FIX_PR" ]; then
    post_replies
    ITEMS="$(fix_items "$BRIEF")"
    if [ "$COMMITTED" -eq 1 ]; then
      COMMENT="Lane $SLOT pushed fixes for: ${ITEMS:-the findings on the ticket}"
    elif [ "$METADATA_CHANGED" -eq 0 ]; then
      COMMENT="Lane $SLOT answered the review threads for: ${ITEMS:-the findings on the ticket}"
    else
      COMMENT="Lane $SLOT repaired the PR text for: ${ITEMS:-the findings on the ticket}"
    fi
    if ! timeout "$NET_TIMEOUT" gh pr comment "$FIX_PR" -R "$REPO" --body "$COMMENT" >> "$LOG" 2>&1; then
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
  INTENT_CLASS=""
  INTENT_TEXT=""
  prepare_intent "$BRIEF" >> "$LOG" 2>&1
  # The class label goes on after the PR exists: a repo without the label
  # must never cost a finished run its PR (2026-10-14, ten runs lost theirs).
  PR_BODY="$(pr_body "$BRIEF" "$TICKET")"
  # Briefs are checked when filed, so this only fires on a brief filed
  # before that check or edited by hand: the PR still opens (the work is
  # done), and the log names what the review bot will hold it for.
  if ! PLAN_PROBLEMS="$(printf '%s' "$PR_BODY" | PYTHONPATH="$REPO_ROOT" python3 -B -m bus.hunt_plan check - 2>&1)"; then
    echo "[$(date -u +%FT%TZ)] $TICKET pull request body fails the plan checks: $(printf '%s' "$PLAN_PROBLEMS" | tr '\n' ';')" >> "$LOG"
  fi
  if ! PR_URL="$(timeout "$NET_TIMEOUT" gh pr create -R "$REPO" ${PR_BASE_ARGS[@]+"${PR_BASE_ARGS[@]}"} --head "$BRANCH" --title "${TITLE:-$TICKET}" \
       --body "$PR_BODY" 2>> "$LOG")"; then
    park "$BRIEF" "pr create failed; branch $BRANCH is pushed" transient; continue
  fi
  echo "$PR_URL" >> "$LOG"
  if [ -n "$INTENT_CLASS" ] && ! timeout "$NET_TIMEOUT" gh pr edit "$PR_URL" -R "$REPO" --add-label "class:$INTENT_CLASS" >> "$LOG" 2>&1; then
    echo "[$(date -u +%FT%TZ)] could not add label class:$INTENT_CLASS to $PR_URL; the PR stands" >> "$LOG"
  fi
  echo "[$(date -u +%FT%TZ)] $TICKET done" >> "$LOG"
  mv "$BRIEF" "$INBOX/done/"
done
