#!/usr/bin/env bash
# Loom opencode lane runner  -  volume lane.
#
# Usage: run.sh <brief-file>
#
# Takes a ticket brief, creates a fresh worktree of the ticket's repo,
# runs headless opencode against the configured provider/model, then opens
# a PR. Never merges.
#
# Provider config comes from lanes/opencode/config.json (see
# config.example.json). API keys are read from the environment
# (OPENROUTER_API_KEY, POOLSIDE_API_KEY)  -  never from files.
set -euo pipefail

BRIEF="$1"
TICKET="$(basename "$BRIEF" .md)"
REPO="$(grep -m1 '^Repo:' "$BRIEF" | cut -d' ' -f2-)"
BRANCH="$(grep -m1 '^Branch:' "$BRIEF" | cut -d' ' -f2-)"

WORKTREE="$HOME/loom/worktrees/$TICKET"
mkdir -p "$HOME/loom/worktrees"

# Fresh disposable clone per ticket.
git clone --depth 50 "git@github.com:${REPO}.git" "$WORKTREE"
cd "$WORKTREE"
git checkout -b "$BRANCH"

# Headless run. Model is chosen by the dispatcher and written into the
# brief as `Model: <provider/model>`; default falls back to config default.
MODEL="$(grep -m1 '^Model:' "$BRIEF" | cut -d' ' -f2- || true)"
MODEL_ARGS=()
[ -n "$MODEL" ] && MODEL_ARGS=(--model "$MODEL")

opencode run "${MODEL_ARGS[@]}" "$(cat "$BRIEF")"

# Open a PR with the work. Title carries the ticket id for traceability.
# Force push + idempotent create: this branch is loom-owned, and a retry
# after a partial run must not die on "PR exists".
git push -f -u origin "$BRANCH"
if [ -n "$(gh pr list --head "$BRANCH" --json number --jq '.[0].number // empty')" ]; then
  echo "PR already exists for $BRANCH, skipping create"
else
  gh pr create --title "[$TICKET] $(grep -m1 '^# Brief:' "$BRIEF" | sed 's/# Brief: //')" \
    --body "Part of loom ticket $TICKET. See brief for scope and definition of done."
fi
