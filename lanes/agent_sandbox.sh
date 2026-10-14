# Loom agent sandbox, sourced by lanes/run_lane.sh (the API lanes) and
# lanes/rewrite_agent.sh (the station's triage rewriter): one seatbelt
# profile, one emptied agent environment and one opencode config for both.
#
# The caller sets AGENT_HOME (the scratch home), AGENT_TIMEOUT and
# NET_TIMEOUT (seconds), LANE, MODEL, SKILLS (the skill snapshot, or a path
# that does not exist), API_KEY and AGENT_EXTRA_ENV, and for the spark lane
# SPARK_URL. The agy lane's caller also defines agy_write_rules.
# shellcheck shell=bash disable=SC2153 # the caller sets LANE and SKILLS

# The decrypted value of the SSM parameter named $1, or nothing when it
# cannot be read. The value goes to stdout only: callers capture it.
ssm_value() {
  timeout "$NET_TIMEOUT" aws ssm get-parameter ${LOOM_AWS_REGION:+--region "$LOOM_AWS_REGION"} \
    --name "$1" --with-decryption --query Parameter.Value --output text 2> /dev/null
}

# Seatbelt profile for API-lane agents: no reads or writes anywhere under the
# real HOME except the worktree, the scratch home and the directory holding
# the CLI's binary, plus read-only access to the skill snapshot. Inside the
# worktree, .git stays readable (git diff and git log need it; a fresh clone
# holds no credentials) but not writable: a planted
# hook or config would run when the runner commits. Also no keychain, no Unix
# sockets except DNS (an SSH agent's socket would hand over its keys), and no
# AppleEvents, LaunchServices, osascript, open, launchctl, sudo or su (each can
# run a command outside the sandbox or as another user), and no connections to this machine's own
# loopback services (the transcript viewer serves other lanes' sessions). Paths are canonical, because the sandbox
# matches resolved paths; proper ancestors get metadata-only literals so
# getcwd and realpath still work without making HOME listable.
sandbox_profile() {
  # The agy lane reaches the keychain server: it runs as the lane user, whose
  # only keychain is its own. Every other lane is denied it, and so is a
  # validation run ($3 = check), whichever lane it serves.
  local keychain_deny='(global-name "com.apple.SecurityServer") ' agy_lane=""
  [ "$LANE" = agy ] && [ "${3:-}" != check ] && { keychain_deny=""; agy_lane=1; }
  local home tree cli_link cli_real cli_root p allow_rw allow_ro meta="" agy_rules="" skills="" skills_rules=""
  home="$(cd "$HOME" && pwd -P)"
  tree="$(cd "$1" && pwd -P)"
  [ -d "$SKILLS" ] && skills="$(cd "$SKILLS" && pwd -P)"
  cli_link="$(command -v "$2")" && [ -n "$cli_link" ] || { echo "refusing sandbox: $2 not on PATH" >&2; return 1; }
  cli_real="$(realpath "$cli_link")" && [ -n "$cli_real" ] || { echo "refusing sandbox: cannot resolve $cli_link" >&2; return 1; }
  cli_root="$(dirname "$cli_real")"
  # Paths go into the profile's string grammar: refuse any that could close it.
  for p in "$home" "$tree" "$cli_link" "$cli_real" "$AGENT_HOME" "$skills"; do
    # shellcheck disable=SC1003 # matching a literal backslash
    case "$p" in *'"'*|*'\'*) echo "refusing sandbox: unsafe path $p" >&2; return 1 ;; esac
  done
  # Never let the CLI's directory cover HOME itself.
  case "$home/" in "$cli_root"/*) echo "refusing sandbox: $cli_real sits at or above HOME" >&2; return 1 ;; esac
  allow_rw="(subpath \"$tree\") (subpath \"$(cd "$AGENT_HOME" && pwd -P)\")"
  allow_ro="(subpath \"$cli_root\") (literal \"$cli_link\")"
  # The skill snapshot: readable, never writable, wherever LOOM_HOME lives.
  [ -n "$skills" ] && skills_rules="(allow file-read* (subpath \"$skills\"))
(deny file-write* (subpath \"$skills\"))
"
  for p in "$tree" "$AGENT_HOME" "$cli_root" "$cli_link" ${skills:+"$skills"}; do
    p="$(dirname "$p")"
    while [ "${p#"$home"}" != "$p" ] && [ "$p" != "/" ]; do
      meta="$meta (literal \"$p\")"
      [ "$p" = "$home" ] && break
      p="$(dirname "$p")"
    done
  done
  [ -n "$agy_lane" ] && agy_rules="$(agy_write_rules "$allow_rw")"
  printf '(version 1)\n(allow default)\n(deny file-read* file-write* (subpath "%s"))\n(allow file-read* file-write* %s)\n%s(deny file-write* (subpath "%s/.git"))\n(allow file-read* %s)\n%s(allow file-read-metadata%s)\n(deny mach-lookup %s(global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.coreservices.appleevents"))\n(deny appleevent-send)\n(deny process-exec (literal "/usr/bin/osascript") (literal "/usr/bin/open") (literal "/bin/launchctl") (literal "/usr/bin/sudo") (literal "/usr/bin/su"))\n(deny network-outbound (remote unix-socket))\n(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))\n(deny network-outbound (remote ip "localhost:*"))\n' \
    "$home" "$allow_rw" "$agy_rules" "$tree" "$allow_ro" "$skills_rules" "$meta" "$keychain_deny"
}

# API lanes run the agent with an emptied environment (its provider key, a
# scratch HOME and TMPDIR, PATH) inside the sandbox above. A key_var of "-"
# means the provider needs no key (OpenCode Zen's free models). The key is
# exported, never put in argv, so `ps` cannot show it. The agent can edit its
# tree but cannot read credentials, push, open, or merge; the runner does that.
# Its stdin is AGENT_STDIN, a file the caller wrote, else /dev/null.
api_agent() {
  local key_var="$1" cli="$2" profile
  mkdir -p "$AGENT_HOME/tmp"
  profile="$(sandbox_profile "$PWD" "$cli")" || return 3
  (
    keep_path="$PATH" keep_lang="${LANG:-en_US.UTF-8}" key="$API_KEY" extra="$AGENT_EXTRA_ENV"
    for v in $(compgen -e); do unset "$v"; done
    [ -z "$(compgen -e)" ] || { echo "could not clear the agent environment: $(compgen -e | tr '\n' ' ')" >&2; exit 3; }
    # shellcheck disable=SC2030 # deliberately local to the agent's subshell
    export PATH="$keep_path" HOME="$AGENT_HOME" TMPDIR="$AGENT_HOME/tmp" \
      TMP="$AGENT_HOME/tmp" TEMP="$AGENT_HOME/tmp" TERM=dumb LANG="$keep_lang"
    [ "$key_var" = - ] || export "$key_var=$key"
    # AGENT_EXTRA_ENV: NAME=value lines (opencode config, a provider's account id).
    while IFS= read -r kv; do [ -z "$kv" ] || export "${kv?}"; done <<< "$extra"
    exec timeout "$AGENT_TIMEOUT" sandbox-exec -p "$profile" "${@:2}" < "${AGENT_STDIN:-/dev/null}"
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
# OPENCODE_PERMISSION, a JSON object, replaces the lanes' permission rules
# (only paths outside the tree denied); the triage rewriter denies every tool.
opencode_config() {
  # MODEL already passed the allowlist; refuse JSON-breaking characters here too.
  # shellcheck disable=SC1003 # matching a literal backslash
  case "$MODEL" in *'"'*|*'\'*) echo "refusing model with a quote or backslash" >&2; return 1 ;; esac
  # The spark lane's provider is the local endpoint, declared here: an
  # OpenAI-compatible provider named spark at LOOM_SPARK_URL (checked in
  # spark_ready), serving the one model after "spark/". No key.
  local provider="" id="${MODEL#spark/}" permission="${OPENCODE_PERMISSION:-}"
  [ -n "$permission" ] || permission='{"external_directory": "deny"}'
  [ "$LANE" = spark ] && provider=", \"provider\": {\"spark\": {\"npm\": \"@ai-sdk/openai-compatible\", \"name\": \"Spark vLLM\", \"options\": {\"baseURL\": \"$SPARK_URL\"}, \"models\": {\"$id\": {\"name\": \"$id\", \"limit\": {\"context\": 32768, \"output\": 4096}}}}}"
  AGENT_EXTRA_ENV="${AGENT_EXTRA_ENV:+$AGENT_EXTRA_ENV
}OPENCODE_CONFIG_CONTENT={\"model\": \"$MODEL\", \"small_model\": \"$MODEL\", \"permission\": $permission$provider}"
}

# opencode 2.x `run` talks to a shared background service unless told
# --standalone; the sandbox denies localhost, and a shared service would mix
# lanes' sessions. --auto lets it act without prompting, as 1.x run does.
# Usage: opencode_run <key_var> <opencode binary> <run arguments...>
opencode_run() {
  local key_var="$1" bin="$2" flags=()
  shift 2
  case "$(timeout "$NET_TIMEOUT" "$bin" --version < /dev/null 2> /dev/null | head -n 1)" in
    "opencode v2."*) flags=(--standalone --auto) ;;
  esac
  opencode_config && api_agent "$key_var" "$bin" run ${flags[@]+"${flags[@]}"} -m "$MODEL" "$@"
}
