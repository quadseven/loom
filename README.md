# Loom

An agentic coding harness: goal-driven dispatcher, subscription worker
lanes, single shipper. Multi-project by design: one loom, many goals.

## Layout

- `docs/` - architecture, lane contracts, brief format, usage routing
- `dispatch/` - the foreman: metrics, dispatcher, ship, verify, report
- `bus/` - GitHub layer for the brain and station: client, labels, tick lock, journal
- `lanes/` - worker lane runners (one per subscription)
  - `lanes/run_lane.sh` - runs the codex, claude, openrouter, opencode-go,
    anthropic, zen, zai, workersai and agy lanes: one pass over a lane
    inbox, one brief at a time
  - `lanes/mac/install.sh` - installs those lanes as launchd agents
  - `lanes/opencode/` - older opencode runner (OpenRouter, Poolside, other
    APIs); not used by run_lane.sh
  - `lanes/sparks/` - older Nvidia sparks box (test farm + local models);
    deferred, not used by run_lane.sh
- `.github/ISSUE_TEMPLATE/` - ticket template

## Not in git

- `state/` - metrics logs, usage snapshots, ship log (append-only, local)
- `briefs/` - lane inboxes (transport, not history). Briefs and lane logs
  never go to GitHub: this repo is public.
- `worktrees/` - per-ticket disposable clones

Goals live in `~/workspace/goals/<slug>/` with their own tickets and metrics.
Loom serves all active goals.

## Quickstart

1. Read `docs/ARCHITECTURE.md`.
2. Put API keys (OpenRouter, Poolside) in the secure vault; never in files.
3. Install the lanes where each subscription login lives
   (`lanes/mac/install.sh` on the login Mac). The openrouter, opencode-go,
   anthropic and zai lanes read their provider key from SSM at run time, so
   name each parameter in the environment when you run the installer:
   `LOOM_OPENROUTER_KEY_SSM`, `LOOM_OPENCODE_GO_KEY_SSM`,
   `LOOM_ANTHROPIC_KEY_SSM` and `LOOM_ZAI_KEY_SSM` (plus optional
   `LOOM_AWS_REGION`); zen needs no key. The workersai lane also reads its
   Cloudflare account id from SSM, and the agy lane reads its lane user's
   keychain password from SSM and needs the one-time setup in
   docs/LANES.md. Those names are
   written only into the local launchd plists, never into this repo. A lane
   whose key is missing reports itself unavailable and gets no work.
4. Run `dispatch/dispatch.py` on a schedule (every 30 min).
5. Run the tests, the same command CI runs:
   `python -B -m unittest discover -s tests -v`.
   The suite checks that dispatcher briefs forbid merging and carry the repo
   and loom branch, and runs `lanes/run_lane.sh` end to end against a local
   bare repo with fake agent and `gh` commands (push and PR, base branch,
   parking failed runs, and refusing off-limits repos or branches).

## Rules Loom enforces

- One ticket, one fix, one clone, PR only.
- Tests drive behavior and fail without the fix (`python -B`).
- Answer every bot comment, then squash merge.
- One shipper merges and rolls; per-goal ship rules (see goal docs).
- Tickets close only after live verification: the proof log line AND the
 metric it targets moved.
- Subscription ToS: Claude/Codex lanes use official CLIs with the owner's
 login only. No credential sharing into third-party harnesses.
- Never write `@grug` on GitHub. ASCII, no em dashes in GitHub text.
