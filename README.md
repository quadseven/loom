# Loom

An agentic coding harness: goal-driven dispatcher, subscription worker
lanes, single shipper. Multi-project by design: one loom, many goals.

## Layout

- `docs/` - architecture, lane contracts, brief format, usage routing
- `dispatch/` - the foreman: metrics, dispatcher, ship, verify, report
- `lanes/` - worker lane runners (one per subscription)
 - `lanes/claude/` - Claude subscription lane (official Claude Code only)
 - `lanes/codex/` - Codex subscription lane (`codex exec`)
 - `lanes/opencode/` - opencode runner (OpenRouter, Poolside, other APIs)
 - `lanes/sparks/` - Nvidia sparks box: test farm + local models
- `.github/ISSUE_TEMPLATE/` - ticket template

## Not in git

- `state/` - metrics logs, usage snapshots, ship log (append-only, local)
- `briefs/` - lane inboxes (transport, not history)
- `worktrees/` - per-ticket disposable clones

Goals live in `~/workspace/goals/<slug>/` with their own tickets and metrics.
Loom serves all active goals.

## Quickstart

1. Read `docs/ARCHITECTURE.md`.
2. Put API keys (OpenRouter, Poolside) in the secure vault; never in files.
3. Install lane pollers where each subscription login lives.
4. Run `dispatch/dispatch.py` on a schedule (every 30 min).

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
