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

# The ports an API-lane agent may connect to, space-separated: 443, its
# provider's HTTPS API (and the CLI's own catalogue and package fetches, and
# GitHub for the brain's spark rung), plus on the spark lane the port of
# SPARK_URL (its scheme's default when the URL names none). A spark URL with
# no usable port is refused, which refuses the sandbox.
provider_ports() {
  local rest port=""
  [ "$LANE" = spark ] || { printf '443'; return 0; }
  case "$SPARK_URL" in
    https://*) port=443 ;;
    http://*) port=80 ;;
  esac
  rest="${SPARK_URL#*://}"
  rest="${rest%%/*}"
  if [ -n "$port" ] && [ "${rest%:*}" != "$rest" ]; then port="${rest##*:}"; fi
  [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$port" -le 65535 ] ||
    { echo "refusing sandbox: no usable port in the spark URL $SPARK_URL" >&2; return 1; }
  [ "$port" = 443 ] && printf '443' || printf '443 %s' "$port"
}

# The rules sandbox_profile adds in api mode, in two parts ($1) the profile
# places apart. Writes: none anywhere but the device
# nodes every CLI opens, before the profile allows the tree and the scratch
# home (later rules win). Network: no outbound connection to any port except
# provider_ports. Seatbelt names no host but * and localhost, and once a *
# rule is present a later localhost rule decides its port for every host, so
# the provider's port stays open on loopback too; every other loopback port,
# the transcript viewer's included, stays closed.
# Usage: api_rules writes|network
api_rules() {
  local p ports
  case "$1" in
    writes) printf '(deny file-write* (subpath "/"))\n(allow file-write* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/dtracehelper") (regex #"^/dev/fd/[0-9]+$"))\n' ;;
    network)
      ports="$(provider_ports)" || return 1
      printf '(deny network-outbound (remote ip "*:*"))\n'
      for p in $ports; do printf '(allow network-outbound (remote ip "*:%s"))\n' "$p"; done ;;
    *) echo "api_rules: unknown part $1" >&2; return 1 ;;
  esac
}

# Seatbelt profile for agents, in one of three modes ($3): api for every API
# lane, the triage rewriter and the brain's spark rung; empty for the agy
# lane; check for a validation run. In every mode: no reads or writes anywhere
# under the real HOME except the worktree, the scratch home and the directory
# holding the CLI's binary, plus read-only access to the skill snapshot. In
# api mode also no writes anywhere outside the worktree and the scratch home,
# and outbound connections only to the provider's port (api_rules); the agy
# lane and a validation run keep the network and the writes outside HOME.
# Inside the worktree, .git stays readable (git diff and git log need it; a
# fresh clone holds no credentials) but not writable: a planted hook or config
# would run when the runner commits. Also no keychain, no Unix sockets except
# DNS (an SSH agent's socket would hand over its keys), and no AppleEvents,
# LaunchServices, osascript, open, launchctl, sudo or su (each can run a
# command outside the sandbox or as another user), and no connections to this
# machine's own loopback services (the transcript viewer serves other lanes'
# sessions), except in api mode on the provider's port. Paths are canonical,
# because the sandbox matches resolved paths; proper ancestors get
# metadata-only literals so getcwd and realpath still work without making
# HOME listable.
sandbox_profile() {
  # The agy lane reaches the keychain server: it runs as the lane user, whose
  # only keychain is its own. Every other lane is denied it, and so is a
  # validation run ($3 = check), whichever lane it serves.
  local keychain_deny='(global-name "com.apple.SecurityServer") ' agy_lane="" api_writes="" api_network=""
  case "${3:-}" in
    "" | check) ;;
    api) api_writes="$(api_rules writes)"$'\n' && api_network="$(api_rules network)"$'\n' || return 1 ;;
    *) echo "refusing sandbox: unknown mode $3" >&2; return 1 ;;
  esac
  [ "$LANE" = agy ] && [ "${3:-}" != check ] && { keychain_deny=""; agy_lane=1; }
  local home tree cli_link cli_real cli_root p allow_rw allow_ro meta="" agy_rules="" skills="" skills_rules="" loopback_rule=""
  if [ -n "${4:-}" ]; then
    [[ "$4" =~ ^[0-9]+$ ]] && [ "$4" -ge 49152 ] && [ "$4" -le 65535 ] ||
      { echo "refusing sandbox: loopback port $4 is not in 49152-65535" >&2; return 1; }
    loopback_rule="(allow network-outbound (remote ip \"localhost:$4\"))"
  fi
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
  printf '(version 1)\n(allow default)\n%s(deny file-read* file-write* (subpath "%s"))\n(allow file-read* file-write* %s)\n%s(deny file-write* (subpath "%s/.git"))\n(allow file-read* %s)\n%s(allow file-read-metadata%s)\n(deny mach-lookup %s(global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.coreservices.appleevents"))\n(deny appleevent-send)\n(deny process-exec (literal "/usr/bin/osascript") (literal "/usr/bin/open") (literal "/bin/launchctl") (literal "/usr/bin/sudo") (literal "/usr/bin/su"))\n(deny network-outbound (remote unix-socket))\n(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))\n(deny network-outbound (remote ip "localhost:*"))\n%s%s\n' \
    "$api_writes" "$home" "$allow_rw" "$agy_rules" "$tree" "$allow_ro" "$skills_rules" "$meta" "$keychain_deny" "$api_network" "$loopback_rule"
}

