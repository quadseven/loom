# App-only GitHub authentication for the lane runner, brain and station.

LOOM_AUTH_DIR=""

github_auth_error() { printf 'Loom GitHub auth: %s\n' "$*" >&2; }

github_auth_init() {
  local lanes="$1" first
  if [ "${GH_TOKEN+x}" = x ]; then
    github_auth_error "ambient GH_TOKEN is set; remove it before starting Loom"
    return 1
  fi
  if [ -n "${GH_CONFIG_DIR:-}" ]; then
    if [ ! -d "$GH_CONFIG_DIR" ] || [ ! -r "$GH_CONFIG_DIR" ] || [ ! -x "$GH_CONFIG_DIR" ]; then
      github_auth_error "GH_CONFIG_DIR is not an accessible directory"
      return 1
    fi
    first="$(find "$GH_CONFIG_DIR/." -mindepth 1 -maxdepth 1 -print -quit)" || return 1
    if [ -n "$first" ]; then
      github_auth_error "GH_CONFIG_DIR is not empty; remove it from the job environment"
      return 1
    fi
  fi
  unset GITHUB_TOKEN LOOM_GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN GH_HOST
  LOOM_REAL_GH="$(command -v gh)" || { github_auth_error "gh is missing from PATH"; return 1; }
  LOOM_GITHUB_BIN="$lanes/github-bin"
  LOOM_AUTH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-github.XXXXXXXX")" || return 1
  GH_CONFIG_DIR="$LOOM_AUTH_DIR/config"
  mkdir "$GH_CONFIG_DIR" || { github_auth_cleanup; return 1; }
  export LOOM_REAL_GH LOOM_GITHUB_BIN GH_CONFIG_DIR
  export LOOM_APP_TOKEN_FILE="$LOOM_AUTH_DIR/token"
  export PATH="$LOOM_GITHUB_BIN:$PATH"

  # Clear inherited helpers and authorization headers. SSH remotes on GitHub
  # use HTTPS so git never tries a login key or a prompted credential.
  unset GIT_CONFIG_PARAMETERS
  export GIT_CONFIG_COUNT=5 GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/usr/bin/false
  export GIT_ALLOW_PROTOCOL=file:https
  export GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0=
  export GIT_CONFIG_KEY_1=credential.https://github.com.helper
  export GIT_CONFIG_VALUE_1='!exec "$LOOM_GITHUB_BIN/git-credential-loom"'
  export GIT_CONFIG_KEY_2=url.https://github.com/.insteadOf GIT_CONFIG_VALUE_2=git@github.com:
  export GIT_CONFIG_KEY_3=url.https://github.com/.insteadOf GIT_CONFIG_VALUE_3=ssh://git@github.com/
  export GIT_CONFIG_KEY_4=http.https://github.com/.extraHeader GIT_CONFIG_VALUE_4=
}

github_auth_refresh() {
  local file="$LOOM_AUTH_DIR/token" token
  LOOM_APP_TOKEN_FILE=""
  rm -f "$file" || return 1
  token="$(timeout "${LOOM_NET_TIMEOUT:-120}" "$LOOM_GITHUB_BIN/../fleet-token" "$@")" || return 1
  (umask 077 && printf '%s\n' "$token" > "$file") || return 1
  LOOM_APP_TOKEN_FILE="$file"
}

github_auth_cleanup() {
  [ -z "${LOOM_AUTH_DIR:-}" ] || rm -rf "$LOOM_AUTH_DIR"
}

github_valid_token() { [[ "$1" =~ ^ghs_[A-Za-z0-9_.-]+$ ]]; }

github_read_token() {
  local value
  [ -r "${LOOM_APP_TOKEN_FILE:-}" ] && IFS= read -r value < "$LOOM_APP_TOKEN_FILE" &&
    github_valid_token "$value" || return 1
  printf -v "$1" '%s' "$value"
}
