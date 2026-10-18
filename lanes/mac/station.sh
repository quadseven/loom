#!/usr/bin/env bash
# The station's launchd entry (lanes/mac/install.sh). Before each pass it
# brings this checkout up to date with its default branch, so a merged Loom
# change runs within a minute instead of waiting for a person to pull: the
# lanes and the brain run from this checkout too. It moves only by
# fast-forward, only from a clean checkout on LOOM_DEPLOY_BRANCH (default
# main), and only to a head whose CI check (LOOM_DEPLOY_CHECK, default
# unittest) passed; a newer head with CI running or failing waits, and so
# does one that would bring in a commit an in-flight revert ticket names
# (bus/revert.py: a merge that turned the branch red). When the newest head
# has not passed yet, the newest of the last DEPLOY_LOOKBACK commits on the
# branch whose check passed is deployed instead: lane and operator merges can
# land faster than CI clears them, and waiting for the head alone then
# deployed nothing for hours (2026-10-18). The update
# runs here, before Python, so a station a bad merge broke still receives the
# fix merged after it. LOOM_DEPLOY=0 turns it off. A deploy that succeeded is
# recorded in the station's state (deployed.json: the revision and when), and
# only then: the pass ships the merged tickets that revision carries
# (bus/station.py ship_deployed).
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BRANCH="${LOOM_DEPLOY_BRANCH:-main}"
CHECK="${LOOM_DEPLOY_CHECK:-unittest}"
DEPLOY_LOOKBACK=8
STATE_DIR="${LOOM_HOME:-$HOME/loom}/state/station"
say() { printf 'station: %s\n' "$*"; }
g() { git -C "$REPO_ROOT" -c core.hooksPath=/dev/null "$@"; }

api() {
  local path="$1"
  if [ -n "${LOOM_GITHUB_APP_ID:-}" ] &&
     [ -n "${LOOM_GITHUB_APP_INSTALLATION_ID:-}" ] &&
     { [ -n "${LOOM_GITHUB_APP_KEY_SSM:-}" ] || [ -n "${LOOM_GITHUB_APP_KEY_FILE:-}" ]; }; then
    PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" timeout 30 python3 -c '
import json, sys
from bus.github import GitHub, auth_from_env
response = GitHub(auth_from_env()).rest("GET", "/" + sys.argv[1].lstrip("/"))
print(json.dumps(response.data))
' "$path"
  else
    timeout 30 gh api "$path"
  fi
}

record_deploy() {
  mkdir -p "$STATE_DIR" &&
    printf '{"revision": "%s", "at": %s}\n' "$1" "$(date +%s)" > "$STATE_DIR/deployed.json.tmp" &&
    mv -f "$STATE_DIR/deployed.json.tmp" "$STATE_DIR/deployed.json" ||
    say "could not record the deploy of ${1:0:12}"
}

# Sets verdict (success|other|none) and check_state for commit $1 of $slug;
# fails with check_error set when the check cannot be read.
read_check() {
  local check_json
  if check_json="$(api "repos/$slug/commits/$1/check-runs?check_name=$CHECK" 2>&1)"; then
    verdict="$(printf '%s' "$check_json" | jq -r '[.check_runs[].conclusion] | if length == 0 then "none" elif all(. == "success") then "success" else "other" end')"
    check_state="$(printf '%s' "$check_json" | jq -r '[.check_runs[].status] | if length == 0 then "none" elif any(. == "in_progress") then "in_progress" elif any(. == "queued") then "queued" elif any(. == "requested") then "requested" else "completed" end')"
  else
    check_error="$(printf '%s' "$check_json" | tail -1)"
    return 1
  fi
}

deploy() {
  [ "${LOOM_DEPLOY:-1}" != 0 ] || return 0
  [ "$(g symbolic-ref --short -q HEAD)" = "$BRANCH" ] || return 0
  if [ -n "$(g status --porcelain --untracked-files=no)" ]; then
    say "not deploying: the checkout has local changes"; return 0
  fi
  timeout 60 git -C "$REPO_ROOT" -c core.hooksPath=/dev/null fetch -q origin "$BRANCH" 2>/dev/null ||
    { say "not deploying: fetch of $BRANCH failed"; return 0; }
  local head target slug verdict check_error check_state newest sha
  head="$(g rev-parse HEAD)"; target="$(g rev-parse FETCH_HEAD)"
  [ "$head" != "$target" ] || return 0
  if ! g merge-base --is-ancestor "$head" "$target"; then
    say "not deploying: $BRANCH is not a fast-forward of the checkout"; return 0
  fi
  slug="$(g remote get-url origin | sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##')"
  if ! read_check "$target"; then
    say "not deploying: the CI check could not be read ($check_error)"
    return 0
  fi
  if [ "$verdict" != success ]; then
    if [ "$check_state" != completed ] && [ "$check_state" != none ]; then
      say "waiting: $CHECK is $check_state on ${target:0:12}"
    else
      say "not deploying: $CHECK did not pass on ${target:0:12} ($verdict)"
    fi
    # The newest older commit on the branch whose check passed, if any:
    # never one the checkout already has, never one unchecked or red.
    newest="$target"; target=""
    for sha in $(g rev-list --first-parent --max-count="$DEPLOY_LOOKBACK" "$head..$newest" | tail -n +2); do
      read_check "$sha" || break
      if [ "$verdict" = success ]; then target="$sha"; break; fi
    done
    [ -n "$target" ] || return 0
    say "deploying ${target:0:12}, the newest commit $CHECK passed on, while ${newest:0:12} waits"
  fi
  local named
  named="$(api "repos/$slug/issues?labels=loom:revert&state=open&per_page=100" 2>/dev/null | jq -r '.[] | select([.labels[].name] | any(. == "loom:merged" or . == "loom:shipped" or . == "loom:verified") | not) | .body')" ||
    { say "not deploying: could not read the revert tickets"; return 0; }
  for sha in $(printf '%s\n' "$named" | sed -nE 's/.*git revert --no-edit ([0-9a-f]{40}).*/\1/p'); do
    if g merge-base --is-ancestor "$sha" "$target" 2>/dev/null &&
       ! g merge-base --is-ancestor "$sha" "$head" 2>/dev/null; then
      say "not deploying: ${sha:0:12} is named by a revert ticket"; return 0
    fi
  done
  if g merge -q --ff-only "$target"; then
    say "deployed $(g rev-parse --short HEAD): $(g log -1 --format=%s HEAD | cut -c1-100)"
    record_deploy "$(g rev-parse HEAD)"
  fi
}

deploy
if [ -n "${LOOM_PIN_TARGETS:-}" ] && [ "${LOOM_DEPLOY:-1}" != 0 ] &&
   [ "$(g symbolic-ref --short -q HEAD)" = "$BRANCH" ]; then
  LOOM_SELF_REPO="${LOOM_SELF_REPO:-$(g remote get-url origin | sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##')}" \
    python3 -m bus.pin || say "pin pass failed; will retry on the next station pass"
fi
cd "$REPO_ROOT" && exec python3 -m bus station
