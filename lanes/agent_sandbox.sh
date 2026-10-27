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
# With LOOM_SSM_DIR set (a lane pod), the value is the file at the parameter's
# name under that folder (/a/b -> $LOOM_SSM_DIR/a/b), a copy of the parameter
# that the cluster keeps in step with SSM, and no AWS call is made: the pod
# holds no AWS key. The folder must sit under HOME, which no sandbox shows.
ssm_value() {
  if [ -n "${LOOM_SSM_DIR:-}" ]; then
    case "$1" in /*) ;; *) return 1 ;; esac
    case "/$1/" in */../*|*/./*) return 1 ;; esac
    cat "$LOOM_SSM_DIR$1" 2> /dev/null
    return
  fi
  timeout "$NET_TIMEOUT" aws ssm get-parameter ${LOOM_AWS_REGION:+--region "$LOOM_AWS_REGION"} \
    --name "$1" --with-decryption --query Parameter.Value --output text 2> /dev/null
}

# The sandbox that confines agents and validation runs: LOOM_SANDBOX=seatbelt
# (macOS sandbox-exec, the default) or bwrap (Linux bubblewrap, for a lane
# pod on the cluster). Prints the kind, or says why not and returns 1.
sandbox_kind() {
  case "${LOOM_SANDBOX:-seatbelt}" in
    seatbelt | bwrap) printf '%s' "${LOOM_SANDBOX:-seatbelt}" ;;
    *) echo "unknown LOOM_SANDBOX '${LOOM_SANDBOX}': seatbelt or bwrap" >&2; return 1 ;;
  esac
}

# Whether this machine can start the sandbox. SANDBOX_WHY says why not. A
# bwrap that cannot make its namespaces here (a container without user
# namespaces, or with a masked /proc) is found before any run, so the lane
# says it is unavailable instead of failing every brief.
# bwrap narrows no network, so it also fails closed unless the pod's own
# egress is narrowed, proved before every pass by egress_denied.
# shellcheck disable=SC2034 # SANDBOX_WHY is read by the callers
sandbox_ready() {
  local kind
  SANDBOX_WHY=""
  kind="$(sandbox_kind 2>&1)" || { SANDBOX_WHY="$kind"; return 1; }
  case "$kind" in
    seatbelt) command -v sandbox-exec > /dev/null || { SANDBOX_WHY="sandbox-exec not available"; return 1; } ;;
    bwrap)
      command -v bwrap > /dev/null || { SANDBOX_WHY="bwrap not available"; return 1; }
      bwrap --die-with-parent --unshare-pid --ro-bind / / --dev /dev --proc /proc true > /dev/null 2>&1 ||
        { SANDBOX_WHY="bwrap cannot start a sandbox here (user namespaces or /proc denied)"; return 1; }
      egress_denied || return 1 ;;
  esac
}

# Whether the pod's egress is narrowed, proved, not assumed. Every
# must-be-blocked destination must fail to connect: those in
# LOOM_EGRESS_DENY_PROBES (IPv4:port, literal addresses only, so a name that
# does not resolve can never pass as blocked; separated by spaces or commas,
# as the cluster's secret holds them), plus the API server at
# KUBERNETES_SERVICE_HOST:KUBERNETES_SERVICE_PORT when the pod has them. And
# the positive control LOOM_EGRESS_ALLOW_PROBE (host:port, the provider's or
# gateway's HTTPS endpoint) must connect, so a pod with no network or no DNS
# does not pass because every connection fails. Each try takes at most
# LOOM_EGRESS_PROBE_SECONDS (default 3). Sets SANDBOX_WHY when it fails.
# shellcheck disable=SC2034 # SANDBOX_WHY is read by sandbox_ready's callers
egress_denied() {
  local target host port wait="${LOOM_EGRESS_PROBE_SECONDS:-3}" denied allow="${LOOM_EGRESS_ALLOW_PROBE:-}"
  local ipv4='^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$'
  [[ "$wait" =~ ^[1-9][0-9]?$ ]] || wait=3
  denied="${LOOM_EGRESS_DENY_PROBES:-}"
  denied="${denied//,/ }"
  [[ "$denied" =~ [^[:space:]] ]] ||
    { SANDBOX_WHY="bwrap narrows no network: set LOOM_EGRESS_DENY_PROBES to the addresses the pod's egress policy blocks"; return 1; }
  [ -n "$allow" ] ||
    { SANDBOX_WHY="bwrap narrows no network: set LOOM_EGRESS_ALLOW_PROBE to an HTTPS endpoint the lane must reach"; return 1; }
  if [ -n "${KUBERNETES_SERVICE_HOST:-}" ]; then
    denied="$denied $KUBERNETES_SERVICE_HOST:${KUBERNETES_SERVICE_PORT:-443}"
  fi
  for target in $denied; do
    host="${target%:*}" port="${target##*:}"
    [[ "$host" =~ $ipv4 && "$port" =~ ^[1-9][0-9]{0,4}$ ]] ||
      { SANDBOX_WHY="egress probe '$target' is not IPv4:port (a name could fail to resolve and pass as blocked)"; return 1; }
    if probe_connects "$host" "$port" "$wait"; then
      SANDBOX_WHY="egress not narrowed: $target accepts connections, so no agent runs here"
      return 1
    fi
  done
  host="${allow%:*}" port="${allow##*:}"
  [[ "$host" =~ ^[A-Za-z0-9.-]+$ && "$port" =~ ^[1-9][0-9]{0,4}$ ]] ||
    { SANDBOX_WHY="LOOM_EGRESS_ALLOW_PROBE '$allow' is not host:port"; return 1; }
  probe_connects "$host" "$port" "$wait" ||
    { SANDBOX_WHY="egress unproven: $allow did not connect, so the blocked probes prove nothing"; return 1; }
}

# Whether a TCP connection to $1:$2 opens within $3 seconds.
probe_connects() {
  # shellcheck disable=SC2016 # $1 and $2 expand in the child shell
  timeout "$3" bash -c 'exec 3<> "/dev/tcp/$1/$2"' _ "$1" "$2" > /dev/null 2>&1
}

# SANDBOX_CMD: the command words that run a command inside the profile $1
# (from sandbox_profile), to be followed by the command and its arguments.
# Seatbelt takes the profile text; bwrap takes the profile's lines as its
# arguments, ending with --.
sandbox_command() {
  local kind line
  kind="$(sandbox_kind)" || return 1
  SANDBOX_CMD=()
  if [ "$kind" = seatbelt ]; then
    SANDBOX_CMD=(sandbox-exec -p "$1")
    return 0
  fi
  SANDBOX_CMD=(bwrap)
  while IFS= read -r line; do
    [ -n "$line" ] && SANDBOX_CMD+=("$line")
  done <<< "$1"
  SANDBOX_CMD+=(--)
}

# The bubblewrap form of sandbox_profile, one argument per line. The variables
# are sandbox_profile's, already canonical and checked. The root is an empty
# tmpfs: only the system folders (BWRAP_SYSTEM_DIRS) are bound in, read-only,
# so nothing else on the machine is there at all (HOME with the loom home and
# the copied SSM values, /run/secrets, /var/run/secrets, the loom checkout).
# Then the tree and the scratch home are bound writable, .git read-only, and
# the CLI's folder, the skill snapshot and the check's uv files read-only.
# /tmp is a private tmpfs; the root itself is read-only. Check runs get the same root as agent runs: their
# writes go to the tree, the check home and /tmp only. A fresh /proc in a new
# PID namespace hides the runner's processes and their environments.
# /run/secrets and /var/run/secrets are masked again with empty tmpfs in case
# a system folder ever reaches them. The network is the pod's (sandbox_ready
# requires its egress narrowed).
BWRAP_SYSTEM_DIRS="/usr /bin /sbin /lib /lib32 /lib64 /libx32 /etc /opt"
bwrap_profile() {
  local p
  printf '%s\n' --die-with-parent --new-session --unshare-pid --unshare-ipc --unshare-uts
  for p in $BWRAP_SYSTEM_DIRS; do printf '%s\n' --ro-bind-try "$p" "$p"; done
  printf '%s\n' --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run/secrets --tmpfs /var/run/secrets
  printf '%s\n' --bind "$tree" "$tree"
  [ -e "$tree/.git" ] && printf '%s\n' --ro-bind "$tree/.git" "$tree/.git"
  printf '%s\n' --bind "$agent_home" "$agent_home"
  for p in "$cli_root" "$cli_link" ${skills:+"$skills"} $uv_files; do
    printf '%s\n' --ro-bind "$p" "$p"
  done
  # The root tmpfs itself takes no writes (remount-ro does not reach the
  # binds and /tmp, which are mounts of their own).
  printf '%s\n' --remount-ro / --chdir "$tree"
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
# lane is bounded the same way (its model endpoint's port is open), and a
# validation run keeps the network and the writes outside HOME.
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
  # The runner unlocks the agy lane's keychain outside the sandbox (agy_unlock),
  # so the agent never needs it: every lane, including agy, is denied the
  # keychain server, and so is a validation run ($3 = check), whichever lane it
  # serves.
  local keychain_deny='(global-name "com.apple.SecurityServer") ' agy_lane="" api_writes="" api_network=""
  case "${3:-}" in
    "" | check) ;;
    api) api_writes="$(api_rules writes)"$'\n' && api_network="$(api_rules network)"$'\n' || return 1 ;;
    *) echo "refusing sandbox: unknown mode $3" >&2; return 1 ;;
  esac
  [ "$LANE" = agy ] && [ "${3:-}" != check ] && { api_network="$(api_rules network)" || return 1; agy_lane=1; }
  local kind
  kind="$(sandbox_kind)" || return 1
  if [ "$kind" = bwrap ] && [ "${3:-}" != api ] && [ "${3:-}" != check ]; then
    echo "refusing sandbox: bwrap serves api and check runs only, not mode '${3:-}' (lane $LANE)" >&2
    return 1
  fi
  local home tree cli_link cli_real cli_root p allow_rw allow_ro meta="" agy_rules="" skills="" skills_rules="" loopback_rule="" agy_keychain_deny=""
  if [ -n "${4:-}" ]; then
    [[ "$4" =~ ^[0-9]+$ ]] && [ "$4" -ge 49152 ] && [ "$4" -le 65535 ] ||
      { echo "refusing sandbox: loopback port $4 is not in 49152-65535" >&2; return 1; }
    loopback_rule="(allow network-outbound (remote ip \"localhost:$4\"))"
  fi
  home="$(cd "$HOME" && pwd -P)"
  tree="$(cd "$1" && pwd -P)"
  if [ "$LANE" = agy ] && [ -n "${AGY_HOME:-}" ]; then
    agy_keychain_deny="(deny file-read* (subpath \"$(cd "$AGY_HOME" && pwd -P)/Library/Keychains\"))"
  fi
  [ -d "$SKILLS" ] && skills="$(cd "$SKILLS" && pwd -P)"
  cli_link="$(command -v "$2")" && [ -n "$cli_link" ] || { echo "refusing sandbox: $2 not on PATH" >&2; return 1; }
  cli_real="$(realpath "$cli_link")" && [ -n "$cli_real" ] || { echo "refusing sandbox: cannot resolve $cli_link" >&2; return 1; }
  cli_root="$(dirname "$cli_real")"
  # Paths go into the profile's string grammar: refuse any that could close it.
  for p in "$home" "$tree" "$cli_link" "$cli_real" "$AGENT_HOME" "$skills"; do
    # shellcheck disable=SC1003 # matching a literal backslash
    case "$p" in *'"'*|*'\'*|*$'\n'*) echo "refusing sandbox: unsafe path $p" >&2; return 1 ;; esac
  done
  # Never let the CLI's directory cover HOME itself.
  case "$home/" in "$cli_root"/*) echo "refusing sandbox: $cli_real sits at or above HOME" >&2; return 1 ;; esac
  allow_rw="(subpath \"$tree\") (subpath \"$(cd "$AGENT_HOME" && pwd -P)\")"
  allow_ro="(subpath \"$cli_root\") (literal \"$cli_link\")"
  # The check profile (only $3 = check, never an agent run) lets a contract
  # reach the runner's uv and uvx: contract_run names their exact paths in
  # CHECK_UV_BINS (space-separated, no spaces in the paths). Those two
  # files only, never the directory that holds them: no subpath rule is
  # added for it, and the ancestor literals below grant metadata only.
  local uv_file uv_real uv_files=""
  if [ "${3:-}" = check ] && [ -n "${CHECK_UV_BINS:-}" ]; then
    for uv_file in $CHECK_UV_BINS; do
      # shellcheck disable=SC1003 # matching a literal backslash
      case "$uv_file" in *'"'*|*'\'*)
        echo "refusing sandbox: unsafe uv path $uv_file" >&2; return 1 ;; esac
      uv_real="$(realpath "$uv_file" 2>/dev/null)" || uv_real=""
      [ -n "$uv_real" ] && [ -r "$uv_real" ] ||
        { echo "refusing sandbox: uv binary not readable $uv_file" >&2; return 1; }
      allow_ro="$allow_ro (literal \"$uv_real\")"
      uv_files="$uv_files $uv_real"
    done
  fi
  # The skill snapshot: readable, never writable, wherever LOOM_HOME lives.
  [ -n "$skills" ] && skills_rules="(allow file-read* (subpath \"$skills\"))
(deny file-write* (subpath \"$skills\"))
"
  if [ "$kind" = bwrap ]; then
    local agent_home
    agent_home="$(cd "$AGENT_HOME" && pwd -P)" || return 1
    # A bind at or above HOME would hand the agent all of HOME (the loom
    # home, the copied SSM values): writable for the tree or scratch home,
    # readable for the rest. Every bind must sit below HOME or beside it.
    for p in "$tree" "$agent_home" "$cli_root" ${skills:+"$skills"} $uv_files; do
      case "$p" in /) echo "refusing sandbox: $p is the root" >&2; return 1 ;; esac
      case "$home/" in "$p"/*) echo "refusing sandbox: $p sits at or above HOME" >&2; return 1 ;; esac
    done
    bwrap_profile
    return 0
  fi
  for p in "$tree" "$AGENT_HOME" "$cli_root" "$cli_link" ${skills:+"$skills"} $uv_files; do
    p="$(dirname "$p")"
    while [ "${p#"$home"}" != "$p" ] && [ "$p" != "/" ]; do
      meta="$meta (literal \"$p\")"
      [ "$p" = "$home" ] && break
      p="$(dirname "$p")"
    done
  done
  [ -n "$agy_lane" ] && agy_rules="$(agy_write_rules "$allow_rw")"
  printf '(version 1)\n(allow default)\n%s(deny file-read* file-write* (subpath "%s"))\n(allow file-read* file-write* %s)\n%s(deny file-write* (subpath "%s/.git"))\n(allow file-read* %s)\n%s(allow file-read-metadata%s)\n(deny mach-lookup %s(global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.coreservices.appleevents"))\n(deny appleevent-send)\n(deny process-exec (literal "/usr/bin/osascript") (literal "/usr/bin/open") (literal "/bin/launchctl") (literal "/usr/bin/sudo") (literal "/usr/bin/su") (literal "/usr/bin/security"))\n(deny network-outbound (remote unix-socket))\n(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))\n(deny network-outbound (remote ip "localhost:*"))\n%s%s\n' \
    "$api_writes" "$home" "$allow_rw" "$agy_rules" "$tree" "$allow_ro" "$skills_rules" "$meta" "$keychain_deny" "$api_network" "$loopback_rule"
  [ -z "$agy_keychain_deny" ] || printf '%s\n' "$agy_keychain_deny"
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
  sandbox_command "$profile" || return 3
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
      "${SANDBOX_CMD[@]}" "$cli" serve --hostname 127.0.0.1 \
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
        if "${SANDBOX_CMD[@]}" /usr/bin/nc -z -w 1 127.0.0.1 "$server_port" \
          > /dev/null 2>&1; then ready=1; break; fi
        sleep 0.1
      done
      [ -n "$ready" ] || { echo "opencode server was not ready within 15 seconds" >&2; exit 3; }
      timeout -k 5 "$AGENT_TIMEOUT" "${SANDBOX_CMD[@]}" "${@:2}" < "${AGENT_STDIN:-/dev/null}"
    else
      exec timeout "$AGENT_TIMEOUT" "${SANDBOX_CMD[@]}" "${@:2}" < "${AGENT_STDIN:-/dev/null}"
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
  # Reads in the tree are allowed: opencode asks before a *.env read by
  # default, a run cannot answer, and a tracked pin file stopped two runs
  # (loom#1728). The tree is a fresh clone, and reads outside it stay denied.
  [ -n "$permission" ] || permission='{"external_directory": "deny", "read": {"*": "allow"}}'
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
# --print-logs at ERROR puts opencode's own error on stderr: its run output
# says only "UnknownError: Unexpected server error" for a model it dropped
# from its catalogue or an upstream 429, so both read as transient and a lane
# whose model was gone kept claiming (zen 2026-10-19, openrouter 2026-10-20).
# Usage: opencode_run <key_var> <opencode binary> <run arguments...>
opencode_run() {
  local key_var="$1" bin="$2" port
  shift 2
  opencode_config || return 1
  case "$(timeout "$NET_TIMEOUT" "$bin" --version < /dev/null 2> /dev/null | head -n 1)" in
    "opencode v2."*)
      port="$(free_loopback_port)" || return 3
      OPENCODE_SERVER_PORT="$port" api_agent "$key_var" "$bin" run \
        --server "http://127.0.0.1:$port" --auto -m "$MODEL" --print-logs --log-level ERROR "$@"
      return $? ;;
  esac
  api_agent "$key_var" "$bin" run -m "$MODEL" --print-logs --log-level ERROR "$@"
}
