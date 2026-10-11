# Loom architecture

## The shape

You (the owner) send a goal. Muse (the foreman) runs everything. The
subscriptions are the crew. Nobody shares credentials across lanes.

## Components

### 1. Goal + scoreboard (`dispatch/metrics.py`)

Every 30 minutes, per active goal, measure the north-star metric and write it
to `state/metrics/<goal>.jsonl` (append-only). For the WoW goal the north star
is member-hours of natural progress: members gaining XP, median and lowest
level, dungeon clears per attempts, class quests done. Other goals define
their own metric in their goal doc.

### 2. Ticket queue (GitHub issues)

The biggest metric loss becomes the next ticket. One ticket = one independent
fix. Each ticket carries: problem statement, cost in the north-star metric,
repo, the failing test, scope, lane assignment, and definition of done.
Template: `.github/ISSUE_TEMPLATE/ticket.md`.

### 3. Worker lanes (`lanes/`)

Each lane polls its inbox in `briefs/<lane>/`, does the work in a fresh
worktree, and opens a PR. Lanes never merge.

- `claude/` - deep root-cause work, hard refactors. Official Claude Code
 with the owner's subscription login. Brief in, PR out.
- `codex/` - scoped implementation, CI repairs, review passes.
 `codex exec --full-auto` on a logged-in box. Models: Astra / Sol / Luna
 chosen per ticket difficulty.
- `opencode/` - volume lane. Headless opencode runs against API providers
 (OpenRouter free tier, Poolside, NVIDIA NIM). Many parallel workers, small
 well-scoped tickets.
- `sparks/` - compute, not a model lane. Test farm (full gates) and local
 model inference. Never touches live realms.

Lane contracts: `docs/LANES.md`.

### 4. Usage-aware routing (`docs/USAGE_ROUTING.md`)

Every lane reports a heartbeat: its model catalog and remaining budget.
Sources: OpenRouter key API, sparks nvidia-smi, official usage websites for
Claude/Codex (their own dashboards, just looking), self-reported burn from
worker logs everywhere else.

The dispatcher scores each ticket easy/medium/hard and assigns the cheapest
lane whose tier fits: frontier (Astra, Opus) for brain surgery, strong
(Sol, Sonnet) for standard building, fast (Luna, Haiku) and free for volume.
Model names are discovered at runtime, never hardcoded.

### 5. Shipper (`dispatch/ship.py`)

One shipper (Muse). Squash merges, answers every bot comment, batches ships.
Every PR is reviewed by grug (Grug - Chief) before merge - no exceptions.
Per-goal ship rules live in the goal doc. WoW rules: at most one worldserver
roll per hour, never while a family is inside a dungeon (wait up to 60 min),
batch bridge restarts, mod-overseer bumps move all five pins together with
worldserver+db-import pinned as a pair.

### 6. Verifier (`dispatch/verify.py`)

Tickets close only on live proof: the fix's proof log line AND the metric it
targets moved. No green CI alone.

### 7. Reporter (`dispatch/report.py`)

Every 2 hours, five lines to the owner: metric deltas, cleared in order, top
blocker, shipped and verified, next. Decisions indexed daily per goal doc.

## Ask policy

Loom asks the owner only for: irreversible actions, rule conflicts,
lineup changes, or HITL tickets. Everything else it does.
