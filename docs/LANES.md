# Lane contracts

Every lane: polls `briefs/<lane>/`, works in a fresh worktree, opens a PR,
never merges. Heartbeats usage + model catalog to `state/usage/<lane>.json`.

## The codex and claude runner

`lanes/run_lane.sh <lane>` makes one pass over `briefs/<lane>/`:

- An optional `Base:` line names the branch to start from and target; the
  default is the remote's default branch.
- Every run passes an explicit model: the brief's `Model:` line, else the
  lane default (`LOOM_<LANE>_MODEL`). The PR footer names the lane and the
  model. A model outside the lane's family is refused; the `openrouter` lane
  runs `:free` models only.
- Each pass writes `state/usage/<lane>.json` with `available` and `at`. A lane
  whose CLI or key is missing reports `available: false` and leaves its briefs
  alone; the dispatcher routes only to fresh, available lanes.

## API lanes: openrouter, opencode-go, anthropic

- `openrouter` (free models via opencode), `opencode-go` (subscription models
  via opencode) and `anthropic` (API credits via the official Claude Code CLI).
- The provider key is read from SSM at run time (`LOOM_<LANE>_KEY_SSM`, set at
  install, never in this repo) and exported to the agent, never passed in
  argv, in an otherwise empty environment with a scratch HOME and TMPDIR.
- The agent runs under a macOS Seatbelt profile (`sandbox-exec`): no reads or
  writes anywhere under the real HOME except its worktree, its scratch home
  and the directory holding its CLI, and no keychain access. It can edit its
  tree but cannot read credentials, push, open, or merge; the runner does
  that with the machine's own credentials. Without `sandbox-exec` an API lane
  reports itself unavailable.
- A brief whose branch already has a PR in any state moves to `done/`
  without running. Restarts cannot re-run a ticket.
- Otherwise: fresh clone, the lane's official CLI with the brief, then the
  runner (not the agent) pushes the branch and opens the PR in the format
  the Chief check requires.
- Any step that fails moves the brief to `failed/` with the reason in
  `logs/<lane>/<ticket>.log`. Failed briefs are never retried; the
  orchestrator re-briefs.
- Logs stay on the lane machine. Nothing is pushed except the ticket branch.
- API-lane session transcripts (Claude Code `.jsonl`, opencode's `opencode.db`)
  are copied to `logs/<lane>/<ticket>/` after each run, before the next run
  wipes the scratch home. Links are never followed.
- No background processes: launchd starts a pass every minute and a lock
  keeps one pass per lane.

## claude/ - deep thought

- Tool: official Claude Code, owner's subscription login, on the owner's
 machine. Nothing else ever holds this credential.
- Trigger: `lanes/run_lane.sh claude` under launchd, official CLI per brief.
- Best for: root-cause investigations, gnarly C++, hard refactors.
- ToS: sanctioned client only. The harness writes briefs; the official CLI
 does the work.

## codex/ - scripted code

- Tool: `codex exec --sandbox workspace-write` on a logged-in box: no network and
 no .git writes, so the agent edits and the runner commits and pushes.
- Best for: scoped implementation, CI failure repair, `codex review` passes.
- Models per ticket: Astra (hardest), Sol (default building), Luna (volume).

## opencode/ - volume

- Tool: headless opencode runs. Providers configured in `config.json`:
 OpenRouter (free tier + paid), Poolside, NVIDIA NIM, local endpoints.
- Best for: small well-scoped tickets, failing tests first, triage, docs.
- Many parallel workers; each in its own worktree.

## sparks/ - the workshop

- Compute, not a model lane. Test farm (full gates: `python -B` suites,
 C++ builds) and local model inference (triage/review workers).
- Rule: sparks power Loom. Sparks never touch a live realm.

## Adding a lane

New lane = directory under `lanes/` with: a poller or runner script, a
heartbeat writer, and a section appended here. No lane may merge, ship, or
hold another lane's credentials.