# API lanes run the agent with an emptied environment (its provider key, a
# scratch HOME and TMPDIR, PATH) inside the sandbox above, in api mode. A
# key_var of "-" means the provider needs no key (OpenCode Zen's free models). The key is
# exported, never put in argv, so `ps` cannot show it. The agent can edit its
# tree but cannot read credentials, push, open, or merge; the runner does that.
# Its stdin is AGENT_STDIN, a file the caller wrote, else /dev/null.
# With OPENCODE_SERVER_PORT set (opencode 2.x), the sandbox allows that one
# loopback port, `$cli serve` runs on it under the same profile with a
# password made for this run, and the agent command (which names the same
# port) runs against it. The server is stopped when the subshell ends,
# whatever the agent's outcome. A port something already listens on is
# refused before the server starts: the readiness probe below would take that
# listener for the server, and the agent would talk to another process.
api_agent() {
  local key_var="$1" cli="$2" profile
  if [ -n "${OPENCODE_SERVER_PORT:-}" ] &&
    /usr/bin/nc -z -w 1 127.0.0.1 "$OPENCODE_SERVER_PORT" > /dev/null 2>&1; then
    echo "loopback port $OPENCODE_SERVER_PORT is already in use; not starting the opencode server on it" >&2
    return 3
  fi
  mkdir -p "$AGENT_HOME/tmp"
  profile="$(sandbox_profile "$PWD" "$cli" api "${OPENCODE_SERVER_PORT:-}")" || return 3
  (
    keep_path="$PATH" keep_lang="${LANG:-en_US.UTF-8}" key="$API_KEY" extra="$AGENT_EXTRA_ENV" server_port="${OPENCODE_SERVER_PORT:-}"
    for v in $(compgen -e); do unset "$v"; done
    [ -z "$(compgen -e)" ] || { echo "could not clear the agent environment: $(compgen -e | tr '\n' ' ')" >&2; exit 3; }
    # shellcheck disable=SC2030 # deliberately local to the agent's subshell
    export PATH="$keep_path" HOME="$AGENT_HOME" TMPDIR="$AGENT_HOME/tmp" \
      TMP="$AGENT_HOME/tmp" TEMP="$AGENT_HOME/tmp" TERM=dumb LANG="$keep_lang"
    [ "$key_var" = - ] || export "$key_var=$key"
    # AGENT_EXTRA_ENV: NAME=value lines (opencode config, a provider's account id).
    while IFS= read -r kv; do [ -z "$kv" ] || export "${kv?}"; done <<< "$extra"
    if [ -n "$server_port" ]; then
      OPENCODE_PASSWORD="$(openssl rand -hex 32)" || exit 3
      export OPENCODE_PASSWORD
      sandbox-exec -p "$profile" "$cli" serve --hostname 127.0.0.1 \
        --port "$server_port" > /dev/null 2>&1 < /dev/null &
      server_pid=$!
      trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true' EXIT
      trap 'exit 143' TERM
      trap 'exit 130' INT
      trap 'exit 129' HUP
      ready=""
      deadline=$((SECONDS + 15))
      while [ "$SECONDS" -lt "$deadline" ]; do
        kill -0 "$server_pid" 2>/dev/null || { echo "opencode server stopped before it was ready" >&2; exit 3; }
        if sandbox-exec -p "$profile" /usr/bin/nc -z -w 1 127.0.0.1 "$server_port" \
          > /dev/null 2>&1; then ready=1; break; fi
        sleep 0.1
      done
      [ -n "$ready" ] || { echo "opencode server was not ready within 15 seconds" >&2; exit 3; }
      timeout -k 5 "$AGENT_TIMEOUT" sandbox-exec -p "$profile" "${@:2}" < "${AGENT_STDIN:-/dev/null}"
    else
      exec timeout "$AGENT_TIMEOUT" sandbox-exec -p "$profile" "${@:2}" < "${AGENT_STDIN:-/dev/null}"
    fi
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
  # The free lane (a free provider's route on the spark-gateway) is declared the
  # same way, as provider free serving the model after "free/".
  local provider="" id="${MODEL#*/}" permission="${OPENCODE_PERMISSION:-}"
  [ -n "$permission" ] || permission='{"external_directory": "deny"}'
  # The window the endpoint serves (LOOM_SPARK_CONTEXT, LOOM_SPARK_OUTPUT;
  # whole numbers, else the vLLM defaults below). A window smaller than the
  # server's makes the agent compact and lose its place on a large repo: the
  # first sparkles run (2026-10-14) re-read files 190 times in 32K and
  # committed nothing, against a server serving 262144.
  local ctx="${LOOM_SPARK_CONTEXT:-32768}" out="${LOOM_SPARK_OUTPUT:-4096}" v
  # A lane or slot may serve its own window (the free lane's providers serve
  # about 128K beside a Spark's 262144):
  # LOOM_<SLOT>_CONTEXT and LOOM_<SLOT>_OUTPUT come first.
  if declare -F lane_setting > /dev/null; then
    v="$(lane_setting CONTEXT)"; [ -n "$v" ] && ctx="$v"
    v="$(lane_setting OUTPUT)"; [ -n "$v" ] && out="$v"
  fi
  [[ "$ctx" =~ ^[1-9][0-9]{3,6}$ ]] || ctx=32768
  [[ "$out" =~ ^[1-9][0-9]{2,5}$ ]] || out=4096
  [ "$LANE" = spark ] && provider=", \"provider\": {\"spark\": {\"npm\": \"@ai-sdk/openai-compatible\", \"name\": \"Spark vLLM\", \"options\": {\"baseURL\": \"$SPARK_URL\"}, \"models\": {\"$id\": {\"name\": \"$id\", \"limit\": {\"context\": $ctx, \"output\": $out}}}}}"
  [ "$LANE" = free ] && provider=", \"provider\": {\"free\": {\"npm\": \"@ai-sdk/openai-compatible\", \"name\": \"Free provider pool\", \"options\": {\"baseURL\": \"$SPARK_URL\"}, \"models\": {\"$id\": {\"name\": \"$id\", \"limit\": {\"context\": $ctx, \"output\": $out}}}}}"
  AGENT_EXTRA_ENV="${AGENT_EXTRA_ENV:+$AGENT_EXTRA_ENV
}OPENCODE_CONFIG_CONTENT={\"model\": \"$MODEL\", \"small_model\": \"$MODEL\", \"permission\": $permission$provider}"
}

# A random port in 49152-65535 that nothing on this machine holds: each
# candidate must bind on 127.0.0.1, so a port the transcript viewer, another
# lane or another slot listens on is skipped. The probe socket closes before
# the server starts; api_agent refuses the port if something took it since.
# Prints the port, or says on stderr why there is none and returns 1: a
# missing interpreter, every candidate in use, or output that is not a port.
free_loopback_port() {
  local port
  port="$(python3 -c 'import secrets, socket, sys
for _ in range(64):
    port = 49152 + secrets.randbelow(16384)
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
    print(port)
    sys.exit(0)
sys.exit("all 64 ports tried were in use")' 2>&1)" ||
    { echo "no free loopback port for the opencode server: ${port:-python3 failed}" >&2; return 1; }
  [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 49152 ] && [ "$port" -le 65535 ] ||
    { echo "the port picker printed '$port', not a port in 49152-65535" >&2; return 1; }
  printf '%s' "$port"
}

# opencode 2.x `run` talks to a shared background service unless pointed at a
# server; --standalone spawns one on a random loopback port, which the sandbox
# denies. So the run gets its own server on one allowed port (see api_agent).
# --auto lets it act without prompting, as 1.x run does.
# The port comes from free_loopback_port; when there is none, the run parks
# with the reason in the log and no server or agent starts.
# Usage: opencode_run <key_var> <opencode binary> <run arguments...>
opencode_run() {
  local key_var="$1" bin="$2" port
  shift 2
  opencode_config || return 1
  case "$(timeout "$NET_TIMEOUT" "$bin" --version < /dev/null 2> /dev/null | head -n 1)" in
    "opencode v2."*)
      port="$(free_loopback_port)" || return 3
      OPENCODE_SERVER_PORT="$port" api_agent "$key_var" "$bin" run \
        --server "http://127.0.0.1:$port" --auto -m "$MODEL" "$@"
      return $? ;;
  esac
  api_agent "$key_var" "$bin" run -m "$MODEL" "$@"
}
