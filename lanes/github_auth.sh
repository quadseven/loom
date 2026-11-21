# Fail-closed GitHub identity for the lane runner (lanes/run_lane.sh), the
# brain tick (lanes/mac/brain.sh) and the station (lanes/mac/station.sh),
# loom#3083. Plain gh falls back to any stored login when its token is
# empty, so an agent silently acted as a person. Sourced; bash 3.2.
#
#   github_auth_init LANES_DIR  stop on an ambient GH_TOKEN or a non-empty
#       GH_CONFIG_DIR; then put LANES_DIR/github-bin first on PATH (the gh
#       wrapper), point GH_CONFIG_DIR at a fresh empty directory, and make
#       git ask only LANES_DIR/github-bin/git-credential-loom for github.com.
#   github_auth_mint [ARGS]     export GH_TOKEN as this process's App token,
#       from LANES_DIR/fleet-token (ARGS go to `bus app token`); on failure
#       GH_TOKEN is unset and it returns 1.
#   github_auth_agent_path      drop the wrapper from PATH, for an agent
#       whose gh reads its own per-run config directory.
#   github_auth_cleanup         remove the config directory.

LOOM_AUTH_DIR=""

github_auth_error() { printf 'Loom GitHub auth: %s\n' "$*" >&2; }

# True when $1 names something that is not an empty, readable directory.
github_auth_dir_used() {
  [ -e "$1" ] || [ -L "$1" ] || return 1
  [ -d "$1" ] && [ -r "$1" ] && [ -x "$1" ] || return 0
  [ -n "$(ls -A "$1")" ]
}

github_auth_init() {
  local lanes="$1" real
  if [ -n "${GH_TOKEN+x}" ]; then
    github_auth_error "an ambient GH_TOKEN is set; Loom mints its own App token, so remove it from the job's environment"
    return 1
  fi
  if [ -n "${GH_CONFIG_DIR:-}" ] && github_auth_dir_used "$GH_CONFIG_DIR"; then
    github_auth_error "GH_CONFIG_DIR ($GH_CONFIG_DIR) is not an empty directory; a stored login there could answer for Loom, so remove it from the job's environment"
    return 1
  fi
  real="$(command -v gh)" || real=""
  if [ "$real" = "$lanes/github-bin/gh" ]; then
    github_auth_error "the gh wrapper is already first on PATH; the real gh cannot be found"
    return 1
  fi
  LOOM_AUTH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-gh.XXXXXXXX")" || return 1
  export LOOM_REAL_GH="$real" LOOM_GITHUB_BIN="$lanes/github-bin"
  export GH_CONFIG_DIR="$LOOM_AUTH_DIR"
  export PATH="$LOOM_GITHUB_BIN:$PATH"
  # git's own helpers (a keychain, a stored login) are emptied for
  # github.com, and only this process's App token answers. A push to an SSH
  # remote goes over HTTPS, so no personal SSH key pushes either.
  export GIT_TERMINAL_PROMPT=0
  github_auth_git_config credential.https://github.com.helper "" || return 1
  github_auth_git_config credential.https://github.com.helper '!"$LOOM_GITHUB_BIN/git-credential-loom"'
  github_auth_git_config url.https://github.com/.pushInsteadOf git@github.com:
  github_auth_git_config url.https://github.com/.pushInsteadOf ssh://git@github.com/
}

# Append one KEY VALUE to git's environment config, after any entries the
# process was started with.
github_auth_git_config() {
  local n="${GIT_CONFIG_COUNT:-0}"
  if ! [[ "$n" =~ ^[0-9]+$ ]]; then
    github_auth_error "GIT_CONFIG_COUNT is not a number"
    return 1
  fi
  export "GIT_CONFIG_KEY_$n=$1" "GIT_CONFIG_VALUE_$n=$2" GIT_CONFIG_COUNT=$((n + 1))
}

github_auth_mint() {
  local token
  unset GH_TOKEN
  token="$(timeout "${LOOM_NET_TIMEOUT:-120}" "$LOOM_GITHUB_BIN/../fleet-token" "$@")" || return 1
  export GH_TOKEN="$token"
}

# For an agent whose gh reads its own per-run config directory (the lane
# agent, loom#665): the real gh, not the wrapper, answers its gh calls.
github_auth_agent_path() { PATH="${PATH#"$LOOM_GITHUB_BIN":}"; }

github_auth_cleanup() {
  [ -z "${LOOM_AUTH_DIR:-}" ] || rm -rf "$LOOM_AUTH_DIR"
}
