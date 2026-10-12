#!/usr/bin/env bash
# Loom lane runner: one pass over a lane's inbox, one brief at a time.
#
#   run_lane.sh <lane>   lane = codex | claude | openrouter | opencode-go | anthropic
#                               | zen | zai | workersai | agy
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
# provider key, read at run time; never stored in this repo; zen needs none),
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
  local size issue refs=""
  issue="$(field Issue "$1" || true)"
  [[ "$issue" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+$ ]] && refs="$(printf '\nRefs %s' "$issue")"
  size="$(section Size "$1" | head -1 | awk '{print $1}' | tr -d ':')"
  printf '## Why\n%s\n\n## Summary\n%s\n\n## Acceptance criteria\n%s\n\nSize: %s\n\n## Out of scope\n%s\n\n---\nLoom ticket %s, lane %s, model %s.%s\n' \
    "$(section Why "$1")" "$(section What "$1")" "$(section 'Acceptance criteria' "$1")" \
    "$size" "$(section 'Out of scope' "$1")" "$2" "$LANE" "$MODEL" "$refs"
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
# AppleEvents, LaunchServices, osascript, open, launchctl, sudo or su (each can
# run a command outside the sandbox or as another user), and no connections to this machine's own
# loopback services (the transcript viewer serves other lanes' sessions). Paths are canonical, because the sandbox
# matches resolved paths; proper ancestors get metadata-only literals so
# getcwd and realpath still work without making HOME listable.
sandbox_profile() {
  # The agy lane reaches the keychain server: it runs as the lane user, whose
  # only keychain is its own. Every other lane is denied it.
  local keychain_deny='(global-name "com.apple.SecurityServer") '
  [ "$LANE" = agy ] && keychain_deny=""
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
  printf '(version 1)\n(allow default)\n(deny file-read* file-write* (subpath "%s"))\n(allow file-read* file-write* %s)\n%s(deny file-write* (subpath "%s/.git"))\n(allow file-read* %s)\n(allow file-read-metadata%s)\n(deny mach-lookup %s(global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.coreservices.appleevents"))\n(deny appleevent-send)\n(deny process-exec (literal "/usr/bin/osascript") (literal "/usr/bin/open") (literal "/bin/launchctl") (literal "/usr/bin/sudo") (literal "/usr/bin/su"))\n(deny network-outbound (remote unix-socket))\n(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))\n(deny network-outbound (remote ip "localhost:*"))\n' \
    "$home" "$allow_rw" "$(agy_write_rules "$allow_rw")" "$tree" "$allow_ro" "$meta" "$keychain_deny"
}

# API lanes run the agent with an emptied environment (its provider key, a
# scratch HOME and TMPDIR, PATH) inside the sandbox above. A key_var of "-"
# means the provider needs no key (OpenCode Zen's free models). The key is
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
    # shellcheck disable=SC2030 # deliberately local to the agent's subshell
    export PATH="$keep_path" HOME="$AGENT_HOME" TMPDIR="$AGENT_HOME/tmp" TERM=dumb LANG="$keep_lang"
    [ "$key_var" = - ] || export "$key_var=$key"
    # AGENT_EXTRA_ENV: NAME=value lines (opencode config, a provider's account id).
    while IFS= read -r kv; do [ -z "$kv" ] || export "${kv?}"; done <<< "$extra"
    exec timeout "$AGENT_TIMEOUT" sandbox-exec -p "$profile" "${@:2}" < /dev/null
  ) 2>&1 | timeout "$((AGENT_TIMEOUT + 60))" cat
  # The agent writes into a pipe, not the log file: the log lives under HOME,
  # where the sandbox denies even fstat, and CLIs fstat their output at start.
  # pipefail carries the agent's exit status through `cat`. The reader has its
  # own deadline: a process the agent detached into another process group
  # survives `timeout` and would hold the pipe, and the lane lock, open.
}

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

# agy stores its token so that /usr/bin/security may read it without a
# prompt, and agy itself reads it that way, so the sandbox cannot forbid that
# tool. Any shell command the agent ran could therefore copy the token out.
# The lane stays edits-only (never --dangerously-skip-permissions): print mode
# soft-denies shell commands, and a denied command ends the run with no
# output, so the brief says so up front. Slash commands and skills stay off.
AGY_TOOLS_NOTE="Tools on this lane: shell commands are not available here, and asking for one ends the run. Read files with your file viewing and search tools, and change them with your edit tools only."

