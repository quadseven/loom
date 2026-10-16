#!/usr/bin/env bash
# One brain tick on the lane machine (docs/BRAIN.md, docs/MUSE.md).
#
#   lanes/mac/brain.sh
#
# launchd runs it every LOOM_BRAIN_INTERVAL seconds (lanes/mac/install.sh).
# It takes the brain lock, refreshes its own checkout of this repo, runs the
# claude CLI headless on docs/MUSE.md, and releases the lock, also when the
# agent fails or times out. A lock held by anyone else (Muse, an interactive
# session) skips the tick before any model call, so only one brain acts at a
# time. With LOOM_REPOS empty it stays idle.
#
# Settings: LOOM_REPOS, LOOM_STATUS_REPO (where the lock lives; default this
# checkout's origin), LOOM_BRAIN_MODEL, LOOM_BRAIN_HOLDER, LOOM_BRAIN_MINUTES
# (the agent's hard stop, also the lock's expiry), LOOM_BRAIN_WAIT (minutes to
# wait for a held lock: 25 for a live tick, 0 otherwise), and LOOM_BRAIN_SCOPE: all
# (the default) or live, which leaves everything but cluster work to Muse.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOOM_HOME="${LOOM_HOME:-$HOME/loom}"
WORK="$LOOM_HOME/brain"
LOG="$LOOM_HOME/logs/brain.log"
MODEL="${LOOM_BRAIN_MODEL:-claude-opus-5-5}"
HOLDER="${LOOM_BRAIN_HOLDER:-lane-machine}"
MINUTES="${LOOM_BRAIN_MINUTES:-40}"
SCOPE="${LOOM_BRAIN_SCOPE:-all}"
# How long a tick waits for a lock another brain holds before it skips. A
# live tick is the only one that ships, so by default it waits for Muse's
# tick to finish rather than skip a whole interval; seconds between tries in
# LOOM_BRAIN_WAIT_STEP (tests shorten it).
if [ "$SCOPE" = live ]; then WAIT="${LOOM_BRAIN_WAIT:-25}"; else WAIT="${LOOM_BRAIN_WAIT:-0}"; fi
WAIT_STEP="${LOOM_BRAIN_WAIT_STEP:-60}"
ORIGIN="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null)"
LOCK_REPO="${LOOM_STATUS_REPO:-$(printf '%s' "$ORIGIN" | sed -E 's#^.*[:/]([^/]+/[^/.]+)(\.git)?$#\1#')}"
mkdir -p "$WORK" "$(dirname "$LOG")"

say() { echo "[$(date -u +%FT%TZ)] brain: $*" >> "$LOG"; }

# The station reads these to tell the operator when the brain stops ticking:
# state/brain.json holds the last tick (outcome ok, skipped or failed, with the
# agent's exit code), and state/brain-ok is touched by every tick that ended
# well, a skip for a held lock included (another brain is acting). A tick
# that ran the agent also records when the agent started and its model; the
# station uploads that session's transcript once brain.json says it ended.
STATE="$LOOM_HOME/state"
STARTED=""
beat() {
  local extra=""
  if [ -n "$STARTED" ]; then
    extra=", \"started\": $STARTED"
    [[ "$MODEL" =~ ^[A-Za-z0-9._:/-]+$ ]] && extra="$extra, \"model\": \"$MODEL\""
  fi
  mkdir -p "$STATE"
  printf '{"at": %s, "outcome": "%s", "exit": %s%s}\n' "$(date +%s)" "$1" "$2" "$extra" > "$STATE/brain.json.tmp" &&
    mv "$STATE/brain.json.tmp" "$STATE/brain.json"
  case "$1" in ok|skipped) touch "$STATE/brain-ok" ;; esac
}

[ -n "${LOOM_REPOS:-}" ] || exit 0
[ -n "$ORIGIN" ] && [ -n "$LOCK_REPO" ] || { say "no origin remote in $REPO_ROOT, so no lock repo"; exit 1; }
case "$SCOPE" in all|live) ;; *) say "LOOM_BRAIN_SCOPE must be all or live"; exit 1 ;; esac
[[ "$MINUTES" =~ ^[0-9]+$ ]] && [ "$MINUTES" -ge 5 ] || { say "LOOM_BRAIN_MINUTES must be a number of at least 5"; exit 1; }

bus() { (cd "$REPO_ROOT" && python3 -m bus "$@"); }

