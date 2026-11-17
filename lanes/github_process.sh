#!/usr/bin/env bash
# Shared fail-closed GitHub setup for lane, brain and station processes.

loom_github_start() {
  local root="$1" run_dir="$2" name real helper_command credential_helper
  for name in GH_TOKEN GITHUB_TOKEN LOOM_GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN; do
    if [ -n "${!name:-}" ]; then
      printf 'refusing GitHub process: ambient %s is set\n' "$name" >&2
      return 1
    fi
  done
  if [ -n "${GH_CONFIG_DIR:-}" ] && [ -d "$GH_CONFIG_DIR" ] &&
     [ -n "$(ls -A "$GH_CONFIG_DIR" 2>/dev/null)" ]; then
    echo "refusing GitHub process: GH_CONFIG_DIR is not empty" >&2
    return 1
  fi
  real="$(command -v gh)" || { echo "refusing GitHub process: gh is unavailable" >&2; return 1; }
  mkdir -p "$run_dir" || return 1
  GH_CONFIG_DIR="$(mktemp -d "$run_dir/gh-config.XXXXXX")" || return 1
  LOOM_REAL_GH="$real"
  credential_helper="$root/lanes/git-credential-app"
  LOOM_GIT_AUTH="$root/lanes/git-app"
  helper_command="$(printf '%q' "$credential_helper")"
  export GH_CONFIG_DIR LOOM_REAL_GH LOOM_GIT_AUTH
  PATH="$root/lanes:$PATH"
  export PATH
  LOOM_GIT_CREDENTIAL_HELPER_ESCAPED="$helper_command"
  export LOOM_GIT_CREDENTIAL_HELPER_ESCAPED
}

loom_github_mint() {
  local root="$1" timeout_seconds="$2" token
  unset GH_TOKEN GITHUB_TOKEN LOOM_GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
  if [ -n "${LOOM_APP_TOKEN_CMD:-}" ]; then
    token="$(timeout "$timeout_seconds" "$LOOM_APP_TOKEN_CMD" 2>> "${LOG:-/dev/null}")" || token=""
  else
    token="$(cd "$root" && timeout "$timeout_seconds" python3 -B -m bus app token \
      2>> "${LOG:-/dev/null}")" || token=""
  fi
  [[ "$token" =~ ^ghs_[A-Za-z0-9_.-]+$ ]] || {
    echo "could not mint a GitHub App installation token" >&2
    return 1
  }
  export GH_TOKEN="$token" GITHUB_TOKEN="$token" LOOM_GITHUB_TOKEN="$token"
}

loom_github_cleanup() {
  [ -z "${GH_CONFIG_DIR:-}" ] || rm -rf "$GH_CONFIG_DIR"
}
