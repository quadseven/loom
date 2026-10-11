#!/usr/bin/env bash
# Loom lane runner: one pass over a lane's inbox, one brief at a time.
#
#   run_lane.sh <lane>   lane = codex | claude | openrouter | opencode-go | anthropic
#
# Run it on a schedule (launchd StartInterval), not as a forever loop. Every
# brief ends in exactly one place: done/ (a PR exists for its branch) or
# failed/ (the run did not produce one). A brief is never run twice, so a
# crash or restart cannot turn into a re-run loop. The agent edits (and
# may commit); this runner commits what is left, pushes, and opens the PR.
# Lanes never merge. codex runs sandboxed: no network, no .git writes.
#
# Env: LOOM_REPO_OWNERS (required: space-separated GitHub owners whose repos
# a brief may name; the agent trusts the cloned repo's own config),
# LOOM_<LANE>_MODEL (the lane's model when a brief has no Model: line, e.g.
# LOOM_OPENCODE_GO_MODEL; every run passes its model explicitly and names it
# in the PR), LOOM_<LANE>_KEY_SSM (API lanes: the SSM parameter holding the
# provider key, read at run time; never stored in this repo),
# LOOM_HOME (default ~/loom), LOOM_AGENT_TIMEOUT (seconds, default 3600),
# LOOM_NET_TIMEOUT (seconds per gh/git network call, default 120),
# LOOM_CLONE_BASE (default git@github.com:).
set -uo pipefail

LANE="${1:?usage: run_lane.sh <lane>}"
LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
INBOX="$LOOM_HOME/briefs/$LANE"
LOCK="$LOOM_HOME/run/$LANE.lock"

mkdir -p "$INBOX/done" "$INBOX/failed" "$LOOM_HOME/run"

# One runner per lane. mkdir is atomic; a stale lock from a killed run is
# cleared when its pid is gone.
if ! mkdir "$LOCK" 2>/dev/null; then
  if kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then exit 0; fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

AGENT_TIMEOUT="${LOOM_AGENT_TIMEOUT:-3600}"
NET_TIMEOUT="${LOOM_NET_TIMEOUT:-120}"
REPO_OWNERS=" ${LOOM_REPO_OWNERS:-} "
CLONE_BASE="${LOOM_CLONE_BASE:-git@github.com:}"
WORKDIR="$LOOM_HOME/worktrees"
LOGDIR="$LOOM_HOME/logs/$LANE"
mkdir -p "$WORKDIR" "$LOGDIR"
# launchd starts with a bare PATH; the agent CLIs live in these.
PATH="$PATH:$HOME/.local/bin:$HOME/.opencode/bin:/opt/homebrew/bin:/usr/local/bin"

field() { grep -m1 "^$1:" "$2" | cut -d' ' -f2-; }

# LOOM_<LANE>_<SUFFIX>, e.g. opencode-go + MODEL -> LOOM_OPENCODE_GO_MODEL.
lane_var() { printf 'LOOM_%s_%s' "$(printf '%s' "$LANE" | tr '[:lower:]-' '[:upper:]_')" "$1"; }
lane_setting() { local v; v="$(lane_var "$1")"; printf '%s' "${!v:-}"; }

# Each pass records whether this lane can take work. The dispatcher routes
# only to lanes with a fresh, available heartbeat.
heartbeat() {
  local reason
  # shellcheck disable=SC1003 # a literal backslash is one of the deleted characters
  reason="$(printf '%s' "$2" | tr -d '"\\' | tr '[:cntrl:]' ' ')"
  mkdir -p "$LOOM_HOME/state/usage"
  printf '{"lane": "%s", "at": %s, "available": %s, "reason": "%s"}\n' \
    "$LANE" "$(date +%s)" "$1" "$reason" > "$LOOM_HOME/state/usage/$LANE.json"
}

# Body of one "## <name>" section of the brief, blank lines dropped.
section() {
  awk -v h="## $1" '$0 == h {on=1; next} /^#+ / {on=0} on' "$2" | sed '/^[[:space:]]*$/d'
}

