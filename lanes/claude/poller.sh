#!/usr/bin/env bash
# Loom Claude lane poller  -  deep-thought lane.
#
# Runs on the machine where the owner's Claude subscription login lives
# (official Claude Code only  -  sanctioned client, no credential sharing).
# Watches INBOX for new briefs, runs one at a time in a fresh worktree,
# opens a PR per brief. Never merges.
#
# Install: run under launchd/cron on the login machine.
# Tunables (env): LOOM_AGENT_TIMEOUT (seconds, default 3600) caps one
# agent run so a stuck worker cannot block the inbox.
set -euo pipefail

INBOX="$HOME/loom/briefs/claude"
DONE="$HOME/loom/briefs/claude/done"
WORKDIR="$HOME/loom/worktrees"
AGENT_TIMEOUT="${LOOM_AGENT_TIMEOUT:-3600}"
mkdir -p "$INBOX" "$DONE" "$WORKDIR"

for BRIEF in "$INBOX"/*.md; do
  [ -e "$BRIEF" ] || continue
  TICKET="$(basename "$BRIEF" .md)"
  REPO="$(grep -m1 '^Repo:' "$BRIEF" | cut -d' ' -f2-)"
  BRANCH="$(grep -m1 '^Branch:' "$BRIEF" | cut -d' ' -f2-)"
  TREE="$WORKDIR/$TICKET"

  # Drop leftovers from a partial run so a retry always starts clean.
  rm -rf "$TREE"

  git clone --depth 50 "git@github.com:${REPO}.git" "$TREE"
  cd "$TREE"
  git checkout -b "$BRANCH"

  # Official CLI, owner's login. Non-interactive, permission mode approved
  # for the lane at install time. timeout keeps one bad run from wedging
  # the lane.
  timeout "$AGENT_TIMEOUT" \
    claude -p "$(cat "$BRIEF")" --output-format text

  # Force push: this branch is loom-owned (loom/<ticket>-<slug>), and a
  # retry of a partial run must replace, not collide with, prior pushes.
  # Fenced like the agent run above: a push to an unreachable remote must
  # not hang the lane forever.
  timeout "$AGENT_TIMEOUT" git push -f -u origin "$BRANCH"

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
