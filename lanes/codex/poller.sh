#!/usr/bin/env bash
# Loom Codex lane poller  -  scripted code lane.
#
# Runs on a box logged into the owner's Codex subscription.
# Watches INBOX for new briefs, runs `codex exec` headless per brief
# (model chosen by the dispatcher, written in the brief as Model:),
# opens a PR per brief. Never merges.
#
# Tunables (env): LOOM_AGENT_TIMEOUT (seconds, default 3600) caps one
# agent run so a stuck worker cannot block the inbox.
set -euo pipefail

INBOX="$HOME/loom/briefs/codex"
DONE="$HOME/loom/briefs/codex/done"
WORKDIR="$HOME/loom/worktrees"
AGENT_TIMEOUT="${LOOM_AGENT_TIMEOUT:-3600}"
mkdir -p "$INBOX" "$DONE" "$WORKDIR"

for BRIEF in "$INBOX"/*.md; do
  [ -e "$BRIEF" ] || continue
  TICKET="$(basename "$BRIEF" .md)"
  REPO="$(grep -m1 '^Repo:' "$BRIEF" | cut -d' ' -f2-)"
  BRANCH="$(grep -m1 '^Branch:' "$BRIEF" | cut -d' ' -f2-)"
  MODEL="$(grep -m1 '^Model:' "$BRIEF" | cut -d' ' -f2- || true)"
  MODEL_ARGS=()
  if [ -n "$MODEL" ]; then
    # The Model: line is dispatcher-written, but validate anyway: it must
    # look like a model id (letters, digits, . _ : / -), never a flag or
    # shell metacharacters. A hostile or careless brief must not steer
    # the lane to an arbitrary --model argument.
    case "$MODEL" in
      -*|*[^A-Za-z0-9._:/-]*)
        echo "WARN: refusing suspicious Model '$MODEL' in $BRIEF; using default" >&2
        ;;
      *)
        MODEL_ARGS=(--model "$MODEL")
        ;;
    esac
  fi
  TREE="$WORKDIR/$TICKET"

  # Drop leftovers from a partial run so a retry always starts clean.
  rm -rf "$TREE"

  git clone --depth 50 "git@github.com:${REPO}.git" "$TREE"
  cd "$TREE"
  git checkout -b "$BRANCH"

  # codex exec is built for this: headless, JSON output for the log.
  # timeout keeps one bad run from wedging the lane.
  timeout "$AGENT_TIMEOUT" \
    codex exec --full-auto "${MODEL_ARGS[@]}" --json \
    "$(cat "$BRIEF")" > "$DONE/$TICKET.log.json"

  # Force push: this branch is loom-owned (loom/<ticket>-<slug>), and a
  # retry of a partial run must replace, not collide with, prior pushes.
  git push -f -u origin "$BRANCH"

  # Idempotent: a retry after a partial run must not die on "PR exists".
  if [ -n "$(gh pr list --head "$BRANCH" --json number --jq '.[0].number // empty')" ]; then
    echo "PR already exists for $BRANCH, skipping create"
  else
    gh pr create --title "[$TICKET] $(grep -m1 '^# Brief:' "$BRIEF" | sed 's/# Brief: //')" \
      --body "Part of loom ticket $TICKET. See brief for scope and definition of done."
  fi

  mv "$BRIEF" "$DONE/"
  cd /
done