# The agy lane: unlock the lane user's keychain (password on stdin to a shell
# running as that user, never in this process's argv), then run agy as that
# user in an emptied environment inside the sandbox, edits only. The runner,
# as this user, commits, pushes and opens the PR from the shared tree.
agy_agent() {
  local profile
  AGENT_HOME="$AGY_HOME/tmp"
  "${AGY_RUNAS[@]}" mkdir -p "$AGENT_HOME" || return 3
  agy_forget || { echo "could not clear agy's memory of earlier runs" >&2; return 3; }
  profile="$(sandbox_profile "$PWD" "$AGY_BIN")" || return 3
  # shellcheck disable=SC2031 # PATH and LANG here are this shell's, not api_agent's subshell's
  "${AGY_RUNAS[@]}" /usr/bin/env -i PATH="$PATH" HOME="$AGY_HOME" TMPDIR="$AGENT_HOME" TERM=dumb \
    LANG="${LANG:-en_US.UTF-8}" timeout "$AGENT_TIMEOUT" sandbox-exec -p "$profile" \
    "$AGY_BIN" -p "$1

$AGY_TOOLS_NOTE" --mode accept-edits --disable-slash-commands --model "$MODEL" < /dev/null 2>&1 | timeout "$((AGENT_TIMEOUT + 60))" cat
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
  AGENT_EXTRA_ENV="${AGENT_EXTRA_ENV:+$AGENT_EXTRA_ENV
}OPENCODE_CONFIG_CONTENT={\"model\": \"$MODEL\", \"small_model\": \"$MODEL\"}"
}

# git in an agent's tree, run by the runner: never run hooks or an fsmonitor
# the tree might name, whatever ended up in its config. (`timeout` cannot run
# a shell function, so the push spells the same options out.)
safe_git() {
  git -C "$TREE" -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
}

# Keep the agent's session transcripts (Claude Code .jsonl; opencode's SQLite
# session store and older session json) in logs/<lane>/<ticket>/ before the next run wipes its scratch home.
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
          --include='opencode.db-shm' --exclude='*' "$leaf" "$dest/$parent/" >> "$LOG" 2>&1; then
        echo "[$(date -u +%FT%TZ)] $TICKET: archiving $parent/$leaf failed; transcript may be incomplete" >> "$LOG"
      fi
    )
  done
  return 0
}

# A fresh scratch home for every API-lane run. The agent can write inside it,
# so anything left there (a link into the real HOME, say) must never meet the
# runner, which is not sandboxed. rm -rf removes links without following them.
fresh_agent_home() {
  rm -rf "$AGENT_HOME"
  mkdir -p "$AGENT_HOME/tmp"
}

# Lanes and models whose provider may train on prompts (docs/LANES.md lists
# them). Their briefs must name a public repo.
trains_on_prompts() {
  case "$LANE:$MODEL" in zen:*|zai:*|agy:*|opencode-go:*muse-spark*contributor*) return 0 ;; esac
  return 1
}

# Lanes whose agent runs sandboxed in a fresh scratch home (api_agent).
is_api_lane() {
  case "$LANE" in openrouter|opencode-go|anthropic|zen|zai|workersai) return 0 ;; esac
  return 1
}

run_agent() {
  local brief_text
  brief_text="$(cat "$1")"
  AGENT_EXTRA_ENV=""
  is_api_lane && fresh_agent_home
  case "$LANE" in
    codex) timeout "$AGENT_TIMEOUT" codex exec --sandbox workspace-write --model "$MODEL" "$brief_text" < /dev/null ;;
    claude) timeout "$AGENT_TIMEOUT" claude -p --model "$MODEL" "$brief_text" < /dev/null ;;
    openrouter) opencode_config && api_agent OPENROUTER_API_KEY opencode run -m "$MODEL" "$brief_text" ;;
    opencode-go) opencode_config && api_agent OPENCODE_API_KEY opencode run -m "$MODEL" "$brief_text" ;;
    zen) opencode_config && api_agent - opencode run -m "$MODEL" "$brief_text" ;;
    zai) opencode_config && api_agent ZHIPU_API_KEY opencode run -m "$MODEL" "$brief_text" ;;
    workersai) AGENT_EXTRA_ENV="CLOUDFLARE_ACCOUNT_ID=$LANE_ACCOUNT"
      opencode_config && api_agent CLOUDFLARE_API_KEY opencode run -m "$MODEL" "$brief_text" ;;
    anthropic) api_agent ANTHROPIC_API_KEY claude -p --model "$MODEL" "$brief_text" ;;
    agy) agy_agent "$brief_text" ;;
    *) echo "unknown lane $LANE" >&2; return 2 ;;
  esac
}