# Chief's format: ## Why, ## Summary, ## Acceptance criteria, Size:, ## Out of scope.
pr_body() {
  local size
  size="$(section Size "$1" | head -1 | awk '{print $1}' | tr -d ':')"
  printf '## Why\n%s\n\n## Summary\n%s\n\n## Acceptance criteria\n%s\n\nSize: %s\n\n## Out of scope\n%s\n\n---\nLoom ticket %s, lane %s, model %s.\n' \
    "$(section Why "$1")" "$(section What "$1")" "$(section 'Acceptance criteria' "$1")" \
    "$size" "$(section 'Out of scope' "$1")" "$2" "$LANE" "$MODEL"
}

# A run that did not end in a PR: say why, park the brief, never retry it.
# The orchestrator reads failed/ and re-briefs by hand.
park() {
  echo "[$(date -u +%FT%TZ)] $TICKET failed: $2" | tee -a "$LOG" >&2
  mv "$1" "$INBOX/failed/"
}

# Seatbelt profile for API-lane agents: no reads or writes anywhere under the
# real HOME except the worktree, the scratch home and the directory holding
# the CLI's binary. Inside the worktree, .git stays readable (git diff and git
# log need it; a fresh clone holds no credentials) but not writable: a planted
# hook or config would run when the runner commits. Also no keychain, no Unix
# sockets except DNS (an SSH agent's socket would hand over its keys), and no
# AppleEvents, LaunchServices, osascript, open or launchctl (each can run a
# command outside the sandbox). Paths are canonical, because the sandbox
# matches resolved paths; proper ancestors get metadata-only literals so
# getcwd and realpath still work without making HOME listable.
sandbox_profile() {
  local home tree cli_link cli_real cli_root p allow_rw allow_ro meta=""
  home="$(cd "$HOME" && pwd -P)"
  tree="$(cd "$1" && pwd -P)"
  cli_link="$(command -v "$2")" && [ -n "$cli_link" ] || { echo "refusing sandbox: $2 not on PATH" >&2; return 1; }
  cli_real="$(realpath "$cli_link")" && [ -n "$cli_real" ] || { echo "refusing sandbox: cannot resolve $cli_link" >&2; return 1; }
  cli_root="$(dirname "$cli_real")"
  # Paths go into the profile's string grammar: refuse any that could close it.
  for p in "$home" "$tree" "$cli_link" "$cli_real" "$AGENT_HOME"; do
    # shellcheck disable=SC1003 # matching a literal backslash
    case "$p" in *'"'*|*'\'*) echo "refusing sandbox: unsafe path $p" >&2; return 1 ;; esac
  done
  # Never let the CLI's directory cover HOME itself.
  case "$home/" in "$cli_root"/*) echo "refusing sandbox: $cli_real sits at or above HOME" >&2; return 1 ;; esac
  allow_rw="(subpath \"$tree\") (subpath \"$(cd "$AGENT_HOME" && pwd -P)\")"
  allow_ro="(subpath \"$cli_root\") (literal \"$cli_link\")"
  for p in "$tree" "$AGENT_HOME" "$cli_root" "$cli_link"; do
    p="$(dirname "$p")"
    while [ "${p#"$home"}" != "$p" ] && [ "$p" != "/" ]; do
      meta="$meta (literal \"$p\")"
      [ "$p" = "$home" ] && break
      p="$(dirname "$p")"
    done
  done
  printf '(version 1)\n(allow default)\n(deny file-read* file-write* (subpath "%s"))\n(allow file-read* file-write* %s)\n(deny file-write* (subpath "%s/.git"))\n(allow file-read* %s)\n(allow file-read-metadata%s)\n(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.coreservices.appleevents"))\n(deny appleevent-send)\n(deny process-exec (literal "/usr/bin/osascript") (literal "/usr/bin/open") (literal "/bin/launchctl"))\n(deny network-outbound (remote unix-socket))\n(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))\n' \
    "$home" "$allow_rw" "$tree" "$allow_ro" "$meta"
}

# API lanes run the agent with an emptied environment (its provider key, a
# scratch HOME and TMPDIR, PATH) inside the sandbox above. The key is
# exported, never put in argv, so `ps` cannot show it. The agent can edit its
# tree but cannot read credentials, push, open, or merge; the runner does that.
api_agent() {
  local key_var="$1" cli="$2" profile
  mkdir -p "$AGENT_HOME/tmp"
  profile="$(sandbox_profile "$PWD" "$cli")" || return 3
  (
    keep_path="$PATH" keep_lang="${LANG:-en_US.UTF-8}" key="$API_KEY" extra="$AGENT_EXTRA_ENV"
    for v in $(compgen -e); do unset "$v"; done
    [ -z "$(compgen -e)" ] || { echo "could not clear the agent environment: $(compgen -e | tr '\n' ' ')" >&2; exit 3; }
    export PATH="$keep_path" HOME="$AGENT_HOME" TMPDIR="$AGENT_HOME/tmp" TERM=dumb LANG="$keep_lang"
    export "$key_var=$key"
    [ -z "$extra" ] || export "${extra?}"
    exec timeout "$AGENT_TIMEOUT" sandbox-exec -p "$profile" "${@:2}" < /dev/null
  ) 2>&1 | timeout "$((AGENT_TIMEOUT + 60))" cat
  # The agent writes into a pipe, not the log file: the log lives under HOME,
  # where the sandbox denies even fstat, and CLIs fstat their output at start.
  # pipefail carries the agent's exit status through `cat`. The reader has its
  # own deadline: a process the agent detached into another process group
  # survives `timeout` and would hold the pipe, and the lane lock, open.
}

# opencode runs helper agents (session titles) on its own default small model,
# which can be a paid one; pin every opencode model to the lane's model so the
# free lane stays free and the subscription lane stays in its catalogue. The
# config travels in the environment: the runner writes nothing into space the
# agent can write, so no link an agent leaves there can redirect it.
opencode_config() {
  # MODEL already passed the allowlist; refuse JSON-breaking characters here too.
  # shellcheck disable=SC1003 # matching a literal backslash
  case "$MODEL" in *'"'*|*'\'*) echo "refusing model with a quote or backslash" >&2; return 1 ;; esac
  AGENT_EXTRA_ENV="OPENCODE_CONFIG_CONTENT={\"model\": \"$MODEL\", \"small_model\": \"$MODEL\"}"
}

# git in an agent's tree, run by the runner: never run hooks or an fsmonitor
# the tree might name, whatever ended up in its config. (`timeout` cannot run
# a shell function, so the push spells the same options out.)
safe_git() {
  git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
}

fresh_agent_home() {
  rm -rf "$AGENT_HOME"
  mkdir -p "$AGENT_HOME/tmp"
}

run_agent() {
  local brief_text
  brief_text="$(cat "$1")"
  AGENT_EXTRA_ENV=""
  case "$LANE" in openrouter|opencode-go|anthropic) fresh_agent_home ;; esac
  case "$LANE" in
    codex) timeout "$AGENT_TIMEOUT" codex exec --sandbox workspace-write --model "$MODEL" "$brief_text" < /dev/null ;;
    claude) timeout "$AGENT_TIMEOUT" claude -p --model "$MODEL" "$brief_text" < /dev/null ;;
    openrouter) opencode_config && api_agent OPENROUTER_API_KEY opencode run -m "$MODEL" "$brief_text" ;;
    opencode-go) opencode_config && api_agent OPENCODE_API_KEY opencode run -m "$MODEL" "$brief_text" ;;
    anthropic) api_agent ANTHROPIC_API_KEY claude -p --model "$MODEL" "$brief_text" ;;
    *) echo "unknown lane $LANE" >&2; return 2 ;;
  esac
}

# The lane's CLI and, for API lanes, its key. Sets API_KEY.
lane_ready() {
  local cli key_ssm
  case "$LANE" in
    codex) cli=codex ;; claude|anthropic) cli=claude ;; openrouter|opencode-go) cli=opencode ;;
    *) READY_REASON="unknown lane"; return 1 ;;
  esac
  command -v "$cli" > /dev/null || { READY_REASON="$cli not on PATH"; return 1; }
  case "$LANE" in codex|claude) return 0 ;; esac
  command -v sandbox-exec > /dev/null || { READY_REASON="sandbox-exec not available"; return 1; }
  key_ssm="$(lane_setting KEY_SSM)"
  [ -n "$key_ssm" ] || { READY_REASON="$(lane_var KEY_SSM) is not set"; return 1; }
  API_KEY="$(timeout "$NET_TIMEOUT" aws ssm get-parameter ${LOOM_AWS_REGION:+--region "$LOOM_AWS_REGION"} \
    --name "$key_ssm" --with-decryption --query Parameter.Value --output text 2>/dev/null)"
  [ -n "$API_KEY" ] || { READY_REASON="no key readable at $(lane_var KEY_SSM)"; return 1; }
}

# Timeouts must be positive integers: `timeout 0` means no limit at all, and a
# wedged agent would then hold the lane lock forever.
for t in "AGENT_TIMEOUT:$AGENT_TIMEOUT:60" "NET_TIMEOUT:$NET_TIMEOUT:1"; do
  IFS=: read -r t_name t_val t_min <<< "$t"
  if ! [[ "$t_val" =~ ^[0-9]+$ ]] || [ "$t_val" -lt "$t_min" ]; then
    heartbeat false "LOOM_$t_name must be an integer >= $t_min, got '$t_val'"
    exit 0
  fi
done

AGENT_HOME="$LOOM_HOME/run/$LANE-home"
API_KEY=""
READY_REASON=""
if ! lane_ready; then
  heartbeat false "$READY_REASON"
  exit 0
fi
heartbeat true ""

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
  MODEL="$(field Model "$BRIEF" || true)"
  [ -n "$MODEL" ] || MODEL="$(lane_setting MODEL)"
  if ! [[ "$MODEL" =~ ^[A-Za-z0-9._:][A-Za-z0-9._:/-]*$ ]]; then
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
    opencode-go:*) park "$BRIEF" "model $MODEL is not an opencode-go model"; continue ;;
  esac
  if [[ "$REPO_OWNERS" != *" ${REPO%%/*} "* ]]; then
    park "$BRIEF" "owner ${REPO%%/*} is not in LOOM_REPO_OWNERS"; continue
  fi

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

  TREE="$WORKDIR/$TICKET"
  rm -rf "$TREE"
  echo "[$(date -u +%FT%TZ)] $TICKET start, lane $LANE" >> "$LOG"
  if ! { timeout "$NET_TIMEOUT" git clone -q --depth 50 ${BASE_ARGS[@]+"${BASE_ARGS[@]}"} "${CLONE_BASE}${REPO}.git" "$TREE" &&
         git -C "$TREE" checkout -q -b "$BRANCH"; } >> "$LOG" 2>&1; then
    park "$BRIEF" "clone or branch failed"; continue
  fi
  BASE="$(git -C "$TREE" rev-parse HEAD)"

  (cd "$TREE" && run_agent "$BRIEF") >> "$LOG" 2>&1
  STATUS=$?
  if [ "$STATUS" -ne 0 ]; then park "$BRIEF" "agent exited $STATUS"; continue; fi
  TITLE="$(sed -n 's/^# T[0-9]*: //p' "$BRIEF" | head -1)"
  # A sandboxed agent cannot write .git, so it leaves its work in the tree.
  if [ -n "$(safe_git status --porcelain)" ]; then
    if ! { safe_git checkout -q "$BRANCH" && safe_git add -A &&
           safe_git commit -qm "${TITLE:-$TICKET}"; } >> "$LOG" 2>&1; then
      park "$BRIEF" "could not commit the agent's changes"; continue
    fi
  fi
  if [ "$(safe_git rev-parse "$BRANCH")" = "$BASE" ]; then
    park "$BRIEF" "agent made no commit on $BRANCH"; continue
  fi
  if ! timeout "$NET_TIMEOUT" git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false push -q -u origin "$BRANCH" >> "$LOG" 2>&1; then
    park "$BRIEF" "push failed"; continue
  fi
  if ! timeout "$NET_TIMEOUT" gh pr create -R "$REPO" ${PR_BASE_ARGS[@]+"${PR_BASE_ARGS[@]}"} --head "$BRANCH" --title "${TITLE:-$TICKET}" \
       --body "$(pr_body "$BRIEF" "$TICKET")" >> "$LOG" 2>&1; then
    park "$BRIEF" "pr create failed; branch $BRANCH is pushed"; continue
  fi
  echo "[$(date -u +%FT%TZ)] $TICKET done" >> "$LOG"
  mv "$BRIEF" "$INBOX/done/"
done
