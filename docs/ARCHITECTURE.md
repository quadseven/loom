# Loom architecture

## The shape

You (the owner) send a goal. Muse (the foreman) runs everything. The
subscriptions are the crew. Nobody shares credentials across lanes.

## Components

The running goal loop lives in `bus/`. Six modules in `dispatch/` are
docstring-only design notes (`dispatch.py`, `metrics.py`, `report.py`,
`ship.py`, `usage_watch.py`, `verify.py`). The remaining module,
`dispatch/dispatcher.py`, routes local ticket files and renders briefs.
Its only callers are `tests/test_dispatcher.py` and `tests/test_repo_config.py`.

### 1. Goal and scoreboard (`bus/metrics.py`)

Each goal defines its metric on its goal home issue in the project's own
repository. The station collects and publishes the factory metrics snapshot.

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

The factory routes tickets using their difficulty, model pins, and lane
availability.

### 5. Shipping (`bus/factory.py` and `bus/station.py`)

The factory gates and merges reviewed pull requests. The station checks for
deployment evidence and marks merged tickets shipped.
Per-goal ship rules (deploy caps, windows when nothing may roll, pins that
move together) live on each goal's home issue in the project's own repo.

### 6. Verification (`bus/station.py`)

The station checks live proof for shipped tickets against their acceptance
criteria.

### 7. Reporting (`bus/brain.py` and `bus/station.py`)

The brain records tick outcomes in goal journals. The station forwards
reports and sends digests on the configured schedule.

## Documentation

- **SDLC.md**: The 8-stage Software Development Life Cycle-brief, run, PR, review, merge, after merge, deploy, verify. Shows how a change moves from ticket to verified.

## Ask policy

Loom asks the owner only for: irreversible actions, rule conflicts,
lineup changes, or HITL tickets. Everything else it does.