# The lane's CLI and, for API lanes, its key. Sets API_KEY.
lane_ready() {
  local cli key_ssm
  if [ "$LANE" = agy ]; then
    command -v sandbox-exec > /dev/null || { READY_REASON="sandbox-exec not available"; return 1; }
    "${AGY_RUNAS[@]}" test -x "$AGY_BIN" > /dev/null 2>&1 ||
      { READY_REASON="cannot run $AGY_BIN as $AGY_USER"; return 1; }
    agy_key || return 1
    agy_unlock
    return
  fi
  case "$LANE" in
    codex) cli=codex ;; claude|anthropic) cli=claude ;; openrouter|opencode-go|zen|zai|workersai) cli=opencode ;;
    *) READY_REASON="unknown lane"; return 1 ;;
  esac
  command -v "$cli" > /dev/null || { READY_REASON="$cli not on PATH"; return 1; }
  case "$LANE" in codex|claude) return 0 ;; esac
  command -v sandbox-exec > /dev/null || { READY_REASON="sandbox-exec not available"; return 1; }
  case "$LANE" in zen) return 0 ;; esac
  key_ssm="$(lane_setting KEY_SSM)"
  [ -n "$key_ssm" ] || { READY_REASON="$(lane_var KEY_SSM) is not set"; return 1; }
  API_KEY="$(timeout "$NET_TIMEOUT" aws ssm get-parameter ${LOOM_AWS_REGION:+--region "$LOOM_AWS_REGION"} \
    --name "$key_ssm" --with-decryption --query Parameter.Value --output text 2>/dev/null)"
  [ -n "$API_KEY" ] || { READY_REASON="no key readable at $(lane_var KEY_SSM)"; return 1; }
  lane_extra_ready
}

# The agy lane's "key" is its keychain password (LOOM_AGY_KEY_SSM).
agy_key() {
  local key_ssm
  key_ssm="$(lane_setting KEY_SSM)"
  [ -n "$key_ssm" ] || { READY_REASON="$(lane_var KEY_SSM) is not set"; return 1; }
  API_KEY="$(timeout "$NET_TIMEOUT" aws ssm get-parameter ${LOOM_AWS_REGION:+--region "$LOOM_AWS_REGION"} \
    --name "$key_ssm" --with-decryption --query Parameter.Value --output text 2>/dev/null)"
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
    LANE_ACCOUNT="$(timeout "$NET_TIMEOUT" aws ssm get-parameter ${LOOM_AWS_REGION:+--region "$LOOM_AWS_REGION"} \
      --name "$account_ssm" --with-decryption --query Parameter.Value --output text 2>/dev/null)"
    [[ "$LANE_ACCOUNT" =~ ^[A-Za-z0-9]+$ ]] || { READY_REASON="no account id readable at $(lane_var ACCOUNT_SSM)"; return 1; }
  fi
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
LANE_ACCOUNT=""
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
    opencode-go:*) park "$BRIEF" "model $MODEL is not an opencode-go model"; continue ;;
    zen:opencode/*-free) ;;
    zen:*) park "$BRIEF" "zen lane runs free models only (opencode/...-free), not $MODEL"; continue ;;
    zai:zai/glm-4.7-flash|zai:zai/glm-4.5-flash) ;;
    zai:*) park "$BRIEF" "zai lane runs the free Flash models only (zai/glm-4.7-flash, zai/glm-4.5-flash), not $MODEL"; continue ;;
    workersai:cloudflare-workers-ai/@cf/*) ;;
    agy:gemini-*|agy:claude-*|agy:gpt-oss-*) ;;
    agy:*) park "$BRIEF" "model $MODEL is not in the agy catalog (gemini-*, claude-*, gpt-oss-*)"; continue ;;
    workersai:*) park "$BRIEF" "workersai lane runs Cloudflare Workers AI models only (cloudflare-workers-ai/@cf/...), not $MODEL"; continue ;;
  esac
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
  is_api_lane && archive_transcripts
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
