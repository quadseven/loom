# Loom architecture

## The shape

You (the owner) send a goal. Muse (the foreman) runs everything. The
subscriptions are the crew. Nobody shares credentials across lanes.

## Components

### 1. Goal + scoreboard (`dispatch/metrics.py`)

Every 30 minutes, per active goal, measure the north-star metric and write it
to `state/metrics/<goal>.jsonl` (append-only). Each goal defines its metric
on its goal home issue, in the project's own repo.

### 2. Ticket queue (GitHub issues)

The biggest metric loss becomes the next ticket. One ticket = one independent
fix. Each ticket carries: problem statement, cost in the north-star metric,
repo, the failing test, scope, lane assignment, and definition of done.
Template: `.github/ISSUE_TEMPLATE/ticket.md`.

### 3. Worker lanes (`lanes/`)

Each lane polls its inbox in `briefs/<lane>/`, does the work in a fresh
worktree, and opens a PR. Lanes never merge.

`lanes/run_lane.sh` runs these lanes (the arms of its `run_agent()`; a
slot such as anthropic-2 runs its lane's agent):

- `codex` - scoped implementation, CI repairs, and review passes.
- `claude` - deep root-cause work and hard refactors.
- `anthropic` - API-credit models via the official Claude Code CLI.
- `agy` - Google's Antigravity CLI on the operator's Google plan, run as a
  separate macOS user.
- `openrouter` - free models via opencode.
- `opencode-go` - subscription models via opencode.
- `zen` - OpenCode Zen free models via opencode; public repos only.
- `copilot` - GitHub Copilot Free CLI with a scoped token; public repos only.
- `zai` - Z.ai free GLM Flash models via opencode; public repos only.
- `workersai` - Cloudflare Workers AI models via opencode, within the
  account's free daily allowance.
- `spark` - a self-hosted vLLM coder via opencode, one stream at a time.
- `free` - one free OpenAI-compatible provider per slot (free-kilo,
  free-groq, ...) via opencode, sharing the spark lane's endpoint handling.

API-lane agents run sandboxed without credentials; the runner pushes the
branch and opens the PR. The directories under `lanes/` are support, not
lanes: `agy/` holds the agy lane's settings, `opencode/` the opencode
config, `sparks/` the workshop's test-farm scripts (compute, not a model
lane), `mac/` the launchd ticks, and `evidence/` and `viewer/` the evidence
and transcript servers.

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
Per-goal ship rules (deploy caps, windows when nothing may roll, pins that
move together) live on each goal's home issue in the project's own repo.

### 6. Verifier (`dispatch/verify.py`)

Tickets close only on live proof: the fix's proof log line AND the metric it
targets moved. No green CI alone.

### 7. Reporter (`dispatch/report.py`)

Every 2 hours, five lines to the owner: metric deltas, cleared in order, top
blocker, shipped and verified, next. Decisions indexed daily per goal doc.

## Documentation

- **SDLC.md**: The 8-stage Software Development Life Cycle-brief, run, PR, review, merge, after merge, deploy, verify. Shows how a change moves from ticket to verified.

## Ask policy

Loom asks the owner only for: irreversible actions, rule conflicts,
lineup changes, or HITL tickets. Everything else it does.