# The lock outlives the agent by 5 minutes, so a tick cut off by the timeout
# still releases it below before anyone else could take it.
[[ "$WAIT" =~ ^[0-9]+$ && "$WAIT_STEP" =~ ^[0-9]+$ ]] || { say "LOOM_BRAIN_WAIT and LOOM_BRAIN_WAIT_STEP must be whole numbers"; exit 1; }
deadline=$(( $(date +%s) + WAIT * 60 ))
until bus lock "$LOCK_REPO" --holder "$HOLDER" --minutes "$((MINUTES + 5))" >> "$LOG" 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    beat skipped 0
    exit 0
  fi
  sleep "$WAIT_STEP"
done
trap 'bus unlock "$LOCK_REPO" --holder "$HOLDER" >> "$LOG" 2>&1' EXIT
# launchd stops a job with SIGTERM; exiting on it runs the EXIT trap.
trap 'exit 143' TERM INT
bus() { (cd "$REPO_ROOT" && python3 -m bus "$@"); }
brain() { (cd "$REPO_ROOT" && python3 -m bus.brain "$@"); }
brain step lock --holder "$HOLDER" --lock-repo "$LOCK_REPO" >> "$LOG" 2>&1 || say "could not record lock step"

if [ -d "$WORK/loom/.git" ]; then
  git -C "$WORK/loom" pull -q --ff-only >> "$LOG" 2>&1 || { say "could not update $WORK/loom"; beat failed 1; exit 1; }
else
  git clone -q "$ORIGIN" "$WORK/loom" >> "$LOG" 2>&1 || { say "could not clone $ORIGIN"; beat failed 1; exit 1; }
fi

PROMPT="You are the Loom brain for one tick. Follow $WORK/loom/docs/MUSE.md, \
with these differences: the checkout is already up to date, and the tick lock \
is already held for you as holder $HOLDER by the wrapper, so skip the lock and unlock steps \
(never lock or unlock). Record each other tick step with python3 -m bus.brain \
step NAME --holder $HOLDER --lock-repo $LOCK_REPO --note \"one line: what this \
step found or decided\" in place of the step \
command MUSE.md gives; the wrapper records lock and unlock. LOOM_REPOS is \
set in your environment. Work from \
$WORK/loom. Read each goal's home issue and its rules before acting on that \
goal. This replaces the 15-minute limit in MUSE.md: stop within \
$((MINUTES - 5)) minutes; you are cut off at $MINUTES."
if [ "$SCOPE" = live ]; then
  PROMPT="$PROMPT Muse does the GitHub steps, ships and rolls, and reads \
live data through REST APIs, but has no cluster or database access. Do what \
needs this machine: checks or measurements that need cluster, log or \
database access, put on the ticket for Muse to act on; and a ship or roll \
that is due when Muse has not done it within its tick. Before any measurement, run timeout 120 python3 -m bus.brain \
resume \$LOOM_REPOS --holder $HOLDER --lock-repo $LOCK_REPO (it walks every goal on GitHub, so a stall must not eat the tick) and finish every resume item it lists: work \
another brain yielded, yours however fresh its journals are. Spend at most \
$((MINUTES / 3)) minutes on them. Resume also lists up to 5 verify-due \
tickets (shipped, no proof yet): spend up to a third of the tick on them, \
oldest first. Write a proof only from evidence you saw in this tick; for a \
ticket you cannot verify, leave a one-line note on it naming what evidence is \
missing. Resume also lists one lane-made proof to audit: re-read the \
evidence each criterion names and record it with python3 -m bus.brain audit \
(docs/BRAIN.md, Proof audits). Resume's sensitive pull requests are yours even when another brain \
journaled in the last 2 hours: read the ticket and diff, answer or resolve \
each review thread, then run bus.brain merge with its ticket when ready. Do \
not wait for that brain's next tick. Review, merge, brief and answer asks beyond \
those items only when resume says all GitHub stages are open to you (no other \
brain journaled in the last 2 hours). Defer and done markers go in your \
journal as MUSE.md says. A brief named but not written this tick is deferred \
with the brief stage, never only in journal prose."
fi

say "tick start (model $MODEL, scope $SCOPE)"
STARTED="$(date +%s)"
(cd "$WORK" && timeout "$((MINUTES * 60))" claude -p --model "$MODEL" \
  --permission-mode bypassPermissions "$PROMPT" < /dev/null) >> "$LOG" 2>&1
code=$?
say "tick end (exit $code)"
brain step unlock --holder "$HOLDER" --lock-repo "$LOCK_REPO" >> "$LOG" 2>&1 || say "could not record unlock step"
if [ "$code" = 0 ]; then beat ok 0; else beat failed "$code"; fi
