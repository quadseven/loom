#!/usr/bin/env bash
# Loom lane runner: one pass over a lane's inbox, one brief at a time.
#
#   run_lane.sh <lane>        lane = codex | claude
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
# LOOM_CODEX_MODEL / LOOM_CLAUDE_MODEL (the lane's model when a brief has no
# Model: line; every run passes its model explicitly and names it in the PR),
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
PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"

field() { grep -m1 "^$1:" "$2" | cut -d' ' -f2-; }

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

run_agent() {
  local brief_text
  brief_text="$(cat "$1")"
  case "$LANE" in
    codex) timeout "$AGENT_TIMEOUT" codex exec --sandbox workspace-write --model "$MODEL" "$brief_text" < /dev/null ;;
    claude) timeout "$AGENT_TIMEOUT" claude -p --model "$MODEL" "$brief_text" < /dev/null ;;
    *) echo "unknown lane $LANE" >&2; return 2 ;;
  esac
}

for BRIEF in "$INBOX"/*.md; do
  [ -e "$BRIEF" ] || continue
  TICKET="$(basename "$BRIEF" .md)"
  REPO="$(field Repo "$BRIEF")"
  BRANCH="$(field Branch "$BRIEF")"
  # Optional: the branch work starts from and the PR targets. Default is the
  # remote's default branch, which is not always where work lands.
  BASE_BRANCH="$(field Base "$BRIEF" || true)"
  LOG="$LOGDIR/$TICKET.log"

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
  if [ -z "$MODEL" ]; then
    case "$LANE" in codex) MODEL="${LOOM_CODEX_MODEL:-}" ;; claude) MODEL="${LOOM_CLAUDE_MODEL:-}" ;; esac
  fi
  if ! [[ "$MODEL" =~ ^[A-Za-z0-9._:][A-Za-z0-9._:/-]*$ ]]; then
    park "$BRIEF" "no usable model: set Model: in the brief or the lane default"; continue
  fi
  case "$LANE:$MODEL" in
    codex:claude*|codex:opus*|codex:sonnet*|codex:haiku*|codex:fable*)
      park "$BRIEF" "model $MODEL is not a codex model"; continue ;;
    claude:claude*|claude:opus*|claude:sonnet*|claude:haiku*|claude:fable*) ;;
    claude:*) park "$BRIEF" "model $MODEL is not a claude model"; continue ;;
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
  if [ -n "$(git -C "$TREE" status --porcelain)" ]; then
    if ! { git -C "$TREE" checkout -q "$BRANCH" && git -C "$TREE" add -A &&
           git -C "$TREE" commit -qm "${TITLE:-$TICKET}"; } >> "$LOG" 2>&1; then
      park "$BRIEF" "could not commit the agent's changes"; continue
    fi
  fi
  if [ "$(git -C "$TREE" rev-parse "$BRANCH")" = "$BASE" ]; then
    park "$BRIEF" "agent made no commit on $BRANCH"; continue
  fi
  if ! timeout "$NET_TIMEOUT" git -C "$TREE" push -q -u origin "$BRANCH" >> "$LOG" 2>&1; then
    park "$BRIEF" "push failed"; continue
  fi
  if ! timeout "$NET_TIMEOUT" gh pr create -R "$REPO" ${PR_BASE_ARGS[@]+"${PR_BASE_ARGS[@]}"} --head "$BRANCH" --title "${TITLE:-$TICKET}" \
       --body "$(pr_body "$BRIEF" "$TICKET")" >> "$LOG" 2>&1; then
    park "$BRIEF" "pr create failed; branch $BRANCH is pushed"; continue
  fi
  echo "[$(date -u +%FT%TZ)] $TICKET done" >> "$LOG"
  mv "$BRIEF" "$INBOX/done/"
done
