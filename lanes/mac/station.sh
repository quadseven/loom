#!/usr/bin/env bash
# The station's launchd entry (lanes/mac/install.sh). Before each pass it
# brings this checkout up to date with its default branch, so a merged Loom
# change runs within a minute instead of waiting for a person to pull: the
# lanes and the brain run from this checkout too. It moves only by
# fast-forward, only from a clean checkout on LOOM_DEPLOY_BRANCH (default
# main), and only to a head whose CI check (LOOM_DEPLOY_CHECK, default
# unittest) passed; a newer head with CI running or failing waits. The update
# runs here, before Python, so a station a bad merge broke still receives the
# fix merged after it. LOOM_DEPLOY=0 turns it off.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BRANCH="${LOOM_DEPLOY_BRANCH:-main}"
CHECK="${LOOM_DEPLOY_CHECK:-unittest}"
say() { printf 'station: %s\n' "$*"; }
g() { git -C "$REPO_ROOT" -c core.hooksPath=/dev/null "$@"; }

deploy() {
  [ "${LOOM_DEPLOY:-1}" != 0 ] || return 0
  [ "$(g symbolic-ref --short -q HEAD)" = "$BRANCH" ] || return 0
  if [ -n "$(g status --porcelain --untracked-files=no)" ]; then
    say "not deploying: the checkout has local changes"; return 0
  fi
  timeout 60 git -C "$REPO_ROOT" -c core.hooksPath=/dev/null fetch -q origin "$BRANCH" 2>/dev/null ||
    { say "not deploying: fetch of $BRANCH failed"; return 0; }
  local head target slug verdict
  head="$(g rev-parse HEAD)"; target="$(g rev-parse FETCH_HEAD)"
  [ "$head" != "$target" ] || return 0
  if ! g merge-base --is-ancestor "$head" "$target"; then
    say "not deploying: $BRANCH is not a fast-forward of the checkout"; return 0
  fi
  slug="$(g remote get-url origin | sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##')"
  verdict="$(timeout 30 gh api "repos/$slug/commits/$target/check-runs?check_name=$CHECK" \
    --jq '[.check_runs[].conclusion] | if length == 0 then "none" elif all(. == "success") then "success" else "other" end' 2>/dev/null)"
  [ "$verdict" = success ] || return 0
  if g merge -q --ff-only "$target"; then
    say "deployed $(g rev-parse --short HEAD): $(g log -1 --format=%s HEAD | cut -c1-100)"
  fi
}

deploy
cd "$REPO_ROOT" && exec python3 -m bus station
